"""Integration test for the Ollama cleanup provider (IMPLEMENTATION_PLAN.md §15).

Requires the ``cleanup`` extra (httpx) AND a running Ollama server, so it is
marked ``integration`` and additionally skipped when httpx is missing or the
endpoint is unreachable. It never runs in ordinary CI (``-m "not integration"``).
"""

from __future__ import annotations

import importlib.util

import pytest

_HTTPX_MISSING = importlib.util.find_spec("httpx") is None


def _ollama_reachable(base_url: str) -> bool:
    """Best-effort probe: True only if Ollama answers /api/tags quickly."""
    if _HTTPX_MISSING:
        return False
    import httpx

    try:
        response = httpx.get(f"{base_url}/api/tags", timeout=1.0)
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False
    return bool(response.status_code < 400)


_BASE_URL = "http://127.0.0.1:11434"


@pytest.mark.integration
@pytest.mark.skipif(_HTTPX_MISSING, reason="cleanup extra (httpx) not installed")
@pytest.mark.skipif(
    not _ollama_reachable(_BASE_URL), reason="Ollama server not reachable"
)
def test_real_ollama_cleanup_preserves_placeholders() -> None:  # type: ignore[no-untyped-def]
    from local_flow.cleanup.ollama import OllamaCleanupProvider
    from local_flow.config import load_config_from_dict
    from local_flow.text.technical_tokens import protect

    config = load_config_from_dict({"cleanup": {"enabled": True}})
    provider = OllamaCleanupProvider(config.cleanup)
    assert provider.is_available()

    protected, registry = protect("um so check src/auth/middleware.ts please")
    cleaned = provider.clean(protected, "standard", [])

    # Placeholders must survive cleanup so restore() succeeds.
    for placeholder in registry.placeholders_in_order():
        assert placeholder in cleaned, f"cleanup dropped placeholder {placeholder}"
    assert isinstance(cleaned, str) and cleaned.strip()
