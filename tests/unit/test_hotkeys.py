"""Unit tests for the hotkey provider (IMPLEMENTATION_PLAN.md §17)."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from local_flow.config import HotkeysConfig
from local_flow.input.hotkeys import HotkeyProvider, PynputHotkeyProvider


def _make_pynput_mock() -> tuple[types.ModuleType, MagicMock, MagicMock]:
    """Return (pynput module, Listener class mock, GlobalHotKeys class mock)."""
    keyboard_mod = types.ModuleType("pynput.keyboard")

    # Listener mock: needs a canonical() method so _on_key_press/_on_key_release work.
    listener_cls = MagicMock()
    fake_listener_instance = MagicMock()
    fake_listener_instance.canonical.side_effect = lambda key: key
    listener_cls.return_value = fake_listener_instance

    # HotKey mock: needs parse() classmethod and press()/release() instance methods.
    hotkey_cls = MagicMock()
    hotkey_cls.parse.return_value = []

    # GlobalHotKeys mock for cancel key.
    global_hotkeys_cls = MagicMock()

    keyboard_mod.Listener = listener_cls  # type: ignore[attr-defined]
    keyboard_mod.HotKey = hotkey_cls  # type: ignore[attr-defined]
    keyboard_mod.GlobalHotKeys = global_hotkeys_cls  # type: ignore[attr-defined]

    pynput_mod = types.ModuleType("pynput")
    pynput_mod.keyboard = keyboard_mod  # type: ignore[attr-defined]

    return pynput_mod, listener_cls, global_hotkeys_cls


def _build_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PynputHotkeyProvider, MagicMock, MagicMock]:
    pynput_mod, listener_cls, global_hotkeys_cls = _make_pynput_mock()
    monkeypatch.setitem(sys.modules, "pynput", pynput_mod)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", pynput_mod.keyboard)
    cfg = HotkeysConfig()
    provider = PynputHotkeyProvider(cfg)
    return provider, listener_cls, global_hotkeys_cls


class TestHotkeyProviderProtocol:
    """PynputHotkeyProvider satisfies the HotkeyProvider Protocol."""

    def test_implements_protocol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider, _, _ = _build_provider(monkeypatch)
        p: HotkeyProvider = provider
        assert hasattr(p, "start")
        assert hasattr(p, "stop")


class TestPynputHotkeyProviderDedup:
    """Auto-repeat and duplicate press events fire on_press exactly once."""

    def test_first_press_fires_callback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider, _, _ = _build_provider(monkeypatch)
        press_calls: list[None] = []
        provider.start(
            on_press=lambda: press_calls.append(None),
            on_release=lambda: None,
            on_cancel=lambda: None,
        )
        provider._on_ptt_press()
        assert len(press_calls) == 1

    def test_duplicate_press_does_not_fire_twice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider, _, _ = _build_provider(monkeypatch)
        press_calls: list[None] = []
        provider.start(
            on_press=lambda: press_calls.append(None),
            on_release=lambda: None,
            on_cancel=lambda: None,
        )
        provider._on_ptt_press()
        provider._on_ptt_press()  # OS auto-repeat / duplicate
        assert len(press_calls) == 1

    def test_release_resets_so_next_press_fires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider, _, _ = _build_provider(monkeypatch)
        press_calls: list[None] = []
        provider.start(
            on_press=lambda: press_calls.append(None),
            on_release=lambda: None,
            on_cancel=lambda: None,
        )
        provider._on_ptt_press()
        provider._on_ptt_release()
        provider._on_ptt_press()
        assert len(press_calls) == 2

    def test_release_fires_on_release_callback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider, _, _ = _build_provider(monkeypatch)
        release_calls: list[None] = []
        provider.start(
            on_press=lambda: None,
            on_release=lambda: release_calls.append(None),
            on_cancel=lambda: None,
        )
        provider._on_ptt_press()
        provider._on_ptt_release()
        assert len(release_calls) == 1

    def test_release_without_prior_press_does_not_fire(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider, _, _ = _build_provider(monkeypatch)
        release_calls: list[None] = []
        provider.start(
            on_press=lambda: None,
            on_release=lambda: release_calls.append(None),
            on_cancel=lambda: None,
        )
        provider._on_ptt_release()  # spurious release
        assert len(release_calls) == 0

    def test_cancel_fires_on_cancel_callback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider, _, _ = _build_provider(monkeypatch)
        cancel_calls: list[None] = []
        provider.start(
            on_press=lambda: None,
            on_release=lambda: None,
            on_cancel=lambda: cancel_calls.append(None),
        )
        provider._on_cancel()
        assert len(cancel_calls) == 1


class TestPynputHotkeyProviderStop:
    def test_stop_calls_underlying_stop_on_both_listeners(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pynput_mod, listener_cls, global_hotkeys_cls = _make_pynput_mock()
        monkeypatch.setitem(sys.modules, "pynput", pynput_mod)
        monkeypatch.setitem(sys.modules, "pynput.keyboard", pynput_mod.keyboard)

        fake_ptt_listener = MagicMock()
        fake_ptt_listener.canonical.side_effect = lambda key: key
        listener_cls.return_value = fake_ptt_listener

        fake_cancel_listener = MagicMock()
        global_hotkeys_cls.return_value = fake_cancel_listener

        cfg = HotkeysConfig()
        provider = PynputHotkeyProvider(cfg)
        provider.start(on_press=lambda: None, on_release=lambda: None, on_cancel=lambda: None)
        provider.stop()

        fake_ptt_listener.stop.assert_called_once()
        fake_cancel_listener.stop.assert_called_once()
