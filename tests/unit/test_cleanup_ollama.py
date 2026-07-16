"""Unit tests for the Ollama cleanup provider (IMPLEMENTATION_PLAN.md §15, §26).

No real network: an injected fake HTTP client stands in for httpx, so these
tests assert request shape, timeout/transport-error handling, and availability
without an Ollama server. A live round-trip lives in
``tests/integration/test_ollama_provider.py`` (skipped unless reachable).
"""

from __future__ import annotations

import pytest

from local_flow.cleanup.ollama import OllamaCleanupProvider
from local_flow.config import CleanupConfig
from local_flow.errors import CleanupError

# ---------------------------------------------------------------------------
# Fake httpx client
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise _FakeHTTPStatusError(f"status {self.status_code}")


class _FakeHTTPStatusError(Exception):
    pass


class _FakeClient:
    """Records requests; returns a canned generate response."""

    def __init__(
        self,
        *,
        generate_text: str = "cleaned output",
        tags_ok: bool = True,
        raise_on_post: Exception | None = None,
        raise_on_get: Exception | None = None,
    ) -> None:
        self.generate_text = generate_text
        self.tags_ok = tags_ok
        self.raise_on_post = raise_on_post
        self.raise_on_get = raise_on_get
        self.posts: list[tuple[str, dict[str, object], float | None]] = []
        self.gets: list[str] = []

    def post(
        self, url: str, *, json: dict[str, object], timeout: float | None = None
    ) -> _FakeResponse:
        self.posts.append((url, json, timeout))
        if self.raise_on_post is not None:
            raise self.raise_on_post
        return _FakeResponse({"response": self.generate_text})

    def get(self, url: str, *, timeout: float | None = None) -> _FakeResponse:
        self.gets.append(url)
        if self.raise_on_get is not None:
            raise self.raise_on_get
        return _FakeResponse({"models": []}, status=200 if self.tags_ok else 500)


def _config(**overrides: object) -> CleanupConfig:
    data: dict[str, object] = {"enabled": True}
    data.update(overrides)
    return CleanupConfig(**data)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# clean()
# ---------------------------------------------------------------------------


class TestClean:
    def test_returns_model_response(self) -> None:
        client = _FakeClient(generate_text="fixed text here")
        provider = OllamaCleanupProvider(_config(), http_client=client)
        out = provider.clean("raw text", "standard", [])
        assert out == "fixed text here"

    def test_posts_to_generate_endpoint(self) -> None:
        client = _FakeClient()
        provider = OllamaCleanupProvider(_config(), http_client=client)
        provider.clean("raw", "standard", [])
        url, body, timeout = client.posts[0]
        assert url.endswith("/api/generate")
        assert body["stream"] is False
        assert body["model"]  # a model name is sent
        # Temperature 0 for determinism (§15).
        assert body.get("options", {}).get("temperature") == 0.0  # type: ignore[union-attr]
        # Bounded context and a max output length are sent (§15).
        options = body["options"]
        assert isinstance(options, dict)
        assert options["num_ctx"] > 0
        assert options["num_predict"] > 0
        # Timeout is passed through from config.
        assert timeout == _config().timeout_seconds

    def test_system_prompt_included(self) -> None:
        client = _FakeClient()
        provider = OllamaCleanupProvider(_config(), http_client=client)
        provider.clean("raw", "standard", [])
        _, body, _ = client.posts[0]
        # The strict system prompt is sent (as system + prompt fields).
        assert "cleanup" in str(body.get("system", "")).lower()

    def test_transport_error_raises_cleanup_error(self) -> None:
        client = _FakeClient(raise_on_post=RuntimeError("connection refused"))
        provider = OllamaCleanupProvider(_config(), http_client=client)
        with pytest.raises(CleanupError):
            provider.clean("raw", "standard", [])

    def test_missing_response_field_raises_cleanup_error(self) -> None:
        client = _FakeClient()
        # Override to return a payload with no "response" key.
        client.generate_text = ""

        class _EmptyClient(_FakeClient):
            def post(  # type: ignore[override]
                self,
                url: str,
                *,
                json: dict[str, object],
                timeout: float | None = None,
            ) -> _FakeResponse:
                self.posts.append((url, json, timeout))
                return _FakeResponse({})  # no "response"

        provider = OllamaCleanupProvider(_config(), http_client=_EmptyClient())
        with pytest.raises(CleanupError):
            provider.clean("raw", "standard", [])


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_available_when_tags_ok(self) -> None:
        provider = OllamaCleanupProvider(_config(), http_client=_FakeClient(tags_ok=True))
        assert provider.is_available() is True

    def test_unavailable_on_error(self) -> None:
        client = _FakeClient(raise_on_get=RuntimeError("no server"))
        provider = OllamaCleanupProvider(_config(), http_client=client)
        assert provider.is_available() is False

    def test_unavailable_on_bad_status(self) -> None:
        provider = OllamaCleanupProvider(_config(), http_client=_FakeClient(tags_ok=False))
        assert provider.is_available() is False
