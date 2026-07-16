"""Unit tests for notifications (IMPLEMENTATION_PLAN.md §18)."""

from __future__ import annotations

import io

from local_flow.notifications import ConsoleNotifier, NotificationEvent


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
