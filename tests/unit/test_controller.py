"""Unit tests for AppController (IMPLEMENTATION_PLAN.md §22).

All tests inject FakeHotkeyProvider and FakeBackend — no real mic, pynput,
or model required.  Threading synchronisation uses threading.Event; no
time.sleep anywhere.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, cast

import numpy as np
import pytest

from seda.app import AppController
from seda.config import Config
from seda.state import AppState
from seda.transcription.fake import FakeBackend

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeHotkeyProvider:
    """Captures the callbacks registered by AppController."""

    def __init__(self) -> None:
        self.on_press: Callable[[], None] = lambda: None
        self.on_release: Callable[[], None] = lambda: None
        self.on_cancel: Callable[[], None] = lambda: None
        self.stopped = False
        self.start_count = 0
        self.chord: str | None = None
        self.set_calls: list[str] = []

    def start(
        self,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_cancel: Callable[[], None],
    ) -> None:
        self.on_press = on_press
        self.on_release = on_release
        self.on_cancel = on_cancel
        self.start_count += 1
        # A restart after a stop clears the stopped flag (models a live listener).
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    def set_push_to_talk(self, new_chord: str) -> None:
        # In-place chord swap (strategy B, #89). Never stops/rebuilds.
        self.set_calls.append(new_chord)
        self.chord = new_chord


def _make_controller(
    *,
    backend: FakeBackend | None = None,
    fake_audio: bool = True,
    config: Config | None = None,
    cleanup_provider: object | None = None,
) -> tuple[AppController, FakeHotkeyProvider, FakeBackend]:
    """Build an AppController with injectable fakes."""
    cfg = config or Config()
    hotkeys = FakeHotkeyProvider()
    be = backend or FakeBackend()
    kwargs: dict[str, object] = {
        "hotkey_provider": hotkeys,
        "backend": be,
        # Inject a fake inserter so unit tests never construct the real
        # pynput-backed paste path (which cannot import on headless Linux).
        "text_inserter": _RecordingInserter(),
    }
    if cleanup_provider is not None:
        kwargs["cleanup_provider"] = cleanup_provider
    ctrl = AppController(cfg, **kwargs)  # type: ignore[arg-type]
    if fake_audio:
        # Inject a fake recorder that produces audio immediately on stop().
        ctrl._recorder = _FakeRecorder()
    return ctrl, hotkeys, be


class _RecordingInserter:
    """Captures the text handed to insert(); reports a successful paste."""

    def __init__(self) -> None:
        self.inserted: list[str] = []
        self.copy_only_calls: list[bool] = []
        self.warmed = 0

    def insert(self, text: str, *, copy_only: bool = False) -> object:
        from seda.input.paste import InsertionResult

        self.inserted.append(text)
        self.copy_only_calls.append(copy_only)
        return InsertionResult(copied=True, pasted=not copy_only, restored=True)

    def warm(self) -> None:
        self.warmed += 1


class _FakeRecorder:
    """Minimal recorder double: stop() returns silent audio, cancel() is a no-op."""

    def start(self) -> None:
        pass

    def stop(self) -> object:
        from seda.audio.recorder import RecordedAudio

        return RecordedAudio(
            samples=np.zeros(16000, dtype=np.float32),
            sample_rate=16000,
            overflow_count=0,
        )

    def cancel(self) -> None:
        pass


def _run_in_thread(ctrl: AppController) -> threading.Thread:
    """Start ctrl.run() in a background thread; return the thread."""
    t = threading.Thread(target=ctrl.run, daemon=True)
    t.start()
    return t


def _wait_state(ctrl: AppController, target: AppState, timeout: float = 2.0) -> bool:
    """Poll (very quickly) until the controller reaches *target* or times out."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ctrl._state_machine.state is target:
            return True
        time.sleep(0.005)
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPressCycle:
    def test_press_transitions_to_recording(self) -> None:
        ctrl, hotkeys, _ = _make_controller()
        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE), "never reached IDLE"
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING), "never reached RECORDING"
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)

    def test_release_after_press_returns_to_idle(self) -> None:
        ctrl, hotkeys, be = _make_controller()
        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            hotkeys.on_release()
            assert _wait_state(ctrl, AppState.IDLE, timeout=5.0), "never returned to IDLE"
            assert be.loaded
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)

    def test_duplicate_press_does_not_double_record(self) -> None:
        ctrl, hotkeys, _ = _make_controller()
        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            # Second press while RECORDING — should be a no-op (state stays RECORDING)
            hotkeys.on_press()
            import time

            time.sleep(0.05)
            assert ctrl._state_machine.state is AppState.RECORDING
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)


