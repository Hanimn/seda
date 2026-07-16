"""Unit tests for the hotkey provider (IMPLEMENTATION_PLAN.md §17)."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from typing import Any
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


class TestPynputHotkeyProviderReleaseKey:
    """Release must be tied to the PTT trigger key, not any key (issue #10).

    These drive the *real* ``_on_key_release(key)`` binding the production
    Listener uses, rather than calling ``_on_ptt_release()`` directly, so they
    catch release being wired to the wrong key.
    """

    def _start_and_capture_handlers(
        self,
        monkeypatch: pytest.MonkeyPatch,
        on_release: Callable[[], None],
        chord: str = "<ctrl>+<shift>+space",
    ) -> tuple[PynputHotkeyProvider, Callable[[Any], None], Callable[[Any], None]]:
        pynput_mod, listener_cls, _ = _make_pynput_mock()
        monkeypatch.setitem(sys.modules, "pynput", pynput_mod)
        monkeypatch.setitem(sys.modules, "pynput.keyboard", pynput_mod.keyboard)

        provider = PynputHotkeyProvider(HotkeysConfig(push_to_talk=chord))
        provider.start(on_press=lambda: None, on_release=on_release, on_cancel=lambda: None)

        # The production code builds the Listener with on_press/on_release
        # closures; capture them from the recorded constructor kwargs.
        _, kwargs = listener_cls.call_args
        return provider, kwargs["on_press"], kwargs["on_release"]

    def test_releasing_a_modifier_does_not_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        release_calls: list[None] = []
        provider, key_press, key_release = self._start_and_capture_handlers(
            monkeypatch, lambda: release_calls.append(None)
        )
        # Simulate the chord being held: mark provider as pressed.
        provider._on_ptt_press()
        assert release_calls == []

        # User lifts a MODIFIER first (ctrl), still meaning to record.
        key_release("<ctrl>")
        assert release_calls == [], "releasing a modifier must not stop recording"

    def test_releasing_the_trigger_key_stops(self, monkeypatch: pytest.MonkeyPatch) -> None:
        release_calls: list[None] = []
        provider, key_press, key_release = self._start_and_capture_handlers(
            monkeypatch, lambda: release_calls.append(None)
        )
        provider._on_ptt_press()

        # Releasing the trigger key (space) fires on_release exactly once.
        key_release("space")
        assert release_calls == [None]

    def test_releasing_unrelated_non_modifier_key_does_not_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A stray non-modifier key released mid-hold must NOT stop recording —
        # only the configured trigger key does (AC1: "a non-PTT key does NOT
        # stop"). Guards against the weaker "any non-modifier" heuristic.
        release_calls: list[None] = []
        provider, key_press, key_release = self._start_and_capture_handlers(
            monkeypatch, lambda: release_calls.append(None)
        )
        provider._on_ptt_press()

        key_release("a")  # not the trigger (space)
        assert release_calls == [], "an unrelated key must not stop recording"

    def test_bare_modifier_chord_can_be_stopped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # If the PTT chord is a single bare modifier, releasing it must still
        # fire on_release — otherwise recording is unstoppable (issue #10).
        release_calls: list[None] = []
        provider, key_press, key_release = self._start_and_capture_handlers(
            monkeypatch, lambda: release_calls.append(None), chord="<ctrl_r>"
        )
        provider._on_ptt_press()

        key_release("<ctrl_r>")
        assert release_calls == [None]


class TestKeySuppressionDecision:
    """Which key events get hidden from the focused app (issue #11)."""

    def test_chord_key_names_expands_modifier_aliases(self) -> None:
        from local_flow.input.hotkeys import _chord_key_names

        names = _chord_key_names("<ctrl>+<shift>+space")
        # Base modifiers expand to their left/right variants.
        assert "ctrl" in names and "ctrl_l" in names and "ctrl_r" in names
        assert "shift" in names and "shift_l" in names and "shift_r" in names
        assert "space" in names

    def test_suppress_chord_keys(self) -> None:
        from local_flow.input.hotkeys import _chord_key_names, _should_suppress_key

        chord = _chord_key_names("<ctrl>+<shift>+space")
        assert _should_suppress_key("space", chord) is True
        assert _should_suppress_key("ctrl_l", chord) is True  # live left-variant
        assert _should_suppress_key("shift", chord) is True

    def test_do_not_suppress_unrelated_keys(self) -> None:
        from local_flow.input.hotkeys import _chord_key_names, _should_suppress_key

        chord = _chord_key_names("<ctrl>+<shift>+space")
        # Ordinary typing must pass through untouched.
        assert _should_suppress_key("a", chord) is False
        assert _should_suppress_key("enter", chord) is False
        assert _should_suppress_key("cmd", chord) is False  # not in this chord

    def test_suppress_handles_keycode_char_and_key_name(self) -> None:
        from local_flow.input.hotkeys import _chord_key_names, _should_suppress_key

        chord = _chord_key_names("<ctrl>+m")  # trigger is a char key

        class _FakeKeyCode:
            name = None
            char = "m"

        class _FakeKey:
            name = "ctrl_r"
            char = None

        assert _should_suppress_key(_FakeKeyCode(), chord) is True
        assert _should_suppress_key(_FakeKey(), chord) is True

        class _OtherChar:
            name = None
            char = "z"

        assert _should_suppress_key(_OtherChar(), chord) is False


class TestDarwinIntercept:
    """The macOS interceptor swallows only chord keys, passes the rest (#11)."""

    def test_intercept_suppresses_chord_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider, _, _ = _build_provider(monkeypatch)
        intercept = provider._make_darwin_intercept()
        # Fake the native identity extraction: this event is the trigger 'space'.
        monkeypatch.setattr("local_flow.input.hotkeys._darwin_event_identity", lambda e: "space")
        # Returning None means the event is swallowed (not delivered to the app).
        assert intercept("keydown", object()) is None

    def test_intercept_passes_unrelated_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider, _, _ = _build_provider(monkeypatch)
        intercept = provider._make_darwin_intercept()
        monkeypatch.setattr("local_flow.input.hotkeys._darwin_event_identity", lambda e: "a")
        sentinel = object()
        # Ordinary typing passes through unchanged.
        assert intercept("keydown", sentinel) is sentinel

    def test_intercept_passes_event_when_identity_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider, _, _ = _build_provider(monkeypatch)
        intercept = provider._make_darwin_intercept()

        def _boom(_event: object) -> str:
            raise RuntimeError("Quartz unavailable")

        monkeypatch.setattr("local_flow.input.hotkeys._darwin_event_identity", _boom)
        sentinel = object()
        # Fail open: never swallow input we could not identify.
        assert intercept("keydown", sentinel) is sentinel


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
