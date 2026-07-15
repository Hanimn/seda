"""Transcription backend interface (see IMPLEMENTATION_PLAN.md §10).

A backend loads a model, turns a mono ``float32`` audio array into a
:class:`TranscriptionResult`, and releases its resources. Concrete backends
live alongside this module (``faster_whisper_backend``, ``fake``) and are
constructed via :mod:`local_flow.transcription.factory`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class TranscriptionResult:
    """The outcome of transcribing one audio buffer.

    ``duration_seconds`` is the audio length; ``processing_seconds`` is the
    wall-clock time the backend spent, measured with a monotonic clock.
    """

    text: str
    language: str | None
    language_probability: float | None
    duration_seconds: float
    processing_seconds: float


class TranscriptionBackend(Protocol):
    def load(self) -> None:
        """Load the model. Called once at startup, before any transcribe."""
        ...

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> TranscriptionResult:
        """Transcribe a mono ``float32`` array sampled at ``sample_rate``."""
        ...

    def close(self) -> None:
        """Release model resources. Safe to call more than once."""
        ...