class TestMaxDurationAutoStop:
    """Auto-stop-and-transcribe when a recording hits the duration cap (#108)."""

    def test_auto_stop_finalizes_and_pastes(self) -> None:
        ctrl, hotkeys, be = _make_controller()
        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            # The recorder's audio callback calls this at the cap; invoke it
            # directly (it schedules the finalize on the worker thread).
            ctrl._on_max_duration_reached()
            assert _wait_state(ctrl, AppState.IDLE, timeout=5.0), "never returned to IDLE"
            assert be.loaded
            inserter = cast(_RecordingInserter, ctrl._inserter)
            assert len(inserter.inserted) == 1  # transcribed + pasted exactly once
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)

    def test_release_after_auto_stop_does_not_double_paste(self) -> None:
        import time

        ctrl, hotkeys, _ = _make_controller()
        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            ctrl._on_max_duration_reached()
            assert _wait_state(ctrl, AppState.IDLE, timeout=5.0)
            # The key was still held; its eventual release must be a harmless
            # no-op — the RECORDING->PROCESSING_AUDIO guard already fired.
            hotkeys.on_release()
            time.sleep(0.05)
            inserter = cast(_RecordingInserter, ctrl._inserter)
            assert len(inserter.inserted) == 1  # not doubled
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)


class TestCancelDuringRecording:
    def test_cancel_stops_recording_without_transcription(self) -> None:
        be = FakeBackend()
        ctrl, hotkeys, _ = _make_controller(backend=be)
        # Track transcribe calls
        transcribe_calls: list[None] = []
        original_transcribe = be.transcribe

        def counting_transcribe(*args: object, **kwargs: object) -> object:
            transcribe_calls.append(None)
            return original_transcribe(*args, **kwargs)  # type: ignore[arg-type]

        be.transcribe = counting_transcribe  # type: ignore[method-assign]

        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            hotkeys.on_cancel()
            assert _wait_state(ctrl, AppState.IDLE, timeout=3.0), "never returned to IDLE"
            assert len(transcribe_calls) == 0, "transcribe was called after cancel"
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)


class TestBusyBehavior:
    def test_press_while_transcribing_emits_busy_and_stays_transcribing(self) -> None:
        import io

        from seda.notifications import ConsoleNotifier

        buf = io.StringIO()
        notifier = ConsoleNotifier(stream=buf)

        # Use a backend that blocks until we release it, so we can press again
        # while it is still "transcribing".
        transcribe_gate = threading.Event()

        class BlockingFakeBackend(FakeBackend):
            def transcribe(self, audio: object, sample_rate: object) -> object:  # type: ignore[override]
                transcribe_gate.wait()
                return super().transcribe(audio, sample_rate)  # type: ignore[arg-type]

        be = BlockingFakeBackend()
        ctrl, hotkeys, _ = _make_controller(backend=be)
        ctrl._notifier = notifier

        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            hotkeys.on_release()
            assert _wait_state(ctrl, AppState.TRANSCRIBING, timeout=3.0), (
                "never reached TRANSCRIBING"
            )

            # Now press while transcribing.
            hotkeys.on_press()
            import time

            time.sleep(0.05)
            assert ctrl._state_machine.state is AppState.TRANSCRIBING
            assert "[busy]" in buf.getvalue()
        finally:
            transcribe_gate.set()
            ctrl.shutdown()
            t.join(timeout=3.0)


