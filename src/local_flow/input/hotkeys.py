"""Global hotkey provider (IMPLEMENTATION_PLAN.md §17).

The ``HotkeyProvider`` Protocol describes the boundary between the
application controller and the platform hotkey library, so the controller
can be tested without importing pynput.

``PynputHotkeyProvider`` is the production implementation.  It wires:
- A ``pynput.keyboard.Listener`` + ``pynput.keyboard.HotKey`` for the
  push-to-talk key, so we receive both key-press and key-release events.
- A ``pynput.keyboard.GlobalHotKeys`` for the cancel key (key-down only).

An internal ``_pressed`` flag (under a lock) ensures OS key-repeat events
and duplicate press notifications only ever fire ``on_press`` once per
physical key-down, and spurious releases are silently dropped.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from typing import Any, Protocol

from local_flow.config import HotkeysConfig, select_push_to_talk
from local_flow.errors import HotkeyError


class HotkeyProvider(Protocol):
    """Minimal interface for global hotkey listeners."""

    def start(
        self,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_cancel: Callable[[], None],
    ) -> None: ...

    def stop(self) -> None: ...


def _normalize_hotkey(s: str) -> str:
    """Wrap bare named keys (e.g. ``space``) with ``<>`` for pynput parsing.

    pynput's ``HotKey.parse`` requires all non-character tokens to be
    bracketed.  The config format uses ``<ctrl>+<alt>+space`` (bare
    ``space``), so we normalise before passing to pynput.
    """
    parts = s.split("+")
    normalized = []
    for part in parts:
        part = part.strip()
        if part.startswith("<") and part.endswith(">") or len(part) == 1:
            normalized.append(part)
        else:
            normalized.append(f"<{part}>")
    return "+".join(normalized)


# Modifier key names (bare, without brackets). The push-to-talk *trigger* is
# the one chord token that is NOT one of these — see :func:`_trigger_token`.
_MODIFIER_NAMES: frozenset[str] = frozenset(
    {
        "ctrl",
        "ctrl_l",
        "ctrl_r",
        "shift",
        "shift_l",
        "shift_r",
        "alt",
        "alt_l",
        "alt_r",
        "alt_gr",
        "cmd",
        "cmd_l",
        "cmd_r",
    }
)


def _trigger_token(hotkey: str) -> str:
    """Return the non-modifier *trigger* token of a chord, bare (no ``<>``).

    Given ``"<ctrl>+<shift>+space"`` returns ``"space"``; given ``"<f5>"``
    returns ``"f5"``; given ``"m"`` returns ``"m"``. If the chord is *only*
    modifiers (unusual), the last token is used so a bare-modifier PTT can
    still be released (issue #10).
    """
    tokens = [t.strip().strip("<>") for t in hotkey.split("+") if t.strip()]
    non_modifiers = [t for t in tokens if t not in _MODIFIER_NAMES]
    if non_modifiers:
        return non_modifiers[-1]
    return tokens[-1] if tokens else ""


def _released_key_matches(key: Any, trigger: str) -> bool:
    """Whether the released *key* is the push-to-talk *trigger* key.

    Handles both the live path (a ``pynput.keyboard.Key`` with a ``.name`` such
    as ``"space"`` / ``"f5"``, or a ``KeyCode`` with a ``.char`` like ``"m"``)
    and the test path (a bare/bracketed string such as ``"space"`` or
    ``"<space>"``). Matching by name-or-char sidesteps the fact that
    ``HotKey.parse`` and live events represent named keys differently.
    """
    name = getattr(key, "name", None)
    if isinstance(name, str):
        return name == trigger
    char = getattr(key, "char", None)
    if isinstance(char, str):
        return char == trigger
    if isinstance(key, str):
        return key.strip("<>") == trigger
    return False


class PynputHotkeyProvider:
    """Push-to-talk hotkey listener backed by ``pynput``.

    Uses a ``keyboard.Listener`` + ``keyboard.HotKey`` for the PTT key so
    we receive both press and release events.  Uses a ``GlobalHotKeys`` for
    the cancel key (key-down only).

    Only the first press event in each press-release cycle fires
    ``on_press``; OS auto-repeat and duplicate press events are dropped.
    A spurious release (release without a preceding press) is similarly
    dropped.
    """

    def __init__(self, config: HotkeysConfig) -> None:
        # Resolve the platform-appropriate push-to-talk chord (issue #9).
        self._ptt_key = select_push_to_talk(config)
        # The non-modifier trigger key whose release ends a hold (issue #10).
        self._ptt_trigger = _trigger_token(self._ptt_key)
        self._cancel_key = config.cancel
        self._on_press_cb: Callable[[], None] = lambda: None
        self._on_release_cb: Callable[[], None] = lambda: None
        self._on_cancel_cb: Callable[[], None] = lambda: None
        self._pressed = False
        self._lock = threading.Lock()
        self._listener: Any = None
        self._cancel_listener: Any = None

    # ------------------------------------------------------------------
    # HotkeyProvider interface
    # ------------------------------------------------------------------

    def start(
        self,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_cancel: Callable[[], None],
    ) -> None:
        """Register hotkeys and start listening.

        Raises :exc:`HotkeyError` if pynput fails to register the hotkeys.
        """
        self._on_press_cb = on_press
        self._on_release_cb = on_release
        self._on_cancel_cb = on_cancel

        try:
            from pynput import keyboard
        except (ImportError, OSError) as exc:
            raise HotkeyError(f"pynput is not available: {exc}") from exc

        try:
            ptt_normalized = _normalize_hotkey(self._ptt_key)
            ptt_hotkey = keyboard.HotKey(
                keyboard.HotKey.parse(ptt_normalized),
                self._on_ptt_press,
            )

            # keyboard.Listener fires on every key event; we feed canonical
            # key objects into the HotKey so it can track modifier state.
            def _on_key_press(key: Any) -> None:
                with contextlib.suppress(Exception):
                    ptt_hotkey.press(listener.canonical(key))

            def _on_key_release(key: Any) -> None:
                with contextlib.suppress(Exception):
                    ptt_hotkey.release(listener.canonical(key))
                # Only releasing the trigger key ends a hold. Releasing a
                # modifier (or any other key) while the chord is held must NOT
                # stop the recording — otherwise multi-key chords stop almost
                # immediately after starting and capture no audio (issue #10).
                if not _released_key_matches(key, self._ptt_trigger):
                    return
                self._on_ptt_release()

            listener = keyboard.Listener(
                on_press=_on_key_press,
                on_release=_on_key_release,
            )
            listener.start()
            self._listener = listener
        except Exception as exc:  # noqa: BLE001
            raise HotkeyError(f"could not register PTT hotkey: {exc}") from exc

        try:
            cancel_normalized = _normalize_hotkey(self._cancel_key)
            cancel_listener = keyboard.GlobalHotKeys({cancel_normalized: self._on_cancel})
            cancel_listener.start()
            self._cancel_listener = cancel_listener
        except Exception as exc:  # noqa: BLE001
            # Stop the PTT listener we already started.
            with contextlib.suppress(Exception):
                self._listener.stop()
            raise HotkeyError(f"could not register cancel hotkey: {exc}") from exc

    def stop(self) -> None:
        """Stop both hotkey listeners."""
        for attr in ("_listener", "_cancel_listener"):
            listener = getattr(self, attr)
            setattr(self, attr, None)
            if listener is not None:
                with contextlib.suppress(Exception):
                    listener.stop()

    # ------------------------------------------------------------------
    # Internal callbacks (also called directly by tests)
    # ------------------------------------------------------------------

    def _on_ptt_press(self) -> None:
        """Fire on_press on the first press only; drop auto-repeat."""
        with self._lock:
            if self._pressed:
                return
            self._pressed = True
        self._on_press_cb()

    def _on_ptt_release(self) -> None:
        """Fire on_release; drop spurious releases."""
        with self._lock:
            if not self._pressed:
                return
            self._pressed = False
        self._on_release_cb()

    def _on_cancel(self) -> None:
        self._on_cancel_cb()
