"""Integration tests.

These require real hardware, models, or network and are marked ``integration``
so CI's ``-m "not integration"`` run skips them. They run only when explicitly
requested with ``pytest -m integration``.
"""

from __future__ import annotations

import importlib.util

import pytest


@pytest.mark.integration
def test_integration_marker_is_registered() -> None:
    # Deselected by `pytest -m "not integration"`; runs only when explicitly
    # requested.
    assert True


@pytest.mark.integration
@pytest.mark.skipif(
    importlib.util.find_spec("faster_whisper") is None,
    reason="faster-whisper extra not installed",
)
def test_real_faster_whisper_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Real end-to-end transcription with the smallest model. Downloads a model
    # on first run, so it is integration-only and never part of ordinary CI.
    import struct
    import wave

    from local_flow.audio.loading import load_wav
    from local_flow.config import load_config_from_dict
    from local_flow.transcription.factory import create_backend

    wav = tmp_path / "tone.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", 0) for _ in range(16000)))

    audio = load_wav(wav)
    config = load_config_from_dict(
        {"transcription": {"backend": "faster-whisper", "model": "tiny.en", "device": "cpu"}}
    )
    backend = create_backend(config)
    backend.load()
    try:
        result = backend.transcribe(audio.samples, audio.sample_rate)
    finally:
        backend.close()
    # Silence may transcribe to empty text; we only assert the call succeeds
    # and returns the expected shape.
    assert isinstance(result.text, str)
    assert result.processing_seconds >= 0.0
