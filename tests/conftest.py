"""Shared test fixtures."""

from __future__ import annotations

import struct
import wave
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_log_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirect the default log file into a temp dir for every test.

    ``configure_logging`` (called by ``seda run``/``transcribe`` with no explicit
    ``log_dir``) resolves its file via ``default_log_path()``. Without this, any
    test that invokes those commands would create/rotate a real file under the
    user log dir (``~/Library/Logs/seda`` etc.) — a filesystem side effect that
    also broke on some CI runners. Point it at a throwaway dir so tests never
    touch the real location; tests that pass ``log_dir`` explicitly are unaffected.
    """
    log_dir = tmp_path_factory.mktemp("seda-logs")
    monkeypatch.setattr("seda.logging_config.default_log_path", lambda: log_dir / "seda.log")


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