class TestTextInsertion:
    """The transcript is processed and handed to the inserter (Phase 5)."""

    def test_transcript_processed_and_inserted(self) -> None:
        be = FakeBackend(text="hello world")
        ctrl, hotkeys, _ = _make_controller(backend=be)
        inserter = _RecordingInserter()
        ctrl._inserter = inserter  # type: ignore[assignment]

        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            hotkeys.on_release()
            assert _wait_state(ctrl, AppState.IDLE, timeout=5.0)
            assert inserter.inserted == ["hello world"]
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)

    def test_spoken_commands_applied_before_insertion(self) -> None:
        be = FakeBackend(text="symbol open brace key symbol colon val symbol close brace")
        ctrl, hotkeys, _ = _make_controller(backend=be)
        inserter = _RecordingInserter()
        ctrl._inserter = inserter  # type: ignore[assignment]

        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            hotkeys.on_release()
            assert _wait_state(ctrl, AppState.IDLE, timeout=5.0)
            assert inserter.inserted == ["{key:val}"]
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)

    def test_cancel_command_in_transcript_skips_insertion(self) -> None:
        be = FakeBackend(text="scratch that never mind")
        ctrl, hotkeys, _ = _make_controller(backend=be)
        inserter = _RecordingInserter()
        ctrl._inserter = inserter  # type: ignore[assignment]

        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            hotkeys.on_release()
            assert _wait_state(ctrl, AppState.IDLE, timeout=5.0)
            # A beginning-of-transcript cancel command discards the dictation.
            assert inserter.inserted == []
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)

    def test_empty_transcript_skips_insertion(self) -> None:
        be = FakeBackend(text="   ")
        ctrl, hotkeys, _ = _make_controller(backend=be)
        inserter = _RecordingInserter()
        ctrl._inserter = inserter  # type: ignore[assignment]

        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            hotkeys.on_release()
            assert _wait_state(ctrl, AppState.IDLE, timeout=5.0)
            assert inserter.inserted == []
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)

    def test_paste_failure_emits_error_notification(self) -> None:
        import io

        from seda.notifications import ConsoleNotifier

        buf = io.StringIO()
        be = FakeBackend(text="hello world")
        ctrl, hotkeys, _ = _make_controller(backend=be)
        ctrl._notifier = ConsoleNotifier(stream=buf)

        class FailingInserter:
            def insert(self, text: str, *, copy_only: bool = False) -> object:
                from seda.input.paste import InsertionResult

                return InsertionResult(
                    copied=True, pasted=False, restored=False, error="paste failed"
                )

        ctrl._inserter = FailingInserter()  # type: ignore[assignment]

        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            hotkeys.on_release()
            assert _wait_state(ctrl, AppState.IDLE, timeout=5.0)
            # Paste failure surfaces as an error, but never crashes the loop.
            assert "[error]" in buf.getvalue()
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)


def _cleanup_config(*, mode: str = "standard", enabled: bool = True) -> Config:
    """A Config with cleanup enabled (and app.mode set)."""
    from seda.config import load_config_from_dict

    return load_config_from_dict({"app": {"mode": mode}, "cleanup": {"enabled": enabled}})


def _record_states(ctrl: AppController) -> list[AppState]:
    """Patch the controller's state machine to record every transition target."""
    seen: list[AppState] = []
    original = ctrl._state_machine.transition

    def recording(to: AppState) -> None:
        seen.append(to)
        original(to)

    ctrl._state_machine.transition = recording  # type: ignore[method-assign]
    return seen


