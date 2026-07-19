"""Unit tests for audio/devices.py — device listing and resolution."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from seda.audio.devices import DeviceError, DeviceInfo, list_devices, resolve_device


def _make_raw(
    name: str,
    idx: int,
    input_channels: int = 2,
    samplerate: float = 44100.0,
) -> dict:
    return {
        "name": name,
        "max_input_channels": input_channels,
        "default_samplerate": samplerate,
    }


@pytest.fixture()
def mock_sd(monkeypatch):
    """Patch sounddevice with two input devices, first is the default."""
    sd = MagicMock()
    sd.query_devices.return_value = [
        _make_raw("Built-in Microphone", 0),
        _make_raw("USB Headset", 1),
        {"name": "Speakers Out", "max_input_channels": 0, "default_samplerate": 44100.0},
    ]
    sd.default.device = [0, 1]  # (input_idx, output_idx)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    yield sd


class TestListDevices:
    def test_filters_output_only_devices(self, mock_sd):
        devs = list_devices()
        names = [d.name for d in devs]
        assert "Speakers Out" not in names

    def test_returns_input_devices(self, mock_sd):
        devs = list_devices()
        assert len(devs) == 2
        assert devs[0].name == "Built-in Microphone"
        assert devs[1].name == "USB Headset"

    def test_default_flag(self, mock_sd):
        devs = list_devices()
        assert devs[0].is_default is True
        assert devs[1].is_default is False

    def test_single_device_dict(self, mock_sd):
        mock_sd.query_devices.return_value = _make_raw("Only Mic", 0)
        mock_sd.default.device = [0, 1]
        devs = list_devices()
        assert len(devs) == 1
        assert devs[0].name == "Only Mic"

    def test_sounddevice_unavailable(self, monkeypatch):
        sd_broken = MagicMock()
        sd_broken.query_devices.side_effect = OSError("no portaudio")
        monkeypatch.setitem(sys.modules, "sounddevice", sd_broken)
        from seda.errors import AudioError

        with pytest.raises(AudioError):
            list_devices()


class TestResolveDevice:
    def _devs(self):
        return [
            DeviceInfo(0, "Built-in Microphone", 2, 44100.0, True),
            DeviceInfo(1, "USB Headset", 2, 44100.0, False),
            DeviceInfo(2, "RODE NT-USB", 2, 48000.0, False),
        ]

    def test_none_returns_none(self):
        assert resolve_device(None) is None

    def test_by_index_int(self, monkeypatch):
        monkeypatch.setattr("seda.audio.devices.list_devices", self._devs)
        dev = resolve_device(1)
        assert dev is not None
        assert dev.name == "USB Headset"

    def test_by_index_string(self, monkeypatch):
        monkeypatch.setattr("seda.audio.devices.list_devices", self._devs)
        dev = resolve_device("2")
        assert dev is not None
        assert dev.name == "RODE NT-USB"

    def test_by_exact_name(self, monkeypatch):
        monkeypatch.setattr("seda.audio.devices.list_devices", self._devs)
        dev = resolve_device("USB Headset")
        assert dev is not None
        assert dev.index == 1

    def test_by_partial_name_unambiguous(self, monkeypatch):
        monkeypatch.setattr("seda.audio.devices.list_devices", self._devs)
        dev = resolve_device("RODE")
        assert dev is not None
        assert dev.name == "RODE NT-USB"

    def test_partial_name_ambiguous_two_matches(self, monkeypatch):
        devs = [
            DeviceInfo(0, "USB Mic A", 2, 44100.0, True),
            DeviceInfo(1, "USB Mic B", 2, 44100.0, False),
        ]
        monkeypatch.setattr("seda.audio.devices.list_devices", lambda: devs)
        with pytest.raises(DeviceError, match="ambiguous"):
            resolve_device("usb")

    def test_no_match_raises(self, monkeypatch):
        monkeypatch.setattr("seda.audio.devices.list_devices", self._devs)
        with pytest.raises(DeviceError, match="no input device matching"):
            resolve_device("Phantom Device")

    def test_missing_index_raises(self, monkeypatch):
        monkeypatch.setattr("seda.audio.devices.list_devices", self._devs)
        with pytest.raises(DeviceError, match="no input device with index"):
            resolve_device(99)
