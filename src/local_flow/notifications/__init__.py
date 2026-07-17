"""User notifications and feedback (IMPLEMENTATION_PLAN.md §18).

Only console notifications are implemented in Phase 3.  Sound playback
is deferred to Phase 7/8 hardening.  Output lines contain metadata only —
never transcript text, clipboard contents, or secrets.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Any, Protocol, TextIO, runtime_checkable

logger = logging.getLogger(__name__)


class NotificationEvent(StrEnum):
    """Every event the application can surface to the user."""

    READY = "READY"
    RECORDING = "RECORDING"
    TRANSCRIBING = "TRANSCRIBING"
    CANCELLED = "CANCELLED"
    BUSY = "BUSY"
    ERROR = "ERROR"
    SUCCESS = "SUCCESS"


@runtime_checkable
class Notifier(Protocol):
    """Anything that can surface a :class:`NotificationEvent` to the user.

    ``ConsoleNotifier`` already satisfies this. The seam lets a GUI overlay be
    added as a second notifier (ADR-0003's fan-out) without the controller
    knowing which concrete notifiers are present.
    """

    def notify(self, event: NotificationEvent, **kwargs: Any) -> None: ...


class ConsoleNotifier:
    """Writes one-line status messages to *stream* (default: stderr).

    Lines contain only state and metadata (duration, char count).  Transcript
    text must never be passed through to the output — any ``transcript=`` kwarg
    in :meth:`notify` is silently discarded.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        enabled: bool = True,
    ) -> None:
        self._stream: TextIO = stream if stream is not None else sys.stderr
        self._enabled = enabled

    def notify(self, event: NotificationEvent, **kwargs: Any) -> None:
        """Emit a single status line for *event*.

        Accepted metadata kwargs:
        - ``duration_seconds`` (float) — shown for TRANSCRIBING.
        - ``char_count`` (int) — shown for SUCCESS.

        Any other kwargs (including ``transcript``) are ignored.
        """
        if not self._enabled:
            return
        line = self._format(event, kwargs)
        print(line, file=self._stream)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _format(self, event: NotificationEvent, meta: dict[str, Any]) -> str:
        if event is NotificationEvent.READY:
            return "[ready]"
        if event is NotificationEvent.RECORDING:
            return "[recording]"
        if event is NotificationEvent.TRANSCRIBING:
            duration = meta.get("duration_seconds")
            if duration is not None:
                return f"[transcribing] {duration:.1f}s audio"
            return "[transcribing]"
        if event is NotificationEvent.CANCELLED:
            return "[cancelled]"
        if event is NotificationEvent.BUSY:
            return "[busy]"
        if event is NotificationEvent.ERROR:
            return "[error]"
        if event is NotificationEvent.SUCCESS:
            char_count = meta.get("char_count")
            if char_count is not None:
                return f"[done] {char_count} characters"
            return "[done]"
        return f"[{event.value.lower()}]"


class FanOutNotifier:
    """Forwards each event to several notifiers (ADR-0003).

    The controller holds a single :class:`Notifier`; this lets a GUI overlay be
    added alongside the console notifier without the controller knowing. Each
    child notifier's exceptions are **swallowed** so one failing notifier never
    breaks another — and never breaks recording.
    """

    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        self._notifiers: list[Notifier] = list(notifiers)

    def add(self, notifier: Notifier) -> None:
        """Append a notifier after construction.

        Lets the GUI host register its :class:`OverlayNotifier` once the panel
        is built on the main thread (ADR-0001/#20), without the controller
        rebuilding its notifier.
        """
        self._notifiers.append(notifier)

    def notify(self, event: NotificationEvent, **kwargs: Any) -> None:
        for notifier in self._notifiers:
            try:
                notifier.notify(event, **kwargs)
            except Exception:  # noqa: BLE001
                # A broken notifier must not affect the others or dictation.
                logger.warning("notifier %r failed for %s", notifier, event, exc_info=True)


class OverlayNotifier:
    """Drives the overlay's show/hide off the notification stream (ADR-0003).

    Maps ``RECORDING`` → show and ``CANCELLED``/``SUCCESS``/``ERROR`` → hide;
    all other events are ignored (the overlay is a RECORDING-only HUD). Because
    ``notify`` runs on the hotkey-listener / worker threads but AppKit must run
    on the main thread (ADR-0001), the show/hide calls are **marshalled onto the
    main thread** via an injected ``dispatch_main`` callable. Show/hide are
    **idempotent** (show-when-shown / hide-when-hidden are no-ops); a broken
    overlay is swallowed (fail-open — no HUD, never a blocked dictation).
    """

    _HIDE_EVENTS = frozenset(
        {NotificationEvent.CANCELLED, NotificationEvent.SUCCESS, NotificationEvent.ERROR}
    )

    def __init__(
        self,
        *,
        show: Callable[[], None],
        hide: Callable[[], None],
        dispatch_main: Callable[[Callable[[], None]], None],
    ) -> None:
        self._show = show
        self._hide = hide
        self._dispatch_main = dispatch_main
        self._visible = False

    def notify(self, event: NotificationEvent, **kwargs: Any) -> None:
        if event is NotificationEvent.RECORDING:
            self._request(show=True)
        elif event in self._HIDE_EVENTS:
            self._request(show=False)
        # READY / BUSY / TRANSCRIBING are not show/hide events — ignore.

    def _request(self, *, show: bool) -> None:
        # Idempotent: skip if already in the requested visibility.
        if show == self._visible:
            return
        self._visible = show
        action = self._show if show else self._hide

        def _run() -> None:
            try:
                action()
            except Exception:  # noqa: BLE001
                # Fail-open: a broken overlay must never harm dictation.
                logger.warning("overlay show/hide failed", exc_info=True)

        try:
            self._dispatch_main(_run)
        except Exception:  # noqa: BLE001
            logger.warning("overlay dispatch_main failed", exc_info=True)
