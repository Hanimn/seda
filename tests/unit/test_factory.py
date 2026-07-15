"""Unit tests for the transcription backend factory (see §10, §25).

These never load a real model: the ``fake`` backend needs none, and the
``faster-whisper`` cases assert on load-time error behavior with a synthetic
config, not a download.
"""

from __future__ import annotations

import pytest

from local_flow.config import load_config_from_dict
from local_flow.errors import ConfigurationError, ModelUnavailableError
from local_flow.transcription.factory import create_backend
from local_flow.transcription.fake import FakeBackend
from local_flow.transcription.faster_whisper_backend import FasterWhisperBackend


def test_factory_returns_fake_backend() -> None:
    config = load_config_from_dict({"transcription": {"backend": "fake"}})
    assert isinstance(create_backend(config), FakeBackend)


def test_factory_returns_faster_whisper_backend() -> None:
    config = load_config_from_dict({"transcription": {"backend": "faster-whisper"}})
    assert isinstance(create_backend(config), FasterWhisperBackend)


def test_factory_rejects_unknown_backend() -> None:
    config = load_config_from_dict({"transcription": {"backend": "magic"}})
    with pytest.raises(ConfigurationError) as exc:
        create_backend(config)
    assert "unknown transcription.backend" in str(exc.value)


def test_fake_backend_lifecycle() -> None:
    import numpy as np

    backend = FakeBackend("hello world")
    backend.load()
    assert backend.loaded
    result = backend.transcribe(np.zeros(16000, dtype=np.float32), 16000)
    assert result.text == "hello world"
    assert result.duration_seconds == pytest.approx(1.0)
    backend.close()
    assert backend.closed


def test_faster_whisper_missing_package_reports_model_unavailable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Simulate the 'whisper' extra not being installed: importing faster_whisper
    # fails, and load() must translate that into a readable ModelUnavailableError.
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if name == "faster_whisper" or name.startswith("faster_whisper."):
            raise ImportError("No module named 'faster_whisper'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    config = load_config_from_dict({"transcription": {"backend": "faster-whisper"}})
    backend = create_backend(config, offline=True)
    with pytest.raises(ModelUnavailableError) as exc:
        backend.load()
    assert "whisper" in str(exc.value).lower()
