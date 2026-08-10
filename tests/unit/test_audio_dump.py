"""Unit tests for debug-audio retention (app.retain_debug_audio, #126)."""

from __future__ import annotations

import stat
import wave
from pathlib import Path

import numpy as np
import pytest

from seda.audio.dump import write_debug_wav
from seda.errors import AudioError


def test_writes_valid_int16_wav(tmp_path: Path) -> None:
    samples = (np.sin(np.linspace(0, 6, 1600))).astype(np.float32) * 0.5
    out = write_debug_wav(tmp_path, samples, 16000)
    assert out.parent == tmp_path
    assert out.name.startswith("seda-")
    assert out.suffix == ".wav"
    with wave.open(str(out), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.getnframes() == 1600


def test_file_is_owner_only_from_birth(tmp_path: Path) -> None:
    out = write_debug_wav(tmp_path, np.zeros(100, dtype=np.float32), 16000)
    # Debug audio is retained speech — 0600 from creation, never wider.
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_samples_round_trip(tmp_path: Path) -> None:
    samples = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
    out = write_debug_wav(tmp_path, samples, 16000)
    with wave.open(str(out), "rb") as wf:
        raw = np.frombuffer(wf.readframes(5), dtype=np.int16)
    assert raw[1] == pytest.approx(0.5 * 32767, abs=1)
    assert raw[2] == pytest.approx(-0.5 * 32768, abs=1)
    assert raw[3] == 32767


def test_empty_samples_rejected(tmp_path: Path) -> None:
    with pytest.raises(AudioError):
        write_debug_wav(tmp_path, np.array([], dtype=np.float32), 16000)


def test_unwritable_directory_raises_audio_error(tmp_path: Path) -> None:
    with pytest.raises(AudioError):
        write_debug_wav(tmp_path / "does-not-exist", np.zeros(100, dtype=np.float32), 16000)
