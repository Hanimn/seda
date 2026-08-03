"""Unit tests for notifications (IMPLEMENTATION_PLAN.md §18)."""

from __future__ import annotations

import io

import pytest

from seda.notifications import (
    HUD_ACTIVE_HZ,
    HUD_IDLE_HZ,
    HUD_IDLE_SHIMMER_AMP,
    HUD_IDLE_SHIMMER_BASE,
    ConsoleNotifier,
    FanOutNotifier,
    HudMode,
    NotificationEvent,
    OverlayNotifier,
    hud_idle_shimmer,
    hud_phase_seconds,
    hud_redraw_hz,
    status_label,
    status_symbol,
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
        # The terminal SUCCESS settles the persistent HUD back to IDLE, still shown
        # (ADR-0007 §2 — was "hide" under the old ephemeral contract).
        assert calls == ["show", "mode:listening", "mode:busy", "mode:idle"]

    def test_ready_shows_in_idle_mode(self) -> None:
        n, calls = self._make()
        # READY is the first-show trigger (ADR-0007 §2) — was ignored before.
        n.notify(NotificationEvent.READY)
        assert calls == ["show", "mode:idle"]

    def test_terminal_events_settle_to_idle_and_stay_shown(self) -> None:
        # Was test_terminal_events_hide: terminals no longer hide, they set IDLE
        # and keep the panel shown (ADR-0007 §2/§3 — only teardown removes it).
        for terminal in (
            NotificationEvent.CANCELLED,
            NotificationEvent.SUCCESS,
            NotificationEvent.ERROR,
        ):
            n, calls = self._make()
            n.notify(NotificationEvent.RECORDING)
            n.notify(terminal)
            assert calls == ["show", "mode:listening", "mode:idle"], terminal
            assert "hide" not in calls, terminal

    def test_transcribing_is_ignored(self) -> None:
        # READY now shows (see test_ready_shows_in_idle_mode); only TRANSCRIBING
        # remains a no-op in the event→mode mapping.
        n, calls = self._make()
        n.notify(NotificationEvent.TRANSCRIBING)
        assert calls == []

    def test_show_is_idempotent_but_mode_reasserts(self) -> None:
        n, calls = self._make()
        n.notify(NotificationEvent.RECORDING)
        n.notify(NotificationEvent.RECORDING)
        # Double show collapses to one (one-shot latch); the listening mode is set
        # each time (cheap + idempotent — re-setting the same mode just redraws).
        assert calls == ["show", "mode:listening", "mode:listening"]

    def test_busy_reassert_while_busy_does_not_reshow(self) -> None:
        n, calls = self._make()
        n.notify(NotificationEvent.BUSY)
        n.notify(NotificationEvent.BUSY)  # e.g. press-while-busy nudge
        assert calls == ["show", "mode:busy", "mode:busy"]

    def test_full_cycle_shows_once_and_never_hides(self) -> None:
        # The persistent-companion guarantee (ADR-0007 §3/§4): across a full
        # READY→record→busy→terminal→record cycle the panel shows EXACTLY once and
        # nothing ever hides — every beat after the first is a flicker-free mode flip.
        n, calls = self._make()
        for event in (
            NotificationEvent.READY,
            NotificationEvent.RECORDING,
            NotificationEvent.BUSY,
            NotificationEvent.SUCCESS,
            NotificationEvent.RECORDING,
            NotificationEvent.CANCELLED,
        ):
            n.notify(event)
        assert calls.count("show") == 1, "panel shows exactly once"
        assert "hide" not in calls, "no event ever hides the persistent HUD"
        assert calls == [
            "show",
            "mode:idle",
            "mode:listening",
            "mode:busy",
            "mode:idle",
            "mode:listening",
            "mode:idle",
        ]

    def test_terminal_self_heals_a_missed_ready_show(self) -> None:
        # If the READY show is ever missed (startup dispatch race), the next event —
        # here a terminal — still carries show=True, so the latch self-heals and the
        # HUD comes up in IDLE rather than staying invisible for the session (§4).
        n, calls = self._make()
        n.notify(NotificationEvent.SUCCESS)  # no prior READY/RECORDING
        assert calls == ["show", "mode:idle"]

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


class TestSharedHudCadence:
    """The ADR-0007 §5 shared knobs: ONE rate + shimmer pair for both hosts."""

    def test_redraw_hz_is_active_except_in_idle(self) -> None:
        assert hud_redraw_hz(HudMode.IDLE) == HUD_IDLE_HZ
        assert hud_redraw_hz(HudMode.LISTENING) == HUD_ACTIVE_HZ
        assert hud_redraw_hz(HudMode.BUSY) == HUD_ACTIVE_HZ
        # Idle must be the throttled rate (the whole point of §5's CPU saving).
        assert HUD_IDLE_HZ < HUD_ACTIVE_HZ

    def test_phase_seconds_normalizes_across_the_rate_change(self) -> None:
        # The same real elapsed second is the same phase regardless of mode, so the
        # shimmer period does not stretch 6× when the timer throttles to idle:
        # one second is HUD_ACTIVE_HZ active frames or HUD_IDLE_HZ idle frames.
        active_1s = hud_phase_seconds(HUD_ACTIVE_HZ, HudMode.LISTENING)
        idle_1s = hud_phase_seconds(HUD_IDLE_HZ, HudMode.IDLE)
        assert active_1s == pytest.approx(1.0)
        assert idle_1s == pytest.approx(1.0)

    def test_idle_shimmer_stays_within_a_visible_band(self) -> None:
        # A slow breath around the base, floored well above zero ("alive at rest").
        lo = HUD_IDLE_SHIMMER_BASE - HUD_IDLE_SHIMMER_AMP
        hi = HUD_IDLE_SHIMMER_BASE + HUD_IDLE_SHIMMER_AMP
        for frame in range(0, 200):
            alpha = hud_idle_shimmer(frame)
            assert lo - 1e-9 <= alpha <= hi + 1e-9
        assert lo > 0.0, "the idle pill never fully vanishes"


# --- Menu-bar status surface (#87) -----------------------------------------


def test_status_label_maps_each_mode() -> None:
    """status_label() is the single home for the menu-bar status text (#87)."""
    assert status_label(HudMode.IDLE) == "Idle"
    assert status_label(HudMode.LISTENING) == "Listening"
    assert status_label(HudMode.BUSY) == "Busy"


def test_status_symbol_maps_each_mode() -> None:
    """status_symbol() yields a distinct, non-empty SF Symbol name per mode (#87).

    Distinctness so the three states read differently in the menu bar; non-empty
    so the AppKit side always has a symbol name to try.
    """
    symbols = {
        status_symbol(HudMode.IDLE),
        status_symbol(HudMode.LISTENING),
        status_symbol(HudMode.BUSY),
    }
    assert len(symbols) == 3, "each mode must map to a distinct symbol"
    assert all(s for s in symbols), "symbol names are non-empty"


def test_composed_set_mode_feeds_overlay_and_status() -> None:
    """A composed set_mode fans the SAME HudMode to both the overlay and the status
    surface, with the event->mode mapping single-sourced in OverlayNotifier (#87)."""
    overlay_modes: list[HudMode] = []
    status_modes: list[HudMode] = []

    def _composed_set_mode(mode: HudMode) -> None:
        overlay_modes.append(mode)  # stand-in for overlay.set_mode
        status_modes.append(mode)  # stand-in for the status-item apply

    # Drive through a real OverlayNotifier (synchronous dispatch) so the event->mode
    # table (READY->IDLE, RECORDING->LISTENING, BUSY->BUSY, SUCCESS->IDLE) is the one
    # exercised — the status surface must never re-derive it.
    notifier = OverlayNotifier(
        show=lambda: None,
        hide=lambda: None,
        set_mode=_composed_set_mode,
        dispatch_main=lambda fn: fn(),
    )
    notifier.notify(NotificationEvent.READY)
    notifier.notify(NotificationEvent.RECORDING)
    notifier.notify(NotificationEvent.BUSY)
    notifier.notify(NotificationEvent.SUCCESS)

    assert overlay_modes == [
        HudMode.IDLE,
        HudMode.LISTENING,
        HudMode.BUSY,
        HudMode.IDLE,
    ]
    assert status_modes == overlay_modes, "status surface sees the identical mode stream"