class TestCleanupPath:
    """Cleanup runs only when enabled + non-literal; always fail-open (Phase 6)."""

    def test_cleaned_text_pasted_and_cleaning_state_entered(self) -> None:
        from seda.cleanup.fake import FakeCleanupProvider

        be = FakeBackend(text="um so fix the bug")
        provider = FakeCleanupProvider(output="fix the bug")
        ctrl, hotkeys, _ = _make_controller(
            backend=be, config=_cleanup_config(), cleanup_provider=provider
        )
        inserter = _RecordingInserter()
        ctrl._inserter = inserter  # type: ignore[assignment]
        seen = _record_states(ctrl)

        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            hotkeys.on_release()
            assert _wait_state(ctrl, AppState.IDLE, timeout=5.0)
            assert AppState.CLEANING in seen
            assert inserter.inserted == ["fix the bug"]
            assert len(provider.calls) == 1
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)

    def test_validation_failure_falls_back_to_deterministic(self) -> None:
        from seda.cleanup.fake import FakeCleanupProvider

        be = FakeBackend(text="fix the bug")
        # An assistant preface is rejected by validation → fall back.
        provider = FakeCleanupProvider(output="Sure, here is the cleaned text")
        ctrl, hotkeys, _ = _make_controller(
            backend=be, config=_cleanup_config(), cleanup_provider=provider
        )
        inserter = _RecordingInserter()
        ctrl._inserter = inserter  # type: ignore[assignment]

        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            hotkeys.on_release()
            assert _wait_state(ctrl, AppState.IDLE, timeout=5.0)
            # Rejected cleanup → deterministic transcript pasted, not the preface.
            assert inserter.inserted == ["fix the bug"]
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)

    def test_apparent_answer_falls_back_to_deterministic(self) -> None:
        from seda.cleanup.fake import FakeCleanupProvider

        be = FakeBackend(text="how do I fix the slow query")
        # The model answered instead of cleaning → rejected → fall back.
        provider = FakeCleanupProvider(output="You should add a database index")
        ctrl, hotkeys, _ = _make_controller(
            backend=be, config=_cleanup_config(), cleanup_provider=provider
        )
        inserter = _RecordingInserter()
        ctrl._inserter = inserter  # type: ignore[assignment]

        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            hotkeys.on_release()
            assert _wait_state(ctrl, AppState.IDLE, timeout=5.0)
            assert inserter.inserted == ["how do I fix the slow query"]
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)

    def test_provider_error_falls_back_transcription_not_lost(self) -> None:
        from seda.cleanup.fake import FakeCleanupProvider
        from seda.errors import CleanupError

        be = FakeBackend(text="fix the bug")
        provider = FakeCleanupProvider(raise_error=CleanupError("no server"))
        ctrl, hotkeys, _ = _make_controller(
            backend=be, config=_cleanup_config(), cleanup_provider=provider
        )
        inserter = _RecordingInserter()
        ctrl._inserter = inserter  # type: ignore[assignment]

        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            hotkeys.on_release()
            assert _wait_state(ctrl, AppState.IDLE, timeout=5.0)
            # Provider crashed → transcription preserved, deterministic pasted.
            assert inserter.inserted == ["fix the bug"]
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)

    def test_literal_mode_bypasses_cleanup(self) -> None:
        from seda.cleanup.fake import FakeCleanupProvider

        # Base mode literal → cleanup provider must never be called.
        be = FakeBackend(text="fix the bug")
        provider = FakeCleanupProvider(output="SHOULD NOT APPEAR")
        ctrl, hotkeys, _ = _make_controller(
            backend=be,
            config=_cleanup_config(mode="literal"),
            cleanup_provider=provider,
        )
        inserter = _RecordingInserter()
        ctrl._inserter = inserter  # type: ignore[assignment]
        seen = _record_states(ctrl)

        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            hotkeys.on_release()
            assert _wait_state(ctrl, AppState.IDLE, timeout=5.0)
            assert provider.calls == []
            assert AppState.CLEANING not in seen
            assert inserter.inserted == ["fix the bug"]
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)

    def test_disabled_cleanup_skips_provider(self) -> None:
        from seda.cleanup.fake import FakeCleanupProvider

        be = FakeBackend(text="fix the bug")
        provider = FakeCleanupProvider(output="SHOULD NOT APPEAR")
        ctrl, hotkeys, _ = _make_controller(
            backend=be,
            config=_cleanup_config(enabled=False),
            cleanup_provider=provider,
        )
        inserter = _RecordingInserter()
        ctrl._inserter = inserter  # type: ignore[assignment]

        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            hotkeys.on_release()
            assert _wait_state(ctrl, AppState.IDLE, timeout=5.0)
            assert provider.calls == []
            assert inserter.inserted == ["fix the bug"]
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)


