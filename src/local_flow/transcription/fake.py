"""A deterministic in-memory transcription backend for tests and dry runs.

Returns a fixed transcript without loading any model, so unit tests exercise
the full ``transcribe`` path (audio loading, CLI wiring, timing) without a
model download or network access (see IMPLEMENTATION_PLAN.md §25).
"""

from __future__ import annotations

import time

import numpy as np

from local_flow.transcription.base import TranscriptionResult


class FakeBackend:
    """A backend that echoes a canned transcript.

    ``text`` is the transcript to return; ``loaded`` records whether
    :meth:`load` ran, so tests can assert startup ordering.
    """

    def __init__(self, text: str = "fake transcript") -> None:
        self._text = text
        self.loaded = False
        self.closed = False

    def load(self) -> None:
        self.loaded = True

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> TranscriptionResult:
        start = time.monotonic()
        duration = len(audio) / sample_rate if sample_rate > 0 else 0.0
        return TranscriptionResult(
            text=self._text,
            language="en",
            language_probability=1.0,
            duration_seconds=duration,
            processing_seconds=time.monotonic() - start,
        )

    def close(self) -> None:
        self.closed = True
