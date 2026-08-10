"""Unit tests for clipboard and text-insertion (IMPLEMENTATION_PLAN.md §16).

All tests use an in-memory ``FakeClipboard`` and a ``FakePasteBackend`` — no
real clipboard, pynput, or focused application is required.  They cover the
required §16 sequence: save prior text, write transcript, paste, then restore
the prior text *only when the clipboard still holds the transcript* (race
safe).  Copy-only mode, the multiline policy, and paste-failure fallback are
exercised too.
"""

from __future__ import annotations

import pytest

from seda.config import load_config_from_dict
from seda.input.clipboard import ClipboardProvider, FakeClipboard
from seda.input.paste import (
    InsertionResult,
    PasteError,
    PynputPasteBackend,
    TextInserter,
    TypeTextInserter,
    _tap_shortcut,
    build_text_inserter,
    select_shortcut,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakePasteBackend:
    """Records paste calls; can be told to fail on the next paste."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.paste_calls: list[str] = []

    def send_paste(self, shortcut: str) -> None:
        self.paste_calls.append(shortcut)
        if self.fail:
            raise PasteError("simulated paste failure")


def _inserter(
    *,
    clipboard: FakeClipboard | None = None,
    paste_backend: FakePasteBackend | None = None,
    restore_clipboard: bool = True,
    multiline_policy: str = "preserve",
    shortcut: str = "cmd+v",
    append_space: bool = False,
    paste_delay_ms: int = 0,
    restore_delay_ms: int = 0,
) -> tuple[TextInserter, FakeClipboard, FakePasteBackend]:
    cb = clipboard or FakeClipboard()
    pb = paste_backend or FakePasteBackend()
    inserter = TextInserter(
        clipboard=cb,
        paste_backend=pb,
        shortcut=shortcut,
        restore_clipboard=restore_clipboard,
        multiline_policy=multiline_policy,  # type: ignore[arg-type]
        append_space=append_space,
        paste_delay_ms=paste_delay_ms,
        restore_delay_ms=restore_delay_ms,
        sleep=lambda _seconds: None,  # no real delays in tests
    )
    return inserter, cb, pb


class FakeTypeBackend:
    """Records typed text; can be told to fail on the next type_text call."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.typed: list[str] = []
        self.warmed = False

    def type_text(self, text: str) -> None:
        self.typed.append(text)
        if self.fail:
            raise PasteError("simulated type failure")

    def warm(self) -> None:
        self.warmed = True


def _type_inserter(
    *,
    multiline_policy: str = "preserve",
    append_space: bool = False,
    fail: bool = False,
) -> tuple[TypeTextInserter, FakeClipboard, FakeTypeBackend]:
    cb = FakeClipboard()
    tb = FakeTypeBackend(fail=fail)
    inserter = TypeTextInserter(
        clipboard=cb,
        type_backend=tb,
        multiline_policy=multiline_policy,  # type: ignore[arg-type]
        append_space=append_space,
    )
    return inserter, cb, tb


class TestTypeTextInserter:
    def test_types_without_touching_clipboard(self) -> None:
        inserter, cb, tb = _type_inserter()
        result = inserter.insert("hello world")
        assert tb.typed == ["hello world"]
        assert result.pasted is True
        assert result.copied is False
        assert result.error is None
        assert cb.read_text() == ""  # a successful type leaves the clipboard alone

    def test_empty_text_is_noop(self) -> None:
        inserter, cb, tb = _type_inserter()
        assert inserter.insert("") == InsertionResult()
        assert tb.typed == []
        assert cb.read_text() == ""

    def test_copy_only_copies_and_does_not_type(self) -> None:
        inserter, cb, tb = _type_inserter()
        result = inserter.insert("secret", copy_only=True)
        assert tb.typed == []
        assert cb.read_text() == "secret"
        assert result.copied is True
        assert result.pasted is False

    def test_copy_only_multiline_policy_does_not_type(self) -> None:
        inserter, cb, tb = _type_inserter(multiline_policy="copy_only")
        inserter.insert("do not type me")
        assert tb.typed == []
        assert cb.read_text() == "do not type me"

    def test_flatten_policy_flattens_before_typing(self) -> None:
        inserter, _cb, tb = _type_inserter(multiline_policy="flatten")
        inserter.insert("line one\nline two")
        assert tb.typed == ["line one line two"]

    def test_append_space(self) -> None:
        inserter, _cb, tb = _type_inserter(append_space=True)
        inserter.insert("hi")
        assert tb.typed == ["hi "]

    def test_type_failure_falls_back_to_clipboard(self) -> None:
        inserter, cb, tb = _type_inserter(fail=True)
        result = inserter.insert("boom")
        assert result.error is not None
        assert result.pasted is False
        assert result.copied is True
        assert cb.read_text() == "boom"  # left on the clipboard as a fallback

    def test_warm_delegates_to_backend(self) -> None:
        inserter, _cb, tb = _type_inserter()
        inserter.warm()
        assert tb.warmed is True


class _FakeController:
    """Records ``type()`` calls; optionally raises to simulate a failure."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.typed: list[str] = []

    def type(self, text: str) -> None:
        self.typed.append(text)
        if self.fail:
            raise RuntimeError("controller boom")


class TestPynputTypeText:
    """PynputPasteBackend.type_text with an injected controller (no real keys)."""

    def test_sanitizes_newlines_to_spaces(self) -> None:
        backend = PynputPasteBackend()
        ctrl = _FakeController()
        backend._controller = ctrl
        backend.type_text("line1\nline2\r\nline3\rline4")
        # Every newline variant becomes a single space — never an Enter keypress.
        assert ctrl.typed == ["line1 line2 line3 line4"]

    def test_empty_text_types_nothing(self) -> None:
        backend = PynputPasteBackend()
        ctrl = _FakeController()
        backend._controller = ctrl
        backend.type_text("")
        assert ctrl.typed == []

    def test_controller_failure_becomes_paste_error(self) -> None:
        backend = PynputPasteBackend()
        backend._controller = _FakeController(fail=True)
        with pytest.raises(PasteError):
            backend.type_text("hello")


class TestBuildTextInserterMethod:
    def test_method_type_builds_type_inserter(self) -> None:
        config = load_config_from_dict({"paste": {"method": "type"}})
        assert isinstance(build_text_inserter(config.paste), TypeTextInserter)

    def test_method_clipboard_builds_text_inserter(self) -> None:
        config = load_config_from_dict({"paste": {"method": "clipboard"}})
        assert isinstance(build_text_inserter(config.paste), TextInserter)


# ---------------------------------------------------------------------------
# FakeClipboard behaves like a text clipboard
# ---------------------------------------------------------------------------


class TestFakeClipboard:
    def test_write_then_read(self) -> None:
        cb = FakeClipboard()
        cb.write_text("hello")
        assert cb.read_text() == "hello"

    def test_empty_by_default(self) -> None:
        cb = FakeClipboard()
        assert cb.read_text() == ""

    def test_non_text_reads_as_none(self) -> None:
        # A non-text payload (image/file) cannot be represented as text.
        cb = FakeClipboard()
        cb.set_non_text()
        assert cb.read_text() is None

    def test_satisfies_protocol(self) -> None:
        cb: ClipboardProvider = FakeClipboard()
        assert cb.read_text() == ""


class TestPyperclipClipboard:
    def test_read_text_returns_none_on_backend_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A broken clipboard backend (no X, no xclip) degrades to 'no text'
        instead of crashing the pipeline (#125)."""
        import sys
        import types

        from seda.input.clipboard import PyperclipClipboard

        broken = types.SimpleNamespace(
            paste=lambda: (_ for _ in ()).throw(RuntimeError("no clipboard mechanism")),
            copy=lambda _text: None,
        )
        monkeypatch.setitem(sys.modules, "pyperclip", broken)
        assert PyperclipClipboard().read_text() is None


# ---------------------------------------------------------------------------
# Happy path: save → write → paste → restore
# ---------------------------------------------------------------------------


class TestPasteAndRestore:
    def test_transcript_written_and_pasted(self) -> None:
        inserter, cb, pb = _inserter()
        cb.write_text("prior clipboard")
        result = inserter.insert("dictated text")
        assert result.pasted is True
        assert pb.paste_calls == ["cmd+v"]

    def test_prior_clipboard_restored_when_unchanged(self) -> None:
        inserter, cb, pb = _inserter()
        cb.write_text("prior clipboard")
        inserter.insert("dictated text")
        # After paste, the clipboard still held the transcript, so the prior
        # text is restored (§16 step 6).
        assert cb.read_text() == "prior clipboard"

    def test_restore_skipped_when_disabled(self) -> None:
        inserter, cb, pb = _inserter(restore_clipboard=False)
        cb.write_text("prior clipboard")
        inserter.insert("dictated text")
        # Restoration disabled → transcript stays on the clipboard.
        assert cb.read_text() == "dictated text"

    def test_result_reports_restored(self) -> None:
        inserter, cb, _ = _inserter()
        cb.write_text("prior")
        result = inserter.insert("new text")
        assert result.restored is True


# ---------------------------------------------------------------------------
# Race safety: the user copied something during processing
# ---------------------------------------------------------------------------


class TestRaceSafety:
    def test_prior_not_restored_if_user_copied_during_paste(self) -> None:
        # Simulate the user copying new content *after* the transcript was put
        # on the clipboard: the paste backend mutates the clipboard.
        cb = FakeClipboard()
        cb.write_text("prior clipboard")

        class RacingPasteBackend(FakePasteBackend):
            def send_paste(self, shortcut: str) -> None:
                super().send_paste(shortcut)
                cb.write_text("user copied this mid-flight")

        inserter, _, _ = _inserter(clipboard=cb, paste_backend=RacingPasteBackend())
        result = inserter.insert("dictated text")
        # The clipboard no longer holds the transcript → do NOT overwrite the
        # user's fresh copy with the stale prior value.
        assert cb.read_text() == "user copied this mid-flight"
        assert result.restored is False

    def test_non_text_prior_clipboard_not_falsely_restored(self) -> None:
        # If the prior clipboard was non-text (image/file), we cannot claim to
        # restore it — leave the transcript rather than clobbering with "".
        cb = FakeClipboard()
        cb.set_non_text()
        inserter, _, _ = _inserter(clipboard=cb)
        result = inserter.insert("dictated text")
        assert result.restored is False
        # Transcript remains on the clipboard (we did not restore a fake empty).
        assert cb.read_text() == "dictated text"


# ---------------------------------------------------------------------------
# Copy-only mode
# ---------------------------------------------------------------------------


class TestCopyOnly:
    def test_copy_only_does_not_paste(self) -> None:
        inserter, cb, pb = _inserter()
        result = inserter.insert("dictated text", copy_only=True)
        assert pb.paste_calls == []
        assert result.pasted is False
        assert cb.read_text() == "dictated text"

    def test_copy_only_does_not_restore(self) -> None:
        inserter, cb, _ = _inserter()
        cb.write_text("prior")
        inserter.insert("dictated text", copy_only=True)
        # In copy-only mode the transcript is intentionally left for the user.
        assert cb.read_text() == "dictated text"

    def test_multiline_policy_copy_only_forces_copy(self) -> None:
        inserter, cb, pb = _inserter(multiline_policy="copy_only")
        result = inserter.insert("line one\nline two")
        assert pb.paste_calls == []
        assert result.pasted is False


# ---------------------------------------------------------------------------
# Multiline policy
# ---------------------------------------------------------------------------


class TestMultilinePolicy:
    def test_preserve_keeps_newlines(self) -> None:
        # With preserve + no restore, the value left on the clipboard keeps its
        # newline exactly.
        cb = FakeClipboard()
        inserter, _, _ = _inserter(
            clipboard=cb, multiline_policy="preserve", restore_clipboard=False
        )
        inserter.insert("line one\nline two")
        assert cb.read_text() == "line one\nline two"

    def test_flatten_converts_newlines_to_spaces(self) -> None:
        # With flatten, the value placed on the clipboard has no newlines.
        cb = FakeClipboard()
        inserter, _, pb = _inserter(
            clipboard=cb, multiline_policy="flatten", restore_clipboard=False
        )
        inserter.insert("line one\nline two")
        assert cb.read_text() == "line one line two"

    def test_never_sends_enter(self) -> None:
        # The paste backend is only ever asked to paste — never to press Enter.
        inserter, _, pb = _inserter()
        inserter.insert("some text")
        for call in pb.paste_calls:
            assert "enter" not in call.lower()
            assert "return" not in call.lower()


# ---------------------------------------------------------------------------
# append_space
# ---------------------------------------------------------------------------


class TestAppendSpace:
    def test_trailing_space_appended_when_enabled(self) -> None:
        cb = FakeClipboard()
        inserter, _, _ = _inserter(clipboard=cb, append_space=True, restore_clipboard=False)
        inserter.insert("hello")
        assert cb.read_text() == "hello "

    def test_no_trailing_space_by_default(self) -> None:
        cb = FakeClipboard()
        inserter, _, _ = _inserter(clipboard=cb, restore_clipboard=False)
        inserter.insert("hello")
        assert cb.read_text() == "hello"

    def test_append_space_after_flatten(self) -> None:
        cb = FakeClipboard()
        inserter, _, _ = _inserter(
            clipboard=cb,
            append_space=True,
            multiline_policy="flatten",
            restore_clipboard=False,
        )
        inserter.insert("line one\nline two")
        assert cb.read_text() == "line one line two "


# ---------------------------------------------------------------------------
# Paste failure fallback
# ---------------------------------------------------------------------------


class TestPasteFailure:
    def test_paste_failure_leaves_transcript_on_clipboard(self) -> None:
        cb = FakeClipboard()
        cb.write_text("prior clipboard")
        inserter, _, _ = _inserter(clipboard=cb, paste_backend=FakePasteBackend(fail=True))
        result = inserter.insert("dictated text")
        assert result.pasted is False
        # Failure fallback: transcript stays on clipboard, prior NOT restored.
        assert cb.read_text() == "dictated text"
        assert result.restored is False

    def test_paste_failure_reports_error(self) -> None:
        inserter, _, _ = _inserter(paste_backend=FakePasteBackend(fail=True))
        result = inserter.insert("dictated text")
        assert result.pasted is False
        assert result.error is not None


# ---------------------------------------------------------------------------
# InsertionResult metadata
# ---------------------------------------------------------------------------


class TestInsertionResult:
    def test_returns_insertion_result(self) -> None:
        inserter, _, _ = _inserter()
        result = inserter.insert("text")
        assert isinstance(result, InsertionResult)

    def test_empty_text_is_noop(self) -> None:
        inserter, cb, pb = _inserter()
        cb.write_text("prior")
        result = inserter.insert("")
        assert result.pasted is False
        assert pb.paste_calls == []
        # Prior clipboard untouched.
        assert cb.read_text() == "prior"


# ---------------------------------------------------------------------------
# PynputPasteBackend: parsing + Enter-refusal (no real keystrokes sent)
# ---------------------------------------------------------------------------


class TestPynputPasteBackendParsing:
    def test_refuses_enter_shortcut(self) -> None:
        backend = PynputPasteBackend()
        with pytest.raises(PasteError, match="Enter"):
            backend.send_paste("enter")

    def test_refuses_enter_even_when_pynput_cannot_import(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SAFETY: the Enter/Return refusal must hold even on a headless system
        # where pynput cannot be imported (no X display). Validation runs before
        # the pynput import, so a PasteError is raised, not an ImportError (#14).
        import sys

        monkeypatch.setitem(sys.modules, "pynput", None)
        backend = PynputPasteBackend()
        with pytest.raises(PasteError, match="Enter"):
            backend.send_paste("ctrl+return")

    def test_refuses_return_as_main_key(self) -> None:
        backend = PynputPasteBackend()
        with pytest.raises(PasteError, match="Enter"):
            backend.send_paste("ctrl+return")

    def test_empty_shortcut_rejected(self) -> None:
        backend = PynputPasteBackend()
        with pytest.raises(PasteError, match="empty"):
            backend.send_paste("")

    @pytest.mark.parametrize(
        "shortcut, expected_mods, expected_main",
        [
            ("cmd+v", ["cmd"], "v"),
            ("ctrl+v", ["ctrl"], "v"),
            ("ctrl+shift+v", ["ctrl", "shift"], "v"),
        ],
    )
    def test_parse_splits_modifiers_and_main_key(
        self, shortcut: str, expected_mods: list[str], expected_main: str
    ) -> None:
        backend = PynputPasteBackend()
        mods, main = backend._parse(shortcut)
        assert mods == expected_mods
        assert main == expected_main


# ---------------------------------------------------------------------------
# Platform shortcut selection
# ---------------------------------------------------------------------------


class TestSelectShortcut:
    def test_macos_uses_cmd_v(self) -> None:
        from seda.config import PasteConfig

        assert select_shortcut(PasteConfig(), platform="darwin") == "cmd+v"

    def test_windows_uses_ctrl_v(self) -> None:
        from seda.config import PasteConfig

        assert select_shortcut(PasteConfig(), platform="win32") == "ctrl+v"

    def test_linux_gui_uses_ctrl_v(self) -> None:
        from seda.config import PasteConfig

        assert select_shortcut(PasteConfig(), platform="linux") == "ctrl+v"

    def test_custom_shortcut_respected(self) -> None:
        from seda.config import PasteConfig

        cfg = PasteConfig(shortcut_macos="cmd+shift+v")
        assert select_shortcut(cfg, platform="darwin") == "cmd+shift+v"

    def test_application_override_wins_over_platform_default(self) -> None:
        from seda.config import ApplicationOverride, PasteConfig

        cfg = PasteConfig(
            application_overrides=[
                ApplicationOverride(application="iTerm2", shortcut="cmd+v"),
                ApplicationOverride(application="Windows Terminal", shortcut="ctrl+shift+v"),
            ]
        )
        assert select_shortcut(cfg, platform="darwin", active_app="iTerm2") == "cmd+v"
        assert (
            select_shortcut(cfg, platform="win32", active_app="Windows Terminal") == "ctrl+shift+v"
        )

    def test_no_match_falls_back_to_platform(self) -> None:
        from seda.config import ApplicationOverride, PasteConfig

        cfg = PasteConfig(
            application_overrides=[
                ApplicationOverride(application="iTerm2", shortcut="ctrl+v"),
            ]
        )
        # "Code" doesn't match — falls back to platform default.
        assert select_shortcut(cfg, platform="darwin", active_app="Code") == "cmd+v"

    def test_unknown_active_app_falls_back_to_platform(self) -> None:
        from seda.config import PasteConfig

        assert select_shortcut(PasteConfig(), platform="darwin", active_app=None) == "cmd+v"


class TestPasteWarm:
    """warm() pre-builds the backend's platform machinery on the caller's thread,
    so its macOS Carbon TIS init doesn't run lazily on the worker thread at first
    paste (which crashes, #89)."""

    def test_text_inserter_warm_delegates_to_backend(self) -> None:
        warmed: list[str] = []

        class _WarmBackend(FakePasteBackend):
            def warm(self) -> None:
                warmed.append("warmed")

        inserter, _, _ = _inserter(paste_backend=_WarmBackend())
        inserter.warm()
        assert warmed == ["warmed"]

    def test_text_inserter_warm_noop_when_backend_has_no_warm(self) -> None:
        # FakePasteBackend has no warm(); TextInserter.warm must be a safe no-op.
        inserter, _, _ = _inserter(paste_backend=FakePasteBackend())
        inserter.warm()  # must not raise

    def test_pynput_backend_warm_builds_controller_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys
        import types
        from unittest.mock import MagicMock

        keyboard_mod = types.ModuleType("pynput.keyboard")
        controller_cls = MagicMock(name="Controller")
        keyboard_mod.Controller = controller_cls  # type: ignore[attr-defined]
        pynput_mod = types.ModuleType("pynput")
        pynput_mod.keyboard = keyboard_mod  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pynput", pynput_mod)
        monkeypatch.setitem(sys.modules, "pynput.keyboard", keyboard_mod)

        backend = PynputPasteBackend()
        assert backend._controller is None
        backend.warm()
        # Built exactly once, and cached so a later paste reuses it.
        controller_cls.assert_called_once()
        cached = backend._controller
        backend.warm()  # idempotent — no second construction
        controller_cls.assert_called_once()
        assert backend._controller is cached

    def test_pynput_backend_warm_is_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        # pynput unavailable (headless): warm must swallow the failure, not raise.
        monkeypatch.setitem(sys.modules, "pynput", None)
        backend = PynputPasteBackend()
        backend.warm()  # must not raise
        assert backend._controller is None


# ---------------------------------------------------------------------------
# _tap_shortcut — the modifier-release guarantee (stuck-modifier guard)
# ---------------------------------------------------------------------------


class _RecordingController:
    """Records press/release calls; optionally raises on a chosen (op, key)."""

    def __init__(self, *, fail_at: tuple[str, object] | None = None) -> None:
        self.fail_at = fail_at
        self.events: list[tuple[str, object]] = []

    def press(self, key: object) -> None:
        self.events.append(("press", key))
        if self.fail_at == ("press", key):
            raise RuntimeError("press boom")

    def release(self, key: object) -> None:
        self.events.append(("release", key))
        if self.fail_at == ("release", key):
            raise RuntimeError("release boom")


class TestTapShortcut:
    def test_happy_path_presses_and_releases_in_order(self) -> None:
        ctrl = _RecordingController()
        _tap_shortcut(ctrl, ["CTRL", "SHIFT"], "V")
        assert ctrl.events == [
            ("press", "CTRL"),
            ("press", "SHIFT"),
            ("press", "V"),
            ("release", "V"),
            ("release", "SHIFT"),
            ("release", "CTRL"),
        ]

    def test_modifiers_released_when_main_key_press_fails(self) -> None:
        ctrl = _RecordingController(fail_at=("press", "V"))
        with pytest.raises(RuntimeError, match="press boom"):
            _tap_shortcut(ctrl, ["CTRL", "SHIFT"], "V")
        # Both already-pressed modifiers are released despite the failure.
        assert ("release", "CTRL") in ctrl.events
        assert ("release", "SHIFT") in ctrl.events

    def test_modifier_released_when_a_later_modifier_press_fails(self) -> None:
        ctrl = _RecordingController(fail_at=("press", "SHIFT"))
        with pytest.raises(RuntimeError, match="press boom"):
            _tap_shortcut(ctrl, ["CTRL", "SHIFT"], "V")
        assert ctrl.events == [
            ("press", "CTRL"),
            ("press", "SHIFT"),
            ("release", "CTRL"),
        ]

    def test_main_key_released_when_its_own_release_fails(self) -> None:
        ctrl = _RecordingController(fail_at=("release", "V"))
        with pytest.raises(RuntimeError, match="release boom"):
            _tap_shortcut(ctrl, ["CTRL"], "V")
        # Cleanup still unwinds everything it pressed (release errors in the
        # unwind itself are swallowed so the original failure wins).
        assert ("release", "CTRL") in ctrl.events


# ---------------------------------------------------------------------------
# Non-text clipboard preservation via the optional snapshot protocol (#147)
# ---------------------------------------------------------------------------


class _FakeSnapshotClipboard(FakeClipboard):
    """FakeClipboard + the optional snapshot/restore capability (#147)."""

    def __init__(self, initial: str = "") -> None:
        super().__init__(initial)
        self.snapshots: list[object] = []
        self.restored: list[object] = []

    def snapshot(self) -> object:
        snap = ("snap", self._text)
        self.snapshots.append(snap)
        return snap

    def restore(self, snapshot: object) -> None:
        self.restored.append(snapshot)
        self._text = snapshot[1]  # type: ignore[index]


class TestNonTextRestore:
    def _inserter(self, cb: ClipboardProvider) -> TextInserter:
        return TextInserter(
            clipboard=cb,
            paste_backend=FakePasteBackend(),
            shortcut="cmd+v",
            sleep=lambda _s: None,
        )

    def test_non_text_prior_restored_via_snapshot(self) -> None:
        cb = _FakeSnapshotClipboard()
        cb.set_non_text()  # prior clipboard holds an image
        result = self._inserter(cb).insert("dictated text")
        assert result.pasted and result.restored
        assert len(cb.restored) == 1  # the snapshot was put back

    def test_text_prior_uses_text_path_not_snapshot(self) -> None:
        cb = _FakeSnapshotClipboard("prior text")
        result = self._inserter(cb).insert("dictated text")
        assert result.restored
        assert cb.restored == []  # snapshot restore NOT used
        assert cb.read_text() == "prior text"

    def test_snapshot_restore_skipped_when_race_detected(self) -> None:
        cb = _FakeSnapshotClipboard()
        cb.set_non_text()
        inserter = self._inserter(cb)
        original_write = cb.write_text

        def racing_write(text: str) -> None:
            original_write(text)
            cb._text = "user copied something else"

        cb.write_text = racing_write  # type: ignore[method-assign]
        result = inserter.insert("dictated text")
        assert result.pasted and not result.restored
        assert cb.restored == []  # never clobber the user's new clipboard

    def test_snapshot_restore_skipped_when_disabled(self) -> None:
        cb = _FakeSnapshotClipboard()
        cb.set_non_text()
        inserter = TextInserter(
            clipboard=cb,
            paste_backend=FakePasteBackend(),
            shortcut="cmd+v",
            restore_clipboard=False,
            sleep=lambda _s: None,
        )
        result = inserter.insert("dictated text")
        assert result.pasted and not result.restored
        assert cb.restored == []


# ---------------------------------------------------------------------------
# MacOSNativeClipboard — NSPasteboard provider with a faked pasteboard (#147)
# ---------------------------------------------------------------------------


class _FakePasteboardItem:
    def __init__(self, pairs: list[tuple[str, bytes]]) -> None:
        self._pairs = dict(pairs)
        self.set_calls: list[tuple[str, object]] = []

    def types(self) -> list[str]:
        return list(self._pairs)

    def dataForType_(self, uti: str) -> bytes | None:
        return self._pairs.get(uti)

    def setData_forType_(self, data: object, uti: str) -> None:
        self.set_calls.append((uti, data))


class _FakePasteboard:
    """Records NSPasteboard calls; serves canned items."""

    def __init__(
        self, *, text: str | None = None, items: list[_FakePasteboardItem] | None = None
    ) -> None:
        self._text = text
        self._items = items or []
        self.cleared = 0
        self.written_text: list[str] = []
        self.written_objects: list[list[object]] = []

    def stringForType_(self, _uti: str) -> str | None:
        return self._text

    def clearContents(self) -> None:
        self.cleared += 1
        self._text = None
        self._items = []

    def setString_forType_(self, text: str, _uti: str) -> None:
        self.written_text.append(text)
        self._text = text

    def pasteboardItems(self) -> list[_FakePasteboardItem]:
        return self._items

    def writeObjects_(self, objects: list[object]) -> None:
        self.written_objects.append(objects)


class TestMacOSNativeClipboard:
    def _provider(self, pb: _FakePasteboard) -> object:
        from seda.input.pasteboard_macos import MacOSNativeClipboard

        provider = MacOSNativeClipboard(pasteboard=pb)
        # Patch the AppKit construction boundaries.
        provider._new_pasteboard_item = lambda: _FakePasteboardItem([])  # type: ignore[method-assign]
        provider._to_data = lambda raw: ("data", raw)  # type: ignore[method-assign]
        return provider

    def test_read_text(self) -> None:
        pb = _FakePasteboard(text="hello")
        assert self._provider(pb).read_text() == "hello"  # type: ignore[attr-defined]

    def test_read_text_non_text_returns_none(self) -> None:
        pb = _FakePasteboard(text=None)
        assert self._provider(pb).read_text() is None  # type: ignore[attr-defined]

    def test_write_text_clears_then_sets(self) -> None:
        pb = _FakePasteboard()
        self._provider(pb).write_text("dictated")  # type: ignore[attr-defined]
        assert pb.cleared == 1
        assert pb.written_text == ["dictated"]

    def test_snapshot_captures_all_items_and_types(self) -> None:
        item = _FakePasteboardItem([("public.png", b"\x89PNG"), ("public.utf8-plain-text", b"hi")])
        pb = _FakePasteboard(items=[item])
        snap = self._provider(pb).snapshot()  # type: ignore[attr-defined]
        assert len(snap.items) == 1
        assert ("public.png", b"\x89PNG") in snap.items[0]

    def test_restore_writes_items_back(self) -> None:
        from seda.input.pasteboard_macos import PasteboardSnapshot

        pb = _FakePasteboard()
        provider = self._provider(pb)
        snap = PasteboardSnapshot(items=((("public.png", b"\x89PNG"),),))
        provider.restore(snap)  # type: ignore[attr-defined]
        assert pb.cleared == 1
        assert len(pb.written_objects) == 1
        restored_item = pb.written_objects[0][0]
        assert restored_item.set_calls == [("public.png", ("data", b"\x89PNG"))]


class TestDefaultClipboardSelection:
    def test_macos_selects_native_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys as _sys

        from seda.input import paste as paste_module
        from seda.input.pasteboard_macos import MacOSNativeClipboard

        monkeypatch.setattr(_sys, "platform", "darwin")
        assert isinstance(paste_module._default_clipboard(), MacOSNativeClipboard)

    def test_macos_falls_back_when_native_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys as _sys

        from seda.input import paste as paste_module
        from seda.input.clipboard import PyperclipClipboard

        monkeypatch.setattr(_sys, "platform", "darwin")
        monkeypatch.setitem(_sys.modules, "seda.input.pasteboard_macos", None)
        assert isinstance(paste_module._default_clipboard(), PyperclipClipboard)

    def test_linux_selects_pyperclip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys as _sys

        from seda.input import paste as paste_module
        from seda.input.clipboard import PyperclipClipboard

        monkeypatch.setattr(_sys, "platform", "linux")
        assert isinstance(paste_module._default_clipboard(), PyperclipClipboard)