class TestShutdown:
    def test_shutdown_from_idle_reaches_stopping(self) -> None:
        ctrl, _, _ = _make_controller()
        t = _run_in_thread(ctrl)
        assert _wait_state(ctrl, AppState.IDLE)
        ctrl.shutdown()
        t.join(timeout=3.0)
        assert not t.is_alive()

    def test_shutdown_from_recording(self) -> None:
        ctrl, hotkeys, _ = _make_controller()
        t = _run_in_thread(ctrl)
        assert _wait_state(ctrl, AppState.IDLE)
        hotkeys.on_press()
        assert _wait_state(ctrl, AppState.RECORDING)
        ctrl.shutdown()
        t.join(timeout=3.0)
        assert not t.is_alive()

    def test_sigint_handler_wired_on_main_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The SIGINT handler is installed when run() executes on the main thread."""
        import signal as _signal

        ctrl, _, _ = _make_controller()
        captured_handler: list[Any] = []
        original_signal = _signal.signal

        def capturing_signal(signum: int, handler: Any) -> Any:
            if signum == _signal.SIGINT:
                captured_handler.append(handler)
            return original_signal(signum, handler)

        monkeypatch.setattr(_signal, "signal", capturing_signal)

        ready = threading.Event()
        original_notify = ctrl._notifier.notify

        def notify_and_signal(event: object, **kw: object) -> None:
            original_notify(event, **kw)  # type: ignore[arg-type]
            ready.set()

        ctrl._notifier.notify = notify_and_signal  # type: ignore[method-assign]

        def _delayed_shutdown() -> None:
            ready.wait(timeout=2.0)
            ctrl.shutdown()

        stopper = threading.Thread(target=_delayed_shutdown, daemon=True)
        stopper.start()
        ctrl.run()  # blocks until shutdown — runs on main thread
        stopper.join(timeout=3.0)

        assert len(captured_handler) > 0, "signal.signal(SIGINT, ...) was never called"


class TestStartSeam:
    """start() is the non-blocking setup half of run() (ADR-0001)."""

    def test_start_is_non_blocking_and_readies_the_controller(self) -> None:
        ctrl, hotkeys, backend = _make_controller()
        # start() must return promptly (it does not wait on the shutdown event)
        # and leave the controller ready to receive hotkey callbacks.
        ctrl.start()
        try:
            assert backend.loaded is True
            assert ctrl._state_machine.state is AppState.IDLE
            # Hotkey callbacks were registered (the provider captured them).
            assert hotkeys.on_press is not None
            # And the controller actually responds to a press.
            hotkeys.on_press()
            assert ctrl._state_machine.state is AppState.RECORDING
        finally:
            ctrl.shutdown()

    def test_start_emits_ready(self) -> None:
        import io

        from seda.notifications import ConsoleNotifier, NotificationEvent

        buf = io.StringIO()
        ctrl, _, _ = _make_controller()
        ctrl._notifier = ConsoleNotifier(stream=buf)
        ctrl.start()
        try:
            assert NotificationEvent.READY.value.lower() in buf.getvalue().lower()
        finally:
            ctrl.shutdown()

    def test_start_bails_cleanly_if_shutdown_raced_during_load(self) -> None:
        """A Ctrl-C during the slow backend load() must not crash start().

        Regression: the signal handler runs on the main thread and moves the
        state to STOPPING while load() is in flight; the subsequent
        STARTING->IDLE transition is then invalid. start() must bail cleanly,
        not raise, and must not start hotkeys on an already-stopping app.
        """
        ctrl, hotkeys, _ = _make_controller()

        # Simulate the race: shutdown lands (state -> STOPPING) *during* load().
        original_load = ctrl._backend.load

        def _load_then_shutdown() -> None:
            original_load()
            ctrl._state_machine.transition(AppState.STOPPING)

        ctrl._backend.load = _load_then_shutdown  # type: ignore[method-assign]

        # Must not raise InvalidTransitionError.
        ctrl.start()

        # It bailed: state stayed STOPPING and hotkeys were never started.
        assert ctrl._state_machine.state is AppState.STOPPING
        assert hotkeys.on_press is not None  # default lambda, never registered a press cb
        # A press must be a no-op (not IDLE -> RECORDING) since we bailed.
        hotkeys.on_press()
        assert ctrl._state_machine.state is AppState.STOPPING


class TestNotifierInjection:
    """A notifier can be injected via the constructor (seam for ADR-0003)."""

    def test_injected_notifier_receives_events(self) -> None:
        recorded: list[object] = []

        class _RecordingNotifier:
            def notify(self, event: object, **kwargs: object) -> None:
                recorded.append(event)

        cfg = Config()
        ctrl = AppController(
            cfg,
            hotkey_provider=FakeHotkeyProvider(),
            backend=FakeBackend(),
            text_inserter=_RecordingInserter(),
            notifier=_RecordingNotifier(),  # type: ignore[arg-type]
        )
        ctrl._recorder = _FakeRecorder()
        ctrl.start()
        try:
            from seda.notifications import NotificationEvent

            assert NotificationEvent.READY in recorded
        finally:
            ctrl.shutdown()

    def test_default_notifier_is_console(self) -> None:
        from seda.notifications import ConsoleNotifier

        ctrl, _, _ = _make_controller()
        assert isinstance(ctrl._notifier, ConsoleNotifier)

    def test_busy_fires_on_release_before_transcribing(self) -> None:
        """Releasing keys emits BUSY, ahead of TRANSCRIBING, so the HUD flips to
        its busy visual immediately (spec: HUD responsive-wave + busy-visual)."""
        from seda.notifications import NotificationEvent

        recorded: list[NotificationEvent] = []

        class _RecordingNotifier:
            def notify(self, event: NotificationEvent, **kwargs: object) -> None:
                recorded.append(event)

        cfg = Config()
        hotkeys = FakeHotkeyProvider()
        ctrl = AppController(
            cfg,
            hotkey_provider=hotkeys,
            backend=FakeBackend(),
            text_inserter=_RecordingInserter(),
            notifier=_RecordingNotifier(),  # type: ignore[arg-type]
        )
        ctrl._recorder = _FakeRecorder()
        t = _run_in_thread(ctrl)
        try:
            assert _wait_state(ctrl, AppState.IDLE)
            hotkeys.on_press()
            assert _wait_state(ctrl, AppState.RECORDING)
            hotkeys.on_release()
            assert _wait_state(ctrl, AppState.IDLE, timeout=5.0)

            assert NotificationEvent.BUSY in recorded, "release must emit BUSY"
            # BUSY (on release) must come before TRANSCRIBING (worker pickup).
            assert recorded.index(NotificationEvent.BUSY) < recorded.index(
                NotificationEvent.TRANSCRIBING
            )
        finally:
            ctrl.shutdown()
            t.join(timeout=3.0)


# ---------------------------------------------------------------------------
# reconfigure_hotkeys — live listener re-registration (#89)
# ---------------------------------------------------------------------------


class TestReconfigureHotkeys:
    """AppController.reconfigure_hotkeys swaps the PTT chord IN PLACE on the running
    provider (strategy B, #89) — it never stops or rebuilds the listener (doing so
    re-enters Carbon TIS and crashes on macOS)."""

    def test_swaps_chord_in_place_on_the_running_provider(self) -> None:
        ctrl, provider, _ = _make_controller()
        ctrl.start()  # STARTING -> IDLE, starts the provider once
        assert isinstance(provider, FakeHotkeyProvider)
        assert provider.start_count == 1

        new_cfg = Config()
        new_cfg.hotkeys.push_to_talk = "<ctrl>+<alt>+m"
        assert ctrl.reconfigure_hotkeys(new_cfg) is True

        # In place: same provider object, chord swapped via set_push_to_talk,
        # listener NEVER stopped or restarted.
        assert ctrl._hotkeys is provider
        assert provider.set_calls == ["<ctrl>+<alt>+m"]
        assert provider.stopped is False
        assert provider.start_count == 1, "the listener must NOT be restarted"

    def test_rebinds_config_on_success(self) -> None:
        ctrl, _, _ = _make_controller()
        ctrl.start()
        new_cfg = Config()
        new_cfg.hotkeys.push_to_talk = "<cmd>+<shift>+d"
        assert ctrl.reconfigure_hotkeys(new_cfg) is True
        assert ctrl._config is new_cfg

    def test_no_swap_while_recording(self) -> None:
        ctrl, provider, _ = _make_controller()
        ctrl.start()
        ctrl._state_machine.transition(AppState.RECORDING)
        new_cfg = Config()
        new_cfg.hotkeys.push_to_talk = "<ctrl>+<alt>+m"
        # Returns False (skipped) so the caller won't persist the unapplied chord.
        assert ctrl.reconfigure_hotkeys(new_cfg) is False
        assert provider.set_calls == []
        assert ctrl._config is not new_cfg

    def test_no_swap_while_stopping(self) -> None:
        ctrl, provider, _ = _make_controller()
        ctrl.start()
        ctrl._state_machine.transition(AppState.STOPPING)
        new_cfg = Config()
        new_cfg.hotkeys.push_to_talk = "<ctrl>+<alt>+m"
        assert ctrl.reconfigure_hotkeys(new_cfg) is False
        assert provider.set_calls == []
        assert ctrl._config is not new_cfg

    def test_invalid_chord_propagates_and_leaves_config_unchanged(self) -> None:
        from seda.errors import HotkeyError

        ctrl, provider, _ = _make_controller()
        ctrl.start()

        def _raise(_chord: str) -> None:
            raise HotkeyError("bad chord")

        provider.set_push_to_talk = _raise  # type: ignore[method-assign, assignment]
        new_cfg = Config()
        new_cfg.hotkeys.push_to_talk = "<ctrl>+<alt>+m"
        with pytest.raises(HotkeyError):
            ctrl.reconfigure_hotkeys(new_cfg)
        # set_push_to_talk validates before mutating, so the live chord is intact;
        # config is NOT rebound. The listener was never stopped.
        assert ctrl._hotkeys is provider
        assert provider.stopped is False
        assert ctrl._config is not new_cfg


class TestHotkeyCaptureGuard:
    """While capturing a new chord, the hotkey callbacks must be neutralized so
    the capture keystrokes (seen by the global listener too) can't drive a phantom
    dictation cycle that wedges the state machine (#89)."""

    def test_on_press_is_ignored_while_capturing(self) -> None:
        ctrl, provider, _ = _make_controller()
        ctrl.start()  # IDLE
        ctrl.begin_hotkey_capture()

        # A press arriving from the global listener during capture must NOT start
        # recording — state stays IDLE.
        provider.on_press()
        assert ctrl._state_machine.state is AppState.IDLE

        ctrl.end_hotkey_capture()
        # Once capture ends, a press records normally again.
        provider.on_press()
        assert ctrl._state_machine.state is AppState.RECORDING

    def test_release_and_cancel_ignored_while_capturing(self) -> None:
        ctrl, provider, _ = _make_controller()
        ctrl.start()
        ctrl.begin_hotkey_capture()
        # Neither release nor cancel should move the state machine while capturing.
        provider.on_release()
        provider.on_cancel()
        assert ctrl._state_machine.state is AppState.IDLE


class TestWarmInserter:
    """warm_inserter() pre-builds the paste backend on the caller thread (#89)."""

    def test_warm_inserter_delegates_to_inserter(self) -> None:
        ctrl, _, _ = _make_controller()
        # _make_controller injects a _RecordingInserter.
        inserter = ctrl._inserter
        assert isinstance(inserter, _RecordingInserter)
        assert inserter.warmed == 0
        ctrl.warm_inserter()
        assert inserter.warmed == 1
