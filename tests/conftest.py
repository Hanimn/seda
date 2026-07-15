"""Shared test fixtures."""

from __future__ import annotations

import struct
import wave
from collections.abc import Callable
from pathlib import Path

import pytest

# A factory that writes a PCM WAV file and returns its path. Signature:
# make_wav(frames: list[int], *, sample_rate=16000, channels=1, width=2) -> Path
WavFactory = Callable[..., Path]


@pytest.fixture
def make_wav(tmp_path: Path) -> WavFactory:
    """Return a factory that writes int16 (by default) PCM WAV files.

    ``frames`` is a flat list of integer samples (interleaved for multi-channel).
    Generated on the fly so no binary audio is committed (see §25).
    """
    counter = {"n": 0}
    fmt = {1: "<b", 2: "<h", 4: "<i"}

    def _make(
        frames: list[int],
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        width: int = 2,
    ) -> Path:
        counter["n"] += 1
        path = tmp_path / f"audio-{counter['n']}.wav"
        packer = fmt[width]
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(width)
            wav.setframerate(sample_rate)
            if width == 1:
                # 8-bit WAV is unsigned; store as bytes centered at 128.
                wav.writeframes(bytes((s + 128) & 0xFF for s in frames))
            else:
                wav.writeframes(b"".join(struct.pack(packer, s) for s in frames))
        return path

    return _make
