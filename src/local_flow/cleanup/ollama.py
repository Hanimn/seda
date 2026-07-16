"""Ollama-backed cleanup provider (IMPLEMENTATION_PLAN.md §15, §26).

Talks to a local Ollama server over its HTTP API (``/api/generate``,
non-streaming) using ``httpx``. Kept behind the ``cleanup`` optional-extra and
imported lazily so a plain dictation install pulls no HTTP stack.

Safety/privacy (§15, §26, §36):
- Temperature 0, streaming disabled, a request timeout, and ``keep_alive`` from
  config; no indefinite retries.
- Loopback-only by default — a non-loopback ``base_url`` is already rejected at
  config-validation time unless ``allow_remote_endpoint`` is set; this provider
  additionally logs a one-time privacy warning when the endpoint is non-local.
- Request and response bodies are **never logged** (only aggregate metrics,
  recorded by the caller).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

from local_flow.cleanup.prompts import build_system_prompt
from local_flow.config import CleanupConfig
from local_flow.errors import CleanupError

logger = logging.getLogger(__name__)


class OllamaCleanupProvider:
    """Cleanup provider backed by a local Ollama server."""

    def __init__(
        self,
        config: CleanupConfig,
        *,
        http_client: Any | None = None,
    ) -> None:
        self._config = config
        self._ollama = config.ollama
        # An injected client (tests) bypasses httpx entirely; production builds
        # one lazily on first use.
        self._client = http_client
        self._warned_remote = False

    # ------------------------------------------------------------------
    # CleanupProvider protocol
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Best-effort reachability check via ``GET /api/tags``."""
        try:
            client = self._get_client()
            response = client.get(
                self._url("/api/tags"), timeout=self._config.timeout_seconds
            )
        except Exception:  # noqa: BLE001 - any failure means "not available"
            return False
        status = getattr(response, "status_code", 200)
        return bool(status < 400)

    def clean(self, transcript: str, mode: str, vocabulary: list[str]) -> str:
        """Send *transcript* to Ollama and return the cleaned text.

        Raises :class:`CleanupError` on any transport/timeout failure or a
        malformed response, so the caller can fail open to the deterministic
        transcript.
        """
        self._warn_if_remote()
        system_prompt = build_system_prompt(mode, vocabulary)
        body: dict[str, object] = {
            "model": self._ollama.model,
            "system": system_prompt,
            "prompt": transcript,
            "stream": False,
            "keep_alive": self._ollama.keep_alive,
            "options": {
                "temperature": self._ollama.temperature,
                # Bounded context and a max output length tied to the input
                # (§15), so the model cannot expand far past the transcript.
                "num_ctx": self._ollama.num_ctx,
                "num_predict": self._num_predict(transcript),
            },
        }
        try:
            client = self._get_client()
            response = client.post(
                self._url("/api/generate"),
                json=body,
                timeout=self._config.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except CleanupError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as a clean CleanupError
            # Never include the request/response body in the message (§26).
            raise CleanupError(f"cleanup request failed: {type(exc).__name__}") from exc

        text = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text:
            raise CleanupError("cleanup response was empty or malformed")
        return text

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _num_predict(self, transcript: str) -> int:
        """Max tokens to generate, scaled to the input (§15).

        Estimates input tokens as ~1 per 4 characters (a common rough ratio),
        multiplies by ``max_output_ratio``, and floors at a small minimum so a
        very short transcript still has room to be cleaned.
        """
        import math

        est_input_tokens = max(1, len(transcript) // 4)
        scaled = math.ceil(est_input_tokens * self._ollama.max_output_ratio)
        return max(64, scaled)

    def _get_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client()
        return self._client

    def _url(self, path: str) -> str:
        base = self._ollama.base_url
        if not base.endswith("/"):
            base = base + "/"
        return urljoin(base, path.lstrip("/"))

    def _warn_if_remote(self) -> None:
        # Config validation already forbids a non-loopback URL unless the user
        # opted in; if they did, warn once (no content, just the fact).
        if self._warned_remote or not self._config.allow_remote_endpoint:
            return
        from urllib.parse import urlparse

        from local_flow.config import _is_loopback  # local import avoids cycle

        host = urlparse(self._ollama.base_url).hostname or ""
        if not _is_loopback(host):
            logger.warning(
                "cleanup endpoint is non-loopback; transcripts will be sent "
                "off-machine (allow_remote_endpoint is enabled)"
            )
        self._warned_remote = True
