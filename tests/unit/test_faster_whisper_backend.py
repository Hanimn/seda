"""Unit tests for the faster-whisper backend's prompt biasing (see §13).

These never load a real model or require the ``whisper`` extra: a recording
stub is injected as ``_model`` so we can assert exactly which ``initial_prompt``
the backend hands to ``WhisperModel.transcribe``. The wiring under test is that
``text.custom_vocabulary`` reaches transcription (not just cleanup).
"""

from __future__ import annotations

import types
from typing import Any

import numpy as np

from seda.config import TranscriptionConfig, load_config_from_dict
from seda.transcription.factory import create_backend
from seda.transcription.faster_whisper_backend import FasterWhisperBackend


class _RecordingModel:
    """A stand-in WhisperModel that records the kwargs it was called with."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    def transcribe(self, audio: np.ndarray, **kwargs: Any) -> tuple[list[Any], Any]:  # noqa: ARG002
        self.kwargs = kwargs
        segment = types.SimpleNamespace(text="hello")
        info = types.SimpleNamespace(language="en", language_probability=1.0, duration=1.0)
        return [segment], info


def _backend(
    config: TranscriptionConfig,
    vocab: list[str] | None,
    *,
    vad_filter: bool = False,
) -> FasterWhisperBackend:
    # device="cpu" avoids the CUDA probe; a recording stub replaces the real model.
    backend = FasterWhisperBackend(
        config, cuda_available=lambda: False, custom_vocabulary=vocab, vad_filter=vad_filter
    )
    backend._model = _RecordingModel()
    return backend


def test_custom_vocabulary_biases_the_initial_prompt() -> None:
    backend = _backend(TranscriptionConfig(device="cpu"), ["Kubernetes", "TypeScript"])
    result = backend.transcribe(np.zeros(1600, dtype=np.float32), 16000)

    model = backend._model
    assert isinstance(model, _RecordingModel)
    assert model.kwargs is not None
    assert model.kwargs["initial_prompt"] == "Vocabulary: Kubernetes, TypeScript."
    assert result.text == "hello"


def test_explicit_initial_prompt_overrides_vocabulary() -> None:
    config = TranscriptionConfig(device="cpu", initial_prompt="use this verbatim")
    backend = _backend(config, ["ignored"])
    backend.transcribe(np.zeros(1600, dtype=np.float32), 16000)

    model = backend._model
    assert isinstance(model, _RecordingModel)
    assert model.kwargs is not None
    assert model.kwargs["initial_prompt"] == "use this verbatim"


def test_no_vocabulary_sends_no_initial_prompt() -> None:
    backend = _backend(TranscriptionConfig(device="cpu"), [])
    backend.transcribe(np.zeros(1600, dtype=np.float32), 16000)

    model = backend._model
    assert isinstance(model, _RecordingModel)
    assert model.kwargs is not None
    # build_initial_prompt returns "" for no vocab; the backend maps that to None.
    assert model.kwargs["initial_prompt"] is None


def test_factory_plumbs_custom_vocabulary_into_backend() -> None:
    config = load_config_from_dict(
        {
            "transcription": {"backend": "faster-whisper", "device": "cpu"},
            "text": {"custom_vocabulary": ["Ollama", "CTranslate2"]},
        }
    )
    backend = create_backend(config)
    assert isinstance(backend, FasterWhisperBackend)
    assert backend._custom_vocabulary == ["Ollama", "CTranslate2"]


def test_vad_filter_passed_to_model_when_enabled() -> None:
    backend = _backend(TranscriptionConfig(device="cpu"), [], vad_filter=True)
    backend.transcribe(np.zeros(1600, dtype=np.float32), 16000)
    model = backend._model
    assert isinstance(model, _RecordingModel)
    assert model.kwargs is not None
    assert model.kwargs["vad_filter"] is True


def test_vad_filter_off_when_disabled() -> None:
    backend = _backend(TranscriptionConfig(device="cpu"), [])
    backend.transcribe(np.zeros(1600, dtype=np.float32), 16000)
    model = backend._model
    assert isinstance(model, _RecordingModel)
    assert model.kwargs is not None
    assert model.kwargs["vad_filter"] is False


def _fw_config(vad_backend: str) -> Any:
    return load_config_from_dict(
        {
            "transcription": {"backend": "faster-whisper", "device": "cpu"},
            "audio": {"vad_backend": vad_backend},
        }
    )


def test_factory_maps_silero_to_vad_filter_on() -> None:
    backend = create_backend(_fw_config("silero"))
    assert isinstance(backend, FasterWhisperBackend)
    assert backend._vad_filter is True


def test_factory_maps_none_and_energy_to_vad_filter_off() -> None:
    for value in ("none", "energy"):
        backend = create_backend(_fw_config(value))
        assert isinstance(backend, FasterWhisperBackend)
        assert backend._vad_filter is False, value


def test_default_config_enables_vad_filter() -> None:
    # audio.vad_backend now defaults to "silero" (#107), so the default filters.
    config = load_config_from_dict(
        {"transcription": {"backend": "faster-whisper", "device": "cpu"}}
    )
    backend = create_backend(config)
    assert isinstance(backend, FasterWhisperBackend)
    assert backend._vad_filter is True
