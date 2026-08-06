"""Unit tests for audio/recorder.py — pure audio-processing helpers.

Tests run entirely over generated NumPy arrays; no real microphone required.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

from seda.audio.recorder import (
    RecordedAudio,
    RecorderConfig,
    RecordingTooShortError,
    SounddeviceRecorder,
    _frame_energy,
    _to_mono_float32,
    _trim_silence,
)
from seda.errors import EmptyAudioError

RATE = 16_000


def _silence(n_samples: int) -> np.ndarray:
    return np.zeros(n_samples, dtype=np.float32)


def _tone(n_samples: int, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(n_samples, dtype=np.float32) / RATE
    return (np.sin(2 * np.pi * 440 * t) * amplitude).astype(np.float32)


def _stereo(n_samples: int, amplitude: float = 0.5) -> np.ndarray:
    ch = _tone(n_samples, amplitude)
    return np.stack([ch, ch], axis=1)


class TestToMonoFloat32:
    def test_1d_passthrough(self):
        arr = _tone(RATE)
        out = _to_mono_float32(arr)
        assert out.dtype == np.float32
        assert out.ndim == 1

    def test_stereo_averages_to_mono(self):
        left = np.ones(100, dtype=np.float32) * 0.8
        right = np.ones(100, dtype=np.float32) * 0.4
        stereo = np.stack([left, right], axis=1)
        out = _to_mono_float32(stereo)
        assert out.ndim == 1
        np.testing.assert_allclose(out, 0.6, atol=1e-6)

    def test_single_channel_2d(self):
        arr = _tone(100).reshape(-1, 1)
        out = _to_mono_float32(arr)
        assert out.ndim == 1
        assert len(out) == 100

    def test_output_dtype_is_float32(self):
        arr = np.array([1, 2, 3], dtype=np.int16)
        out = _to_mono_float32(arr)
        assert out.dtype == np.float32


class TestTrimSilence:
    def test_silence_returns_minimal_frame(self):
        silence = _silence(RATE)
        out = _trim_silence(silence, RATE, threshold=0.015, leading_pad_ms=150, trailing_pad_ms=300)
        assert len(out) > 0
        assert len(out) < len(silence)

    def test_speech_in_middle_is_preserved(self):
        # 1 s silence + 1 s speech + 1 s silence
        signal = np.concatenate([_silence(RATE), _tone(RATE, 0.2), _silence(RATE)])
        out = _trim_silence(signal, RATE, threshold=0.015, leading_pad_ms=0, trailing_pad_ms=0)
        # Output should be shorter than the full 3 s
        assert len(out) < len(signal)
        # But still contain the speech
        assert len(out) >= RATE * 0.8

    def test_padding_is_added_back(self):
        signal = np.concatenate([_silence(RATE), _tone(RATE, 0.3), _silence(RATE)])
        trim = dict(threshold=0.015, leading_pad_ms=0, trailing_pad_ms=0)
        pad = dict(threshold=0.015, leading_pad_ms=150, trailing_pad_ms=300)
        out_no_pad = _trim_silence(signal, RATE, **trim)  # type: ignore[arg-type]
        out_pad = _trim_silence(signal, RATE, **pad)  # type: ignore[arg-type]
        assert len(out_pad) >= len(out_no_pad)

    def test_empty_input_returns_empty(self):
        out = _trim_silence(np.array([], dtype=np.float32), RATE, 0.015, 150, 300)
        assert len(out) == 0

    def test_quiet_speech_below_threshold(self):
        # Very quiet signal — below VAD threshold — treated as silence.
        quiet = _tone(RATE, amplitude=0.001)
        out = _trim_silence(quiet, RATE, threshold=0.015, leading_pad_ms=0, trailing_pad_ms=0)
        assert len(out) < len(quiet)


class TestFrameEnergy:
    def test_silence_gives_zero_energy(self):
        energies = _frame_energy(_silence(RATE), RATE)
        assert np.all(energies < 1e-6)

    def test_clipping_signal_high_energy(self):
        clip = np.ones(RATE, dtype=np.float32)
        energies = _frame_energy(clip, RATE)
        assert np.all(energies >= 0.99)

    def test_shape(self):
        n_frames = RATE // max(1, int(RATE * 20 / 1000))
        energies = _frame_energy(_tone(RATE), RATE, frame_ms=20)
        assert energies.ndim == 1
        assert len(energies) == n_frames


class TestRecordedAudio:
    def _make(self, samples: np.ndarray, overflow: int = 0) -> RecordedAudio:
        return RecordedAudio(samples=samples, sample_rate=RATE, overflow_count=overflow)

    def test_duration(self):
        audio = self._make(_tone(RATE))
        assert abs(audio.duration_seconds - 1.0) < 0.01

    def test_peak_silence(self):
        audio = self._make(_silence(RATE))
        assert audio.peak_level < 1e-6

    def test_peak_clipping(self):
        audio = self._make(np.ones(RATE, dtype=np.float32))
        assert audio.clipping is True

    def test_peak_no_clipping(self):
        audio = self._make(_tone(RATE, 0.5))
        assert audio.clipping is False

    def test_speech_detected_above_threshold(self):
        audio = self._make(_tone(RATE, 0.2))
        assert audio.speech_detected is True

    def test_speech_not_detected_silence(self):
        audio = self._make(_silence(RATE))
        assert audio.speech_detected is False

    def test_empty_samples(self):
        audio = self._make(np.array([], dtype=np.float32))
        assert audio.duration_seconds == 0.0
        assert audio.peak_level == 0.0
        assert audio.speech_detected is False


class TestSounddeviceRecorderStopProcessing:
    """Test stop() processing path using monkeypatched sounddevice."""

    def _make_recorder(
        self, monkeypatch, blocks: list[np.ndarray], config: RecorderConfig | None = None
    ) -> SounddeviceRecorder:
        """Build a SounddeviceRecorder with pre-filled blocks, no real stream."""
        sd_mock = MagicMock()
        stream_mock = MagicMock()
        sd_mock.InputStream.return_value = stream_mock
        monkeypatch.setitem(sys.modules, "sounddevice", sd_mock)

        rec = SounddeviceRecorder(config or RecorderConfig(min_duration_ms=50))
        rec._blocks = blocks
        rec._overflow_count = 0
        rec._stream = stream_mock
        return rec

    def test_stop_concatenates_blocks(self, monkeypatch):
        tone = _tone(RATE, 0.3)
        blocks = [tone[: RATE // 2].reshape(-1, 1), tone[RATE // 2 :].reshape(-1, 1)]
        rec = self._make_recorder(monkeypatch, blocks)
        audio = rec.stop()
        assert audio.sample_rate == RATE
        assert audio.samples.dtype == np.float32

    def test_stop_empty_blocks_raises(self, monkeypatch):
        rec = self._make_recorder(monkeypatch, [])
        with pytest.raises(EmptyAudioError):
            rec.stop()

    def test_stop_too_short_raises(self, monkeypatch):
        # 10 ms of speech — well below 50 ms min
        short = _tone(int(RATE * 0.01), 0.3).reshape(-1, 1)
        rec = self._make_recorder(monkeypatch, [short])
        with pytest.raises(RecordingTooShortError):
            rec.stop()

    def test_stop_trims_silence_by_default(self, monkeypatch):
        # 1 s silence + 1 s speech + 1 s silence, trimming on (default).
        signal = np.concatenate([_silence(RATE), _tone(RATE, 0.3), _silence(RATE)])
        rec = self._make_recorder(monkeypatch, [signal.reshape(-1, 1)])
        audio = rec.stop()
        assert len(audio.samples) < len(signal)

    def test_stop_without_trim_keeps_full_length(self, monkeypatch):
        # trim_silence=False must leave the buffer untouched (raw audio).
        signal = np.concatenate([_silence(RATE), _tone(RATE, 0.3), _silence(RATE)])
        rec = self._make_recorder(
            monkeypatch,
            [signal.reshape(-1, 1)],
            RecorderConfig(min_duration_ms=50, trim_silence=False),
        )
        audio = rec.stop()
        assert len(audio.samples) == len(signal)


class TestStartStream:
    """start() honors the configured channel count (mono default, stereo opt-in)."""

    def _sd_mock(self, monkeypatch) -> MagicMock:
        sd_mock = MagicMock()
        sd_mock.InputStream.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "sounddevice", sd_mock)
        return sd_mock

    def test_start_defaults_to_mono(self, monkeypatch):
        sd_mock = self._sd_mock(monkeypatch)
        SounddeviceRecorder(RecorderConfig()).start()
        assert sd_mock.InputStream.call_args.kwargs["channels"] == 1

    def test_start_uses_configured_channels(self, monkeypatch):
        sd_mock = self._sd_mock(monkeypatch)
        SounddeviceRecorder(RecorderConfig(channels=2)).start()
        assert sd_mock.InputStream.call_args.kwargs["channels"] == 2


def test_recorder_config_defaults() -> None:
    cfg = RecorderConfig()
    assert cfg.trim_silence is True
    assert cfg.channels == 1


class TestLatestLevel:
    """The pulled per-block RMS hand-off for the overlay (ADR-0002)."""

    def test_idle_level_is_zero(self):
        rec = SounddeviceRecorder(RecorderConfig())
        assert rec.latest_level == 0.0

    def test_callback_updates_level_to_block_rms(self):
        from seda.audio.recorder import _rms

        rec = SounddeviceRecorder(RecorderConfig())
        block = _tone(1024, 0.4).reshape(-1, 1)
        rec._callback(block, 1024, None, None)
        # latest_level reflects the RMS of the block just seen.
        assert rec.latest_level == pytest.approx(_rms(block), rel=1e-6)
        assert rec.latest_level > 0.0

    def test_silence_block_gives_near_zero_level(self):
        rec = SounddeviceRecorder(RecorderConfig())
        rec._callback(_silence(1024).reshape(-1, 1), 1024, None, None)
        assert rec.latest_level == pytest.approx(0.0, abs=1e-6)

    def test_cancel_resets_level_to_zero(self, monkeypatch):
        rec = SounddeviceRecorder(RecorderConfig())
        rec._callback(_tone(1024, 0.4).reshape(-1, 1), 1024, None, None)
        assert rec.latest_level > 0.0
        # cancel() must not require a real stream.
        monkeypatch.setattr(rec, "_close_stream", lambda: None)
        rec.cancel()
        assert rec.latest_level == 0.0

    def test_level_held_after_stop(self, monkeypatch):
        # After stop(), latest_level retains the last value (bars settle, ADR-0002).
        rec = SounddeviceRecorder(RecorderConfig(min_duration_ms=1))
        block = _tone(RATE, 0.4).reshape(-1, 1)
        rec._callback(block, RATE, None, None)
        held = rec.latest_level
        assert held > 0.0
        monkeypatch.setattr(rec, "_close_stream", lambda: None)
        rec.stop()
        assert rec.latest_level == pytest.approx(held, rel=1e-6)

    def test_raising_rms_never_breaks_recording(self, monkeypatch):
        # If level computation raises, the block is still recorded and the
        # callback does not propagate (fail-open, ADR-0002).
        import seda.audio.recorder as recorder_mod

        def _boom(_samples):
            raise RuntimeError("rms exploded")

        monkeypatch.setattr(recorder_mod, "_rms", _boom)
        rec = SounddeviceRecorder(RecorderConfig())
        block = _tone(1024, 0.4).reshape(-1, 1)
        rec._callback(block, 1024, None, None)  # must not raise
        # The block was still captured; the level simply stayed at its prior value.
        assert len(rec._blocks) == 1
        assert rec.latest_level == 0.0
