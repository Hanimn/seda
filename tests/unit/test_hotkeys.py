"""Unit tests for the hotkey provider (IMPLEMENTATION_PLAN.md §17)."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from seda.config import HotkeysConfig
from seda.input.hotkeys import (
    HotkeyProvider,
    PynputHotkeyProvider,
    key_to_token,
    serialize_chord,
)


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
    """Which key events belong to the chord and get gated (issues #11, #12)."""

    def test_chord_key_names_expands_modifier_aliases(self) -> None:
        from seda.input.hotkeys import _chord_key_names

        names = _chord_key_names("<ctrl>+<shift>+space")
        assert "ctrl" in names and "ctrl_l" in names and "ctrl_r" in names
        assert "shift" in names and "shift_l" in names and "shift_r" in names
        assert "space" in names

    def test_chord_modifier_names_excludes_trigger(self) -> None:
        from seda.input.hotkeys import _chord_modifier_names

        mods = _chord_modifier_names("<ctrl>+<shift>+space")
        assert "ctrl_l" in mods and "shift_r" in mods
        assert "space" not in mods


class TestChordSuppressor:
    """Only suppress chord/cancel keys while the chord is engaged (issues #12, #13)."""

    def _make(self) -> object:
        from seda.input.hotkeys import (
            _chord_key_names,
            _chord_modifier_names,
            _ChordSuppressor,
        )

        chord = "<ctrl>+<shift>+space"
        return _ChordSuppressor(
            _chord_key_names(chord),
            _chord_modifier_names(chord),
            _chord_key_names("<esc>"),
        )

    def test_bare_trigger_when_idle_passes_through(self) -> None:
        # The regression: space alone, no modifiers held, must NOT be suppressed.
        s = self._make()
        assert s.should_suppress("space", modifiers_held=False) is False

    def test_trigger_while_modifier_held_is_suppressed(self) -> None:
        s = self._make()
        assert s.should_suppress("space", modifiers_held=True) is True

    def test_chord_modifier_is_suppressed(self) -> None:
        s = self._make()
        # A chord modifier is how the chord is composed — always suppressed.
        assert s.should_suppress("ctrl_l", modifiers_held=True) is True
        assert s.should_suppress("shift", modifiers_held=False) is True

    def test_unrelated_key_never_suppressed(self) -> None:
        s = self._make()
        assert s.should_suppress("a", modifiers_held=True) is False
        assert s.should_suppress("enter", modifiers_held=True) is False
        assert s.should_suppress(None, modifiers_held=True) is False

    def test_bare_cancel_key_when_idle_passes_through(self) -> None:
        # Esc must still reach the focused app when not push-to-talking (issue #13):
        # suppressing it globally would break dialogs, vim, etc.
        s = self._make()
        assert s.should_suppress("esc", modifiers_held=False) is False

    def test_cancel_key_while_chord_engaged_is_suppressed(self) -> None:
        # While holding the chord (modifiers down), Esc cancels and must not leak.
        s = self._make()
        assert s.should_suppress("esc", modifiers_held=True) is True


class TestDarwinIntercept:
    """The macOS interceptor gates on identity + modifier state (#11, #12)."""

    def _patch_native(
        self, monkeypatch: pytest.MonkeyPatch, *, identity: object, modifiers_held: bool
    ) -> None:
        monkeypatch.setattr("seda.input.hotkeys._darwin_event_identity", lambda e: identity)
        monkeypatch.setattr(
            "seda.input.hotkeys._darwin_chord_modifiers_held",
            lambda e, m: modifiers_held,
        )

    def test_intercept_suppresses_trigger_during_hold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider, _, _ = _build_provider(monkeypatch)
        intercept = provider._make_darwin_intercept()
        self._patch_native(monkeypatch, identity="space", modifiers_held=True)
        assert intercept("keydown", object()) is None

    def test_intercept_passes_bare_trigger_when_idle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The #12 regression case: space with no chord modifier held.
        provider, _, _ = _build_provider(monkeypatch)
        intercept = provider._make_darwin_intercept()
        self._patch_native(monkeypatch, identity="space", modifiers_held=False)
        sentinel = object()
        assert intercept("keydown", sentinel) is sentinel

    def test_intercept_passes_unrelated_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider, _, _ = _build_provider(monkeypatch)
        intercept = provider._make_darwin_intercept()
        self._patch_native(monkeypatch, identity="a", modifiers_held=True)
        sentinel = object()
        assert intercept("keydown", sentinel) is sentinel

    def test_intercept_passes_event_when_identity_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider, _, _ = _build_provider(monkeypatch)
        intercept = provider._make_darwin_intercept()

        def _boom(_event: object) -> str:
            raise RuntimeError("Quartz unavailable")

        monkeypatch.setattr("seda.input.hotkeys._darwin_event_identity", _boom)
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


class TestSetPushToTalk:
    """set_push_to_talk swaps the chord IN PLACE — the #89 crash-guard invariant is
    that it NEVER rebuilds the listener (rebuilding re-enters Carbon TIS → crash)."""

    def _started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[PynputHotkeyProvider, object, object]:
        pynput_mod, listener_cls, global_hotkeys_cls = _make_pynput_mock()
        monkeypatch.setitem(sys.modules, "pynput", pynput_mod)
        monkeypatch.setitem(sys.modules, "pynput.keyboard", pynput_mod.keyboard)

        fake_ptt_listener = MagicMock()
        fake_ptt_listener.canonical.side_effect = lambda key: key
        listener_cls.return_value = fake_ptt_listener
        fake_cancel_listener = MagicMock()
        global_hotkeys_cls.return_value = fake_cancel_listener

        provider = PynputHotkeyProvider(HotkeysConfig(push_to_talk="<ctrl>+<shift>+space"))
        # Each HotKey(...) call returns a DISTINCT object so a rebuilt _ptt_hotkey
        # is observably different (the class mock otherwise shares one return_value).
        import pynput.keyboard as kb  # the mock

        kb.HotKey.side_effect = lambda *a, **k: MagicMock(name="HotKey")  # type: ignore[attr-defined]
        provider.start(on_press=lambda: None, on_release=lambda: None, on_cancel=lambda: None)
        return provider, fake_ptt_listener, fake_cancel_listener

    def test_swaps_chord_without_touching_the_listener(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider, ptt_listener, cancel_listener = self._started(monkeypatch)
        listener_before = provider._listener
        cancel_before = provider._cancel_listener
        hotkey_before = provider._ptt_hotkey

        provider.set_push_to_talk("<ctrl>+<alt>+m")

        # Chord-derived state updated...
        assert provider._ptt_key == "<ctrl>+<alt>+m"
        assert provider._ptt_trigger == "m"
        assert provider._ptt_hotkey is not hotkey_before  # a fresh HotKey object
        # ...but the LIVE listeners are the SAME objects and were NEVER stopped.
        assert provider._listener is listener_before
        assert provider._cancel_listener is cancel_before
        ptt_listener.stop.assert_not_called()
        cancel_listener.stop.assert_not_called()

    def test_invalid_chord_raises_and_leaves_state_intact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from seda.errors import HotkeyError

        provider, _, _ = self._started(monkeypatch)
        before_key = provider._ptt_key
        before_hotkey = provider._ptt_hotkey

        # Make HotKey.parse raise for the new chord (invalid), as pynput would.
        import pynput.keyboard as kb  # the mock

        kb.HotKey.parse.side_effect = ValueError("bad chord")  # type: ignore[attr-defined]
        try:
            with pytest.raises(HotkeyError):
                provider.set_push_to_talk("<not-a-key>")
        finally:
            kb.HotKey.parse.side_effect = None  # type: ignore[attr-defined]

        # Validated before mutating: the live chord is untouched.
        assert provider._ptt_key == before_key
        assert provider._ptt_hotkey is before_hotkey


# --- Seam 1 (#89): chord serializer — pynput key objects → config chord string ---


class _FakeNamedKey:
    """Stand-in for a pynput ``keyboard.Key`` (has ``.name``, no ``.char``)."""

    def __init__(self, name: str) -> None:
        self.name = name


class _FakeCharKey:
    """Stand-in for a pynput ``keyboard.KeyCode`` (has ``.char``, ``.name`` is None)."""

    def __init__(self, char: str) -> None:
        self.char = char
        self.name = None


class TestSerializeChord:
    """serialize_chord builds a canonical config chord from modifiers + trigger."""

    def test_canonical_modifier_order_regardless_of_input_order(self) -> None:
        # A set has no order; the output must be deterministic (ctrl, alt, shift, cmd).
        assert serialize_chord(frozenset({"shift", "ctrl"}), "space") == "<ctrl>+<shift>+space"
        assert serialize_chord(frozenset({"ctrl", "shift"}), "space") == "<ctrl>+<shift>+space"
        assert (
            serialize_chord(frozenset({"cmd", "alt", "ctrl", "shift"}), "m")
            == "<ctrl>+<alt>+<shift>+<cmd>+m"
        )

    def test_modifiers_are_bracketed_trigger_is_bare(self) -> None:
        # Named triggers (space, f5) stay bare — config format is <mod>+<mod>+trigger.
        assert serialize_chord(frozenset({"ctrl"}), "space") == "<ctrl>+space"
        assert serialize_chord(frozenset({"cmd"}), "f5") == "<cmd>+f5"

    def test_single_char_trigger_stays_bare(self) -> None:
        assert serialize_chord(frozenset({"cmd"}), "d") == "<cmd>+d"

    def test_no_modifiers_is_just_the_trigger(self) -> None:
        assert serialize_chord(frozenset(), "f5") == "f5"


class TestKeyToToken:
    """key_to_token maps a live pynput key to its bare config token."""

    def test_left_right_modifier_aliases_collapse_to_canonical(self) -> None:
        assert key_to_token(_FakeNamedKey("ctrl_l")) == "ctrl"
        assert key_to_token(_FakeNamedKey("ctrl_r")) == "ctrl"
        assert key_to_token(_FakeNamedKey("shift_r")) == "shift"
        assert key_to_token(_FakeNamedKey("alt_gr")) == "alt"
        assert key_to_token(_FakeNamedKey("cmd_l")) == "cmd"

    def test_plain_modifier_names_pass_through(self) -> None:
        assert key_to_token(_FakeNamedKey("ctrl")) == "ctrl"
        assert key_to_token(_FakeNamedKey("shift")) == "shift"

    def test_named_non_modifier_key_uses_its_name(self) -> None:
        assert key_to_token(_FakeNamedKey("space")) == "space"
        assert key_to_token(_FakeNamedKey("f5")) == "f5"

    def test_char_key_uses_its_char(self) -> None:
        assert key_to_token(_FakeCharKey("d")) == "d"

    def test_unencodable_key_returns_none(self) -> None:
        # A KeyCode with no char (e.g. a dead key) is not encodable.
        assert key_to_token(_FakeCharKey(char=None)) is None  # type: ignore[arg-type]

        class _Blank:
            name = None

        assert key_to_token(_Blank()) is None


class TestSerializeChordRoundTrip:
    """The whole point of the seam: serialize_chord output must parse under the
    SAME path the provider uses (_normalize_hotkey → keyboard.HotKey.parse), so a
    captured chord is always a chord the live listener can register."""

    @pytest.mark.parametrize(
        ("modifiers", "trigger"),
        [
            (frozenset({"ctrl", "shift"}), "space"),
            (frozenset({"cmd"}), "d"),
            (frozenset({"ctrl", "alt"}), "m"),
            (frozenset({"cmd", "shift"}), "f5"),
            (frozenset(), "f5"),
        ],
    )
    def test_output_parses_via_provider_path(self, modifiers: frozenset[str], trigger: str) -> None:
        keyboard = pytest.importorskip("pynput.keyboard")
        from seda.input.hotkeys import _normalize_hotkey

        chord = serialize_chord(modifiers, trigger)
        # Must not raise — this is exactly PynputHotkeyProvider.start()'s parse path.
        parsed = keyboard.HotKey.parse(_normalize_hotkey(chord))
        assert len(parsed) == len(modifiers) + 1
