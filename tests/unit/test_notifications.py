"""Unit tests for notifications (IMPLEMENTATION_PLAN.md §18)."""

from __future__ import annotations

import io

from seda.notifications import (
    ConsoleNotifier,
    FanOutNotifier,
    HudMode,
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
        # set_mode records "mode:<name>" so ordering vs show/hide is assertable.
        n = OverlayNotifier(
            show=lambda: calls.append("show"),
            hide=lambda: calls.append("hide"),
            set_mode=lambda mode: calls.append(f"mode:{mode}"),
            dispatch_main=lambda fn: fn(),
        )
        return n, calls

    def test_recording_shows_in_listening_mode(self) -> None:
        n, calls = self._make()
        n.notify(NotificationEvent.RECORDING)
        assert calls == ["show", "mode:listening"]

    def test_busy_shows_in_busy_mode(self) -> None:
        n, calls = self._make()
        n.notify(NotificationEvent.BUSY)
        # BUSY shows defensively (in case a RECORDING was missed) and sets busy.
        assert calls == ["show", "mode:busy"]

    def test_set_mode_receives_a_hudmode_enum_not_a_bare_string(self) -> None:
        modes: list[object] = []
        n = OverlayNotifier(
            show=lambda: None,
            hide=lambda: None,
            set_mode=modes.append,
            dispatch_main=lambda fn: fn(),
        )
        n.notify(NotificationEvent.RECORDING)
        n.notify(NotificationEvent.BUSY)
        assert modes == [HudMode.LISTENING, HudMode.BUSY]
        assert all(isinstance(m, HudMode) for m in modes)

    def test_recording_then_busy_switches_mode_without_rehiding(self) -> None:
        n, calls = self._make()
        n.notify(NotificationEvent.RECORDING)
        n.notify(NotificationEvent.BUSY)
        # Already visible after RECORDING → BUSY must NOT re-show, only set mode.
        assert calls == ["show", "mode:listening", "mode:busy"]

    def test_busy_persists_through_transcribing_until_terminal(self) -> None:
        n, calls = self._make()
        n.notify(NotificationEvent.RECORDING)
        n.notify(NotificationEvent.BUSY)
        # TRANSCRIBING must not change visibility or mode (busy is already set).
        n.notify(NotificationEvent.TRANSCRIBING)
        n.notify(NotificationEvent.SUCCESS)
        assert calls == ["show", "mode:listening", "mode:busy", "hide"]

    def test_terminal_events_hide(self) -> None:
        for terminal in (
            NotificationEvent.CANCELLED,
            NotificationEvent.SUCCESS,
            NotificationEvent.ERROR,
        ):
            n, calls = self._make()
            n.notify(NotificationEvent.RECORDING)
            n.notify(terminal)
            assert calls == ["show", "mode:listening", "hide"], terminal

    def test_ready_and_transcribing_are_ignored(self) -> None:
        n, calls = self._make()
        for ignored in (
            NotificationEvent.READY,
            NotificationEvent.TRANSCRIBING,
        ):
            n.notify(ignored)
        assert calls == []

    def test_show_is_idempotent_but_mode_reasserts(self) -> None:
        n, calls = self._make()
        n.notify(NotificationEvent.RECORDING)
        n.notify(NotificationEvent.RECORDING)
        # Double show collapses to one; the listening mode is set each time
        # (cheap + idempotent — re-setting the same mode just redraws).
        assert calls == ["show", "mode:listening", "mode:listening"]

    def test_busy_reassert_while_busy_does_not_reshow(self) -> None:
        n, calls = self._make()
        n.notify(NotificationEvent.BUSY)
        n.notify(NotificationEvent.BUSY)  # e.g. press-while-busy nudge
        assert calls == ["show", "mode:busy", "mode:busy"]

    def test_hide_is_idempotent(self) -> None:
        n, calls = self._make()
        # Hide before any show is a no-op (already hidden).
        n.notify(NotificationEvent.CANCELLED)
        assert calls == []
        # After a show, two hides collapse to one.
        n.notify(NotificationEvent.RECORDING)
        n.notify(NotificationEvent.SUCCESS)
        n.notify(NotificationEvent.ERROR)
        assert calls == ["show", "mode:listening", "hide"]

    def test_show_hide_and_mode_are_marshalled_through_dispatch_main(self) -> None:
        dispatched: list[object] = []
        n = OverlayNotifier(
            show=lambda: None,
            hide=lambda: None,
            set_mode=lambda _mode: None,
            dispatch_main=lambda fn: dispatched.append(fn),
        )
        n.notify(NotificationEvent.RECORDING)
        # show + set_mode are batched into ONE main-thread dispatch (atomic — no
        # flicker between showing the panel and setting its mode), not called
        # inline and not split across two run-loop turns.
        assert len(dispatched) == 1

    def test_show_failure_is_swallowed(self) -> None:
        def _boom() -> None:
            raise RuntimeError("panel exploded")

        n = OverlayNotifier(
            show=_boom,
            hide=lambda: None,
            set_mode=lambda _mode: None,
            dispatch_main=lambda fn: fn(),
        )
        # Must not propagate — fail-open.
        n.notify(NotificationEvent.RECORDING)

    def test_set_mode_failure_is_swallowed(self) -> None:
        def _boom(_mode: str) -> None:
            raise RuntimeError("mode exploded")

        n = OverlayNotifier(
            show=lambda: None,
            hide=lambda: None,
            set_mode=_boom,
            dispatch_main=lambda fn: fn(),
        )
        n.notify(NotificationEvent.RECORDING)  # must not raise

    def test_dispatch_failure_is_swallowed(self) -> None:
        def _bad_dispatch(_fn: object) -> None:
            raise RuntimeError("dispatch exploded")

        n = OverlayNotifier(
            show=lambda: None,
            hide=lambda: None,
            set_mode=lambda _mode: None,
            dispatch_main=_bad_dispatch,
        )
        n.notify(NotificationEvent.RECORDING)  # must not raise
