"""Unit tests for the cleanup provider factory (IMPLEMENTATION_PLAN.md §10)."""

from __future__ import annotations

import pytest

from local_flow.cleanup.base import NoopCleanupProvider
from local_flow.cleanup.factory import create_cleanup_provider
from local_flow.cleanup.ollama import OllamaCleanupProvider
from local_flow.config import load_config_from_dict
from local_flow.errors import ConfigurationError


def _cfg(**cleanup: object):  # type: ignore[no-untyped-def]
    return load_config_from_dict({"cleanup": {"enabled": True, **cleanup}})


class TestFactory:
    def test_ollama_provider(self) -> None:
        provider = create_cleanup_provider(_cfg(provider="ollama"))
        assert isinstance(provider, OllamaCleanupProvider)

    def test_noop_provider(self) -> None:
        provider = create_cleanup_provider(_cfg(provider="noop"))
        assert isinstance(provider, NoopCleanupProvider)

    def test_unknown_provider_raises(self) -> None:
        # An unknown provider string is rejected at config validation already,
        # but the factory guards defensively too.
        cfg = _cfg(provider="ollama")
        object.__setattr__(cfg.cleanup, "provider", "bogus")
        with pytest.raises(ConfigurationError, match="cleanup.provider"):
            create_cleanup_provider(cfg)
