"""Cleanup provider factory (IMPLEMENTATION_PLAN.md §10).

Maps ``config.cleanup.provider`` to a concrete, not-yet-connected
:class:`~seda.cleanup.base.CleanupProvider`, mirroring
:func:`seda.transcription.factory.create_backend`.
"""

from __future__ import annotations

from seda.cleanup.base import CleanupProvider, NoopCleanupProvider
from seda.cleanup.ollama import OllamaCleanupProvider
from seda.config import Config
from seda.errors import ConfigurationError


def create_cleanup_provider(config: Config) -> CleanupProvider:
    """Return a cleanup provider for ``config.cleanup.provider``.

    Raises :class:`ConfigurationError` for an unrecognized provider (the config
    schema already restricts the value, so this is a defensive guard).
    """
    provider = config.cleanup.provider
    if provider == "ollama":
        return OllamaCleanupProvider(config.cleanup)
    if provider == "noop":
        return NoopCleanupProvider()
    raise ConfigurationError(f"unknown cleanup.provider '{provider}' (supported: ollama, noop)")
