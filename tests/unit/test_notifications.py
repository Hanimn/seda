"""Unit tests for notifications (IMPLEMENTATION_PLAN.md §18)."""

from __future__ import annotations

import io

from local_flow.notifications import (
    ConsoleNotifier,
    FanOutNotifier,
    NotificationEvent,
    OverlayNotifier,
)


class TestNotificationEvent:
    def test_all_required_events_exist(self) -> None:
        names = {e.value for e in NotificationEvent}
        assert {
            "READY",
            "RECORDING",
            "TRANSCRIBING",
            "CANCELLED",
            "BUSY",
            "ERROR",
            "SUCCESS",
        } <= names


class TestConsoleNotifier:
    def _make(self) -> tuple[ConsoleNotifier, io.StringIO]:
        buf = io.StringIO()
        notifier = ConsoleNotifier(stream=buf)
        return notifier, buf

    def test_ready_line(self) -> None:
        n, buf = self._make()
        n.notify(NotificationEvent.READY)
        assert "[ready]" in buf.getvalue()

    def test_recording_line(self) -> None:
        n, buf = self._make()
        n.notify(NotificationEvent.RECORDING)
        assert "[recording]" in buf.getvalue()

    def test_cancelled_line(self) -> None:
        n, buf = self._make()
        n.notify(NotificationEvent.CANCELLED)
        assert "[cancelled]" in buf.getvalue()

    def test_busy_line(self) -> None:
        n, buf = self._make()
        n.notify(NotificationEvent.BUSY)
        assert "[busy]" in buf.getvalue()

    def test_error_line(self) -> None:
        n, buf = self._make()
        n.notify(NotificationEvent.ERROR)
        assert "[error]" in buf.getvalue()

    def test_success_line(self) -> None:
        n, buf = self._make()
        n.notify(NotificationEvent.SUCCESS)
        assert "[done]" in buf.getvalue()

    def test_transcribing_with_duration(self) -> None:
        n, buf = self._make()
        n.notify(NotificationEvent.TRANSCRIBING, duration_seconds=4.2)
        line = buf.getvalue()
        assert "[transcribing]" in line
        assert "4.2" in line

    def test_transcribing_without_duration(self) -> None:
        n, buf = self._make()
        n.notify(NotificationEvent.TRANSCRIBING)
        assert "[transcribing]" in buf.getvalue()

    def test_transcript_text_never_emitted(self) -> None:
        n, buf = self._make()
        secret = "my secret dictation content"
        # Pass the transcript text as a kwarg — the notifier must ignore it.
        n.notify(NotificationEvent.SUCCESS, transcript=secret)
        assert secret not in buf.getvalue()

    def test_notify_respects_console_disabled(self) -> None:
        buf = io.StringIO()
        n = ConsoleNotifier(stream=buf, enabled=False)
        n.notify(NotificationEvent.RECORDING)
        assert buf.getvalue() == ""


class _RecordingNotifier:
    """Records every event it receives; can optionally raise."""

    def __init__(self, *, raises: bool = False) -> None:
        self.events: list[NotificationEvent] = []
        self._raises = raises

    def notify(self, event: NotificationEvent, **kwargs: object) -> None:
        if self._raises:
            raise RuntimeError("notifier boom")
        self.events.append(event)


class TestFanOutNotifier:
    def test_forwards_to_all_children(self) -> None:
        a, b = _RecordingNotifier(), _RecordingNotifier()
        fan = FanOutNotifier([a, b])
        fan.notify(NotificationEvent.RECORDING)
        fan.notify(NotificationEvent.SUCCESS)
        assert a.events == [NotificationEvent.RECORDING, NotificationEvent.SUCCESS]
        assert b.events == [NotificationEvent.RECORDING, NotificationEvent.SUCCESS]

    def test_swallows_a_raising_child_and_still_notifies_others(self) -> None:
        bad = _RecordingNotifier(raises=True)
        good = _RecordingNotifier()
        fan = FanOutNotifier([bad, good])
        # Must not raise, and the good notifier still gets the event.
        fan.notify(NotificationEvent.RECORDING)
        assert good.events == [NotificationEvent.RECORDING]

    def test_forwards_kwargs(self) -> None:
        seen: list[dict[str, object]] = []

        class _KwNotifier:
            def notify(self, event: NotificationEvent, **kwargs: object) -> None:
                seen.append(kwargs)

        fan = FanOutNotifier([_KwNotifier()])
        fan.notify(NotificationEvent.SUCCESS, char_count=42)
        assert seen == [{"char_count": 42}]


class TestOverlayNotifier:
    def _make(self) -> tuple[OverlayNotifier, list[str]]:
        calls: list[str] = []
        # dispatch_main runs the callable synchronously (assert the effect).
        n = OverlayNotifier(
            show=lambda: calls.append("show"),
            hide=lambda: calls.append("hide"),
            dispatch_main=lambda fn: fn(),
        )
        return n, calls

    def test_recording_shows(self) -> None:
        n, calls = self._make()
        n.notify(NotificationEvent.RECORDING)
        assert calls == ["show"]

    def test_terminal_events_hide(self) -> None:
        for terminal in (
            NotificationEvent.CANCELLED,
            NotificationEvent.SUCCESS,
            NotificationEvent.ERROR,
        ):
            n, calls = self._make()
            n.notify(NotificationEvent.RECORDING)
            n.notify(terminal)
            assert calls == ["show", "hide"], terminal

    def test_non_show_hide_events_ignored(self) -> None:
        n, calls = self._make()
        for ignored in (
            NotificationEvent.READY,
            NotificationEvent.BUSY,
            NotificationEvent.TRANSCRIBING,
        ):
            n.notify(ignored)
        assert calls == []

    def test_show_is_idempotent(self) -> None:
        n, calls = self._make()
        n.notify(NotificationEvent.RECORDING)
        n.notify(NotificationEvent.RECORDING)
        assert calls == ["show"], "double show should be a no-op"

    def test_hide_is_idempotent(self) -> None:
        n, calls = self._make()
        # Hide before any show is a no-op (already hidden).
        n.notify(NotificationEvent.CANCELLED)
        assert calls == []
        # After a show, two hides collapse to one.
        n.notify(NotificationEvent.RECORDING)
        n.notify(NotificationEvent.SUCCESS)
        n.notify(NotificationEvent.ERROR)
        assert calls == ["show", "hide"]

    def test_show_hide_are_marshalled_through_dispatch_main(self) -> None:
        dispatched: list[object] = []
        n = OverlayNotifier(
            show=lambda: None,
            hide=lambda: None,
            dispatch_main=lambda fn: dispatched.append(fn),
        )
        n.notify(NotificationEvent.RECORDING)
        # The show was routed through dispatch_main, not called inline.
        assert len(dispatched) == 1

    def test_show_failure_is_swallowed(self) -> None:
        def _boom() -> None:
            raise RuntimeError("panel exploded")

        n = OverlayNotifier(show=_boom, hide=lambda: None, dispatch_main=lambda fn: fn())
        # Must not propagate — fail-open.
        n.notify(NotificationEvent.RECORDING)

    def test_dispatch_failure_is_swallowed(self) -> None:
        def _bad_dispatch(_fn: object) -> None:
            raise RuntimeError("dispatch exploded")

        n = OverlayNotifier(show=lambda: None, hide=lambda: None, dispatch_main=_bad_dispatch)
        n.notify(NotificationEvent.RECORDING)  # must not raise
