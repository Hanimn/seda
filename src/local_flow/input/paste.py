"""Text insertion via clipboard + simulated paste (IMPLEMENTATION_PLAN.md §16).

This module turns a finished transcript into text at the user's cursor without
ever pressing Enter.  The sequence (§16 "Required behavior") is:

1. Read and remember the current clipboard text.
2. Put the transcript on the clipboard.
3. Wait the configured propagation delay.
4. Send the configured paste shortcut.
5. Wait for the target application to consume the clipboard.
6. Restore the prior clipboard **only if the clipboard still holds the
   transcript** — the race check that stops us clobbering something the user
   copied while we worked.

Copy-only mode (a dedicated hotkey or ``--no-paste``) stops after step 2.  The
multiline policy (``preserve`` / ``flatten`` / ``copy_only``) decides whether
newlines survive and whether we paste at all.  On paste failure the transcript
is left on the clipboard and the prior value is *not* restored (§16 "Paste
failure"); we never retry with arbitrary keystrokes.

The orchestrator (:class:`TextInserter`) depends only on the
:class:`~local_flow.input.clipboard.ClipboardProvider` and :class:`PasteBackend`
protocols, so it is fully testable with in-memory fakes.  Transcript and
clipboard *contents* are never logged (§21).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from local_flow.errors import PasteError

if TYPE_CHECKING:
    from local_flow.config import PasteConfig
    from local_flow.input.clipboard import ClipboardProvider

__all__ = [
    "InsertionResult",
    "MultilinePolicy",
    "PasteBackend",
    "PasteError",
    "PynputPasteBackend",
    "TextInserter",
    "build_text_inserter",
    "select_shortcut",
]

MultilinePolicy = Literal["preserve", "flatten", "copy_only"]


def select_shortcut(
    config: PasteConfig,
    *,
    platform: str | None = None,
    active_app: str | None = None,
) -> str:
    """Return the paste shortcut for the current platform (§16).

    ``platform`` defaults to :data:`sys.platform`.  If ``active_app`` is given
    and matches a configured ``application_override`` entry (case-insensitive
    prefix match), that shortcut takes priority over the platform default.
    Application detection is best-effort and isolated: when the active
    application is unknown or there is no matching override, the platform
    default is used.  On Linux the GUI shortcut is used as the fallback;
    reliable terminal detection deferred to Phase 7 application overrides.
    """
    # Application-specific override takes priority (§16 "Application-specific
    # overrides"). Best-effort case-insensitive prefix match.
    if active_app and config.application_overrides:
        active_lower = active_app.lower()
        for override in config.application_overrides:
            if active_lower.startswith(override.application.lower()):
                return override.shortcut

    plat = platform if platform is not None else sys.platform
    if plat == "darwin":
        return config.shortcut_macos
    if plat.startswith("win"):
        return config.shortcut_windows
    # Linux and other POSIX desktops: use the GUI shortcut by default.
    return config.shortcut_linux_gui


def build_text_inserter(config: PasteConfig) -> TextInserter:
    """Construct a production :class:`TextInserter` from ``config`` (§16).

    Wires the real ``pyperclip`` clipboard and ``pynput`` paste backend, and
    selects the platform paste shortcut.  Deferred import of the clipboard
    provider keeps ``pyperclip`` off the ``--help`` / config-only import path.
    """
    from local_flow.input.clipboard import PyperclipClipboard

    return TextInserter(
        clipboard=PyperclipClipboard(),
        paste_backend=PynputPasteBackend(),
        shortcut=select_shortcut(config),
        restore_clipboard=config.restore_clipboard,
        multiline_policy=config.multiline_policy,
        append_space=config.append_space,
        paste_delay_ms=config.paste_delay_ms,
        restore_delay_ms=config.restore_delay_ms,
    )


class PasteBackend(Protocol):
    """Delivers the platform paste shortcut to the focused application."""

    def send_paste(self, shortcut: str) -> None:
        """Simulate pressing ``shortcut`` (e.g. ``"cmd+v"``).

        Raises :class:`PasteError` if the keystroke could not be delivered.
        """
        ...


@dataclass(frozen=True)
class InsertionResult:
    """Outcome of a single :meth:`TextInserter.insert` call.

    ``pasted`` — the paste shortcut was sent successfully.
    ``restored`` — the prior clipboard text was put back (race-safe).
    ``copied`` — the transcript was placed on the clipboard.
    ``error`` — a user-safe message when paste failed (never contains text).
    """

    copied: bool = False
    pasted: bool = False
    restored: bool = False
    error: str | None = None


class TextInserter:
    """Orchestrates clipboard save → paste → restore for one transcript."""

    def __init__(
        self,
        *,
        clipboard: ClipboardProvider,
        paste_backend: PasteBackend,
        shortcut: str,
        restore_clipboard: bool = True,
        multiline_policy: MultilinePolicy = "preserve",
        append_space: bool = False,
        paste_delay_ms: int = 100,
        restore_delay_ms: int = 750,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._clipboard = clipboard
        self._paste_backend = paste_backend
        self._shortcut = shortcut
        self._restore_clipboard = restore_clipboard
        self._multiline_policy = multiline_policy
        self._append_space = append_space
        self._paste_delay_ms = paste_delay_ms
        self._restore_delay_ms = restore_delay_ms
        self._sleep = sleep if sleep is not None else time.sleep

    def insert(self, text: str, *, copy_only: bool = False) -> InsertionResult:
        """Insert ``text`` at the cursor, or just copy it (``copy_only``).

        Returns an :class:`InsertionResult` describing what happened.  Never
        raises for an expected paste failure — the failure is reported in the
        result so the caller can notify the user and leave the transcript on
        the clipboard.
        """
        if not text:
            # Nothing to insert; leave the clipboard untouched.
            return InsertionResult()

        payload = self._apply_multiline_policy(text)
        if self._append_space:
            # Append a trailing space so consecutive dictations don't run
            # together (§16 "append_space").
            payload += " "
        # A "copy_only" multiline policy overrides an explicit paste request:
        # the safest thing for a multiline terminal paste is to not paste.
        want_paste = not copy_only and self._multiline_policy != "copy_only"

        # Step 1: remember the prior clipboard text (may be None if non-text).
        prior = self._clipboard.read_text()

        # Step 2: put the transcript on the clipboard.
        self._clipboard.write_text(payload)

        if not want_paste:
            # Copy-only: intentionally leave the transcript for the user; do not
            # restore the prior clipboard (there is nothing to paste into).
            return InsertionResult(copied=True, pasted=False, restored=False)

        # Steps 3–4: propagation delay, then paste.
        self._delay(self._paste_delay_ms)
        try:
            self._paste_backend.send_paste(self._shortcut)
        except PasteError as exc:
            # §16 "Paste failure": leave the transcript on the clipboard, do not
            # restore the prior value, do not retry with arbitrary keystrokes.
            return InsertionResult(
                copied=True,
                pasted=False,
                restored=False,
                error=str(exc),
            )

        # Step 5: let the target application consume the clipboard.
        self._delay(self._restore_delay_ms)

        # Step 6: restore the prior clipboard only when it is safe to do so.
        restored = self._maybe_restore(prior, payload)
        return InsertionResult(copied=True, pasted=True, restored=restored)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _maybe_restore(self, prior: str | None, payload: str) -> bool:
        """Restore ``prior`` iff restoration is on, prior was text, and the
        clipboard still holds the transcript we wrote (race-safe, §16 step 6)."""
        if not self._restore_clipboard:
            return False
        if prior is None:
            # The prior clipboard was non-text; we cannot faithfully restore it,
            # so we do not claim to (§16 "do not claim full restoration").
            return False
        if self._clipboard.read_text() != payload:
            # The user copied something new while we worked — never overwrite it.
            return False
        self._clipboard.write_text(prior)
        return True

    def _apply_multiline_policy(self, text: str) -> str:
        """Flatten newlines to spaces when the policy asks for it.

        ``preserve`` and ``copy_only`` keep the text verbatim; ``flatten``
        collapses every run of newline whitespace to a single space so a
        terminal cannot interpret embedded newlines as separate commands.
        """
        if self._multiline_policy != "flatten":
            return text
        # Replace CR/LF (and surrounding runs) with single spaces.
        flattened = text.replace("\r\n", "\n").replace("\r", "\n")
        parts = [line.strip() for line in flattened.split("\n")]
        return " ".join(part for part in parts if part)

    def _delay(self, milliseconds: int) -> None:
        if milliseconds > 0:
            self._sleep(milliseconds / 1000.0)


class PynputPasteBackend:
    """Production paste backend: taps the paste shortcut via ``pynput``.

    The shortcut string (e.g. ``"cmd+v"``, ``"ctrl+shift+v"``) is parsed into a
    set of modifier keys plus one main key.  Enter/Return are refused outright —
    this backend physically cannot press Enter, upholding the "never submit"
    guarantee (§3, §16).
    """

    # Recognised modifier tokens → pynput Key names.
    _MODIFIERS = {
        "cmd": "cmd",
        "command": "cmd",
        "super": "cmd",
        "win": "cmd",
        "ctrl": "ctrl",
        "control": "ctrl",
        "alt": "alt",
        "option": "alt",
        "shift": "shift",
    }
    _FORBIDDEN = frozenset({"enter", "return", "\n", "\r"})

    def __init__(self) -> None:
        self._controller: object | None = None

    def send_paste(self, shortcut: str) -> None:
        from pynput import keyboard

        modifiers, main_key = self._parse(shortcut)
        controller = self._get_controller()

        resolved_mods = [self._resolve_key(keyboard, m) for m in modifiers]
        resolved_main = self._resolve_key(keyboard, main_key)

        try:
            for mod in resolved_mods:
                controller.press(mod)  # type: ignore[attr-defined]
            controller.press(resolved_main)  # type: ignore[attr-defined]
            controller.release(resolved_main)  # type: ignore[attr-defined]
            for mod in reversed(resolved_mods):
                controller.release(mod)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - surfaced as a clean PasteError
            raise PasteError(f"could not deliver paste shortcut: {exc}") from exc

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_controller(self) -> object:
        if self._controller is None:
            from pynput import keyboard

            self._controller = keyboard.Controller()
        return self._controller

    def _parse(self, shortcut: str) -> tuple[list[str], str]:
        tokens = [t.strip().lower() for t in shortcut.split("+") if t.strip()]
        if not tokens:
            raise PasteError("empty paste shortcut")
        *mods, main = tokens
        if main in self._FORBIDDEN:
            raise PasteError("paste shortcut must never press Enter/Return")
        return mods, main

    def _resolve_key(self, keyboard: object, token: str) -> object:
        if token in self._MODIFIERS:
            name = self._MODIFIERS[token]
            return getattr(keyboard.Key, name)  # type: ignore[attr-defined]
        if len(token) == 1:
            return token
        # A named non-modifier key (rare for paste); look it up on Key.
        try:
            return getattr(keyboard.Key, token)  # type: ignore[attr-defined]
        except AttributeError as exc:
            raise PasteError(f"unknown key in paste shortcut: {token!r}") from exc
