"""Callback-based microphone recorder (see IMPLEMENTATION_PLAN.md §12).

The audio callback copies blocks into a list — no disk I/O, no inference.
Overflow events increment a diagnostic counter.  ``stop()`` concatenates
blocks, converts to mono float32, applies energy-based silence trimming, and
enforces min/max duration.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

from local_flow.errors import AudioError, EmptyAudioError

# Recorder output sample rate.
TARGET_SAMPLE_RATE = 16_000

# Energy threshold for silence (RMS, float32 range).
_DEFAULT_VAD_THRESHOLD = 0.015
_DEFAULT_LEADING_PAD_MS = 150
_DEFAULT_TRAILING_PAD_MS = 300
_DEFAULT_MIN_DURATION_MS = 250
_DEFAULT_MAX_DURATION_S = 180


@dataclass
class RecordedAudio:
    """A finished microphone recording."""

    samples: np.ndarray  # mono, float32, TARGET_SAMPLE_RATE
    sample_rate: int
    overflow_count: int

    @property
    def duration_seconds(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return len(self.samples) / self.sample_rate

    @property
    def peak_level(self) -> float:
        if self.samples.size == 0:
            return 0.0
        return float(np.max(np.abs(self.samples)))

    @property
    def clipping(self) -> bool:
        return self.peak_level >= 0.99

    @property
    def speech_detected(self) -> bool:
        return _rms(self.samples) >= _DEFAULT_VAD_THRESHOLD


@dataclass
class RecorderConfig:
    """Tunable parameters for :class:`SounddeviceRecorder`."""

    device: int | str | None = None
    sample_rate: int = TARGET_SAMPLE_RATE
    blocksize: int = 1024
    vad_threshold: float = _DEFAULT_VAD_THRESHOLD
    leading_padding_ms: int = _DEFAULT_LEADING_PAD_MS
    trailing_padding_ms: int = _DEFAULT_TRAILING_PAD_MS
    min_duration_ms: int = _DEFAULT_MIN_DURATION_MS
    max_duration_seconds: float = _DEFAULT_MAX_DURATION_S


class RecordingTooShortError(AudioError):
    """Recording was shorter than the minimum duration."""


class RecordingTooLongError(AudioError):
    """Recording exceeded the maximum duration and was not auto-stopped."""


class SounddeviceRecorder:
    """Callback-based microphone recorder backed by ``sounddevice``.

    Usage::

        recorder = SounddeviceRecorder()
        recorder.start()
        ...user speaks...
        audio = recorder.stop()
    """

    def __init__(self, config: RecorderConfig | None = None) -> None:
        self._cfg = config or RecorderConfig()
        self._blocks: list[np.ndarray] = []
        self._overflow_count: int = 0
        self._stream: Any = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the microphone stream and begin collecting audio blocks."""
        try:
            import sounddevice as sd
        except (ImportError, OSError) as exc:
            raise AudioError(f"sounddevice is not available: {exc}") from exc

        with self._lock:
            self._blocks = []
            self._overflow_count = 0
            self._stop_event.clear()

        try:
            self._stream = sd.InputStream(
                device=self._cfg.device,
                samplerate=self._cfg.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self._cfg.blocksize,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001
            raise AudioError(f"could not open microphone stream: {exc}") from exc

    def stop(self) -> RecordedAudio:
        """Stop recording and return processed audio.

        Raises:
            RecordingTooShortError: if the trimmed audio is below min duration.
            AudioError: if the stream was never started.
        """
        self._stop_event.set()
        self._close_stream()

        with self._lock:
            blocks = list(self._blocks)
            overflow = self._overflow_count

        if not blocks:
            raise EmptyAudioError("no audio was recorded")

        raw = np.concatenate(blocks, axis=0).squeeze()
        samples = _to_mono_float32(raw)
        samples = _trim_silence(
            samples,
            self._cfg.sample_rate,
            self._cfg.vad_threshold,
            self._cfg.leading_padding_ms,
            self._cfg.trailing_padding_ms,
        )

        min_samples = int(self._cfg.min_duration_ms / 1000 * self._cfg.sample_rate)
        if len(samples) < min_samples:
            raise RecordingTooShortError(
                f"recording is too short (minimum {self._cfg.min_duration_ms} ms)"
            )

        return RecordedAudio(
            samples=samples,
            sample_rate=self._cfg.sample_rate,
            overflow_count=overflow,
        )

    def cancel(self) -> None:
        """Discard the current recording without processing."""
        self._stop_event.set()
        self._close_stream()
        with self._lock:
            self._blocks = []
            self._overflow_count = 0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _callback(
        self,
        indata: np.ndarray,
        frames: int,  # noqa: ARG002
        time: object,  # noqa: ARG002
        status: object,
    ) -> None:
        """sounddevice audio callback — runs on a dedicated C thread.

        Must not do disk I/O, model inference, or expensive logging.
        """
        if status:
            with self._lock:
                self._overflow_count += 1
        with self._lock:
            self._blocks.append(indata.copy())

    def _close_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------------
# Pure audio-processing helpers (tested independently)
# ------------------------------------------------------------------


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def _to_mono_float32(samples: np.ndarray) -> np.ndarray:
    """Convert to mono float32, handling stereo or multi-channel input."""
    arr = np.asarray(samples, dtype=np.float32)
    if arr.ndim == 2 and arr.shape[1] > 1:
        arr = arr.mean(axis=1)
    elif arr.ndim == 2:
        arr = arr[:, 0]
    return arr


def _frame_energy(samples: np.ndarray, sample_rate: int, frame_ms: int = 20) -> np.ndarray:
    """Compute per-frame RMS energy with *frame_ms* millisecond frames."""
    frame_len = max(1, int(sample_rate * frame_ms / 1000))
    n_frames = len(samples) // frame_len
    if n_frames == 0:
        return np.array([_rms(samples)], dtype=np.float32)
    trimmed = samples[: n_frames * frame_len]
    frames = trimmed.reshape(n_frames, frame_len)
    rms: np.ndarray = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1)).astype(np.float32)
    return rms


def _trim_silence(
    samples: np.ndarray,
    sample_rate: int,
    threshold: float,
    leading_pad_ms: int,
    trailing_pad_ms: int,
) -> np.ndarray:
    """Trim leading and trailing silence by energy, then re-add padding."""
    if samples.size == 0:
        return samples

    frame_ms = 20
    frame_len = max(1, int(sample_rate * frame_ms / 1000))
    energies = _frame_energy(samples, sample_rate, frame_ms)
    speech_frames = np.where(energies >= threshold)[0]

    if speech_frames.size == 0:
        # No speech — return a single frame's worth so RecordingTooShortError fires.
        return samples[:frame_len]

    first = speech_frames[0]
    last = speech_frames[-1]

    lead_frames = max(0, first - int(leading_pad_ms / frame_ms))
    trail_frames = min(len(energies) - 1, last + int(trailing_pad_ms / frame_ms))

    start = lead_frames * frame_len
    end = min(len(samples), (trail_frames + 1) * frame_len)
    return samples[start:end]
