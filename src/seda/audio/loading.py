"""WAV file loading for ``seda transcribe`` (see IMPLEMENTATION_PLAN.md §13).

Uses the standard-library :mod:`wave` module plus NumPy — no extra dependency —
to read a PCM WAV file into a mono ``float32`` array in ``[-1.0, 1.0]``, which
is the shape transcription backends expect. Stereo is downmixed to mono.

Only PCM WAV is supported here; other containers/codecs (MP3, FLAC) are a
later concern and raise a clear :class:`~seda.errors.AudioError`.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from seda.errors import AudioError, EmptyAudioError

# Map WAV sample width (bytes) → the signed integer dtype it decodes to.
_WIDTH_TO_DTYPE = {1: np.uint8, 2: np.int16, 4: np.int32}


@dataclass(frozen=True)
class LoadedAudio:
    """A decoded audio buffer: mono ``float32`` samples and their rate."""

    samples: np.ndarray
    sample_rate: int

    @property
    def duration_seconds(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return len(self.samples) / self.sample_rate


def load_wav(path: Path) -> LoadedAudio:
    """Load a PCM WAV file as mono ``float32`` in ``[-1.0, 1.0]``.

    Raises :class:`AudioError` for a missing file, a non-WAV/unsupported file,
    or an empty recording.
    """
    if not path.exists():
        raise AudioError(f"audio file not found: {path}")

    try:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as exc:
        raise AudioError(f"{path} is not a readable PCM WAV file: {exc}") from exc

    if width not in _WIDTH_TO_DTYPE:
        raise AudioError(
            f"{path} has an unsupported sample width of {width * 8} bits (supported: 8, 16, 32)"
        )
    if not frames:
        raise EmptyAudioError(f"{path} contains no audio samples")

    raw = np.frombuffer(frames, dtype=_WIDTH_TO_DTYPE[width])
    if channels > 1:
        # Interleaved frames → (frames, channels), then average to mono.
        raw = raw.reshape(-1, channels).mean(axis=1)

    samples = _to_float32(raw, width)
    return LoadedAudio(samples=samples, sample_rate=sample_rate)


def _to_float32(raw: np.ndarray, width: int) -> np.ndarray:
    """Convert decoded PCM samples to ``float32`` in ``[-1.0, 1.0]``."""
    data = raw.astype(np.float32)
    if width == 1:
        # 8-bit WAV is unsigned, centered at 128.
        return (data - 128.0) / 128.0
    # 16-/32-bit are signed; normalize by the type's max magnitude.
    max_magnitude = float(2 ** (width * 8 - 1))
    return data / max_magnitude
