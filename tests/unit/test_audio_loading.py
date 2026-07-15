"""Unit tests for WAV loading (see §13, §25)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from local_flow.audio.loading import load_wav
from local_flow.errors import AudioError, EmptyAudioError

WavFactory = Callable[..., Path]


def test_load_mono_int16(make_wav: WavFactory) -> None:
    audio = load_wav(make_wav([0, 16383, -16384, 32767, -32768], sample_rate=16000))
    assert audio.sample_rate == 16000
    assert audio.samples.dtype == np.float32
    assert len(audio.samples) == 5
    # Full-scale samples map close to +/-1.0.
    assert audio.samples.max() == pytest.approx(1.0, abs=1e-4)
    assert audio.samples.min() == pytest.approx(-1.0, abs=1e-4)


def test_stereo_is_downmixed_to_mono(make_wav: WavFactory) -> None:
    # Two frames, two channels: [L0, R0, L1, R1].
    audio = load_wav(make_wav([10000, 20000, -10000, -20000], channels=2))
    assert len(audio.samples) == 2  # frames, not samples
    # First frame is the mean of 10000 and 20000, normalized.
    assert audio.samples[0] == pytest.approx(15000 / 32768, abs=1e-3)


def test_eight_bit_is_centered(make_wav: WavFactory) -> None:
    # 8-bit silence is 0 after centering (stored as 128).
    audio = load_wav(make_wav([0, 0, 0], width=1))
    assert np.allclose(audio.samples, 0.0, atol=1e-6)


def test_duration_seconds(make_wav: WavFactory) -> None:
    audio = load_wav(make_wav([0] * 8000, sample_rate=16000))
    assert audio.duration_seconds == pytest.approx(0.5)


def test_missing_file_raises_audio_error(tmp_path: Path) -> None:
    with pytest.raises(AudioError) as exc:
        load_wav(tmp_path / "nope.wav")
    assert "not found" in str(exc.value)


def test_empty_wav_raises_audio_error(make_wav: WavFactory) -> None:
    with pytest.raises(AudioError) as exc:
        load_wav(make_wav([]))
    assert "no audio samples" in str(exc.value)


def test_empty_wav_raises_specific_empty_audio_error(make_wav: WavFactory) -> None:
    # §23 gives empty audio its own type; it must still be an AudioError.
    with pytest.raises(EmptyAudioError):
        load_wav(make_wav([]))


def test_non_wav_file_raises_audio_error(tmp_path: Path) -> None:
    bogus = tmp_path / "not-audio.wav"
    bogus.write_text("this is not a wav", encoding="utf-8")
    with pytest.raises(AudioError) as exc:
        load_wav(bogus)
    assert "not a readable" in str(exc.value)
