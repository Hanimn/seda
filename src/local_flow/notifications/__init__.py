"""User notifications and feedback (IMPLEMENTATION_PLAN.md §18).

Only console notifications are implemented in Phase 3.  Sound playback
is deferred to Phase 7/8 hardening.  Output lines contain metadata only —
never transcript text, clipboard contents, or secrets.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from typing import Any, Protocol, TextIO, runtime_checkable


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
