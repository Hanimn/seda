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

from seda.input.clipboard import ClipboardProvider, FakeClipboard
from seda.input.paste import (
    InsertionResult,
    PasteError,
    PynputPasteBackend,
    TextInserter,
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
