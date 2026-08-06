"""Construct a transcription backend from configuration (§10 factory).

Maps ``transcription.backend`` to a concrete implementation. The ``fake``
backend is always available (no extras); ``faster-whisper`` requires the
``whisper`` extra and reports a clear error at load time when it's missing.
"""

from __future__ import annotations

from collections.abc import Callable

from seda.config import Config
from seda.errors import ConfigurationError
from seda.transcription.base import TranscriptionBackend
from seda.transcription.fake import FakeBackend
from seda.transcription.faster_whisper_backend import FasterWhisperBackend


def create_backend(
    config: Config,
    *,
    offline: bool = False,
    cuda_available: Callable[[], bool] | None = None,
) -> TranscriptionBackend:
    """Return a (not-yet-loaded) backend for ``config.transcription.backend``.

    The caller is responsible for calling :meth:`load` before transcribing and
    :meth:`close` afterward.
    """
    backend = config.transcription.backend
    if backend in ("faster-whisper", "faster_whisper"):
        return FasterWhisperBackend(
            config.transcription,
            offline=offline,
            cuda_available=cuda_available,
            custom_vocabulary=config.text.custom_vocabulary,
            vad_filter=(config.audio.vad_backend == "silero"),
        )
    if backend == "fake":
        return FakeBackend()
    raise ConfigurationError(
        f"unknown transcription.backend '{backend}' (supported: faster-whisper, fake)"
    )
