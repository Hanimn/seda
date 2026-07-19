"""faster-whisper transcription backend (see IMPLEMENTATION_PLAN.md §13).

``faster_whisper`` is an optional extra, so it is imported lazily inside
:meth:`FasterWhisperBackend.load` — importing this module never requires the
``whisper`` extra, and unit tests that use the fake backend don't pull it in.

Offline mode maps to CTranslate2/Hugging Face ``local_files_only=True``: a
missing model then raises :class:`ModelUnavailableError` with a clear message
instead of triggering a download.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import numpy as np

from seda.config import TranscriptionConfig
from seda.errors import ModelUnavailableError, TranscriptionError
from seda.logging_config import get_logger
from seda.transcription.base import TranscriptionResult
from seda.transcription.hints import build_initial_prompt
from seda.transcription.selection import (
    compute_type_fallbacks,
    resolve_compute_type,
    resolve_device,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger()


class FasterWhisperBackend:
    """Local speech-to-text via a faster-whisper (CTranslate2) model."""

    def __init__(
        self,
        config: TranscriptionConfig,
        *,
        offline: bool = False,
        cuda_available: Callable[[], bool] | None = None,
    ) -> None:
        self._config = config
        self._offline = offline
        self._cuda_available = cuda_available
        self._model: Any | None = None
        self._device = resolve_device(config.device, cuda_available=cuda_available)

    def load(self) -> None:
        """Load the model, trying auto compute types in order (§13)."""
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ModelUnavailableError(
                "faster-whisper is not installed; install the 'whisper' extra "
                "(e.g. `uv sync --extra whisper`)"
            ) from exc

        model_ref = self._config.model_path or self._config.model
        download_root = self._config.download_root or None

        # For an explicit compute_type there's one candidate; for "auto" we try
        # the device's preference list and keep the first that initializes.
        if self._config.compute_type == "auto":
            candidates = compute_type_fallbacks(self._device)
        else:
            candidates = (resolve_compute_type(self._config.compute_type, self._device),)

        last_error: Exception | None = None
        for compute_type in candidates:
            try:
                self._model = WhisperModel(
                    model_ref,
                    device=self._device,
                    compute_type=compute_type,
                    download_root=download_root,
                    local_files_only=self._offline,
                )
                # Log the resolved selection — never the transcript (§13, §21).
                logger.info(
                    "transcription backend ready: device=%s compute_type=%s model=%s",
                    self._device,
                    compute_type,
                    model_ref,
                )
                return
            except ValueError as exc:
                # CTranslate2 raises ValueError for an unsupported compute type;
                # try the next candidate.
                last_error = exc
                logger.warning(
                    "compute_type %s unavailable on %s, trying next",
                    compute_type,
                    self._device,
                )
            except Exception as exc:  # noqa: BLE001 - classified just below
                last_error = exc
                break

        message = str(last_error) if last_error else "unknown error"
        if self._offline and _looks_like_missing_model(last_error):
            raise ModelUnavailableError(
                f"model '{model_ref}' is not available locally and offline mode "
                "forbids downloading it; run `seda models download` first "
                f"(cause: {message})"
            ) from last_error
        raise ModelUnavailableError(
            f"could not load transcription model '{model_ref}': {message}"
        ) from last_error

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> TranscriptionResult:
        if self._model is None:
            raise TranscriptionError("transcribe called before load")

        initial_prompt = build_initial_prompt([], explicit=self._config.initial_prompt)
        start = time.monotonic()
        try:
            segments, info = self._model.transcribe(
                audio,
                beam_size=self._config.beam_size,
                language=self._config.language or None,
                temperature=self._config.temperature,
                condition_on_previous_text=self._config.condition_on_previous_text,
                initial_prompt=initial_prompt or None,
            )
            # Transcription runs lazily during iteration; join the segments.
            text = "".join(segment.text for segment in segments).strip()
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed error
            raise TranscriptionError(f"transcription failed: {exc}") from exc
        processing = time.monotonic() - start

        return TranscriptionResult(
            text=text,
            language=getattr(info, "language", None),
            language_probability=getattr(info, "language_probability", None),
            duration_seconds=getattr(info, "duration", len(audio) / sample_rate),
            processing_seconds=processing,
        )

    def close(self) -> None:
        # WhisperModel holds native resources; dropping the reference lets
        # CTranslate2 release them. There is no explicit close() to call.
        self._model = None


def _looks_like_missing_model(error: Exception | None) -> bool:
    """Heuristic: does ``error`` indicate an absent local model in offline mode?"""
    if error is None:
        return False
    text = str(error).lower()
    return any(
        marker in text
        for marker in ("local_files_only", "not found", "no such file", "cannot find")
    )
