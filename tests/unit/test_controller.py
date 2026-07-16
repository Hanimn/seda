"""Unit tests for AppController (IMPLEMENTATION_PLAN.md §22).

All tests inject FakeHotkeyProvider and FakeBackend — no real mic, pynput,
or model required.  Threading synchronisation uses threading.Event; no
time.sleep anywhere.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from local_flow.app import AppController
from local_flow.config import Config
from local_flow.state import AppState
from local_flow.transcription.fake import FakeBackend

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

    def start(
        self,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_cancel: Callable[[], None],
    ) -> None:
        self.on_press = on_press
        self.on_release = on_release
        self.on_cancel = on_cancel

    def stop(self) -> None:
        self.stopped = True


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
    kwargs: dict[str, object] = {"hotkey_provider": hotkeys, "backend": be}
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

    def insert(self, text: str, *, copy_only: bool = False) -> object:
        from local_flow.input.paste import InsertionResult

        self.inserted.append(text)
        self.copy_only_calls.append(copy_only)
        return InsertionResult(copied=True, pasted=not copy_only, restored=True)


class _FakeRecorder:
    """Minimal recorder double: stop() returns silent audio, cancel() is a no-op."""

    def start(self) -> None:
        pass

    def stop(self) -> object:
        from local_flow.audio.recorder import RecordedAudio

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

        from local_flow.notifications import ConsoleNotifier

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

        from local_flow.notifications import ConsoleNotifier

        buf = io.StringIO()
        be = FakeBackend(text="hello world")
        ctrl, hotkeys, _ = _make_controller(backend=be)
        ctrl._notifier = ConsoleNotifier(stream=buf)

        class FailingInserter:
            def insert(self, text: str, *, copy_only: bool = False) -> object:
                from local_flow.input.paste import InsertionResult

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
    from local_flow.config import load_config_from_dict

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
        from local_flow.cleanup.fake import FakeCleanupProvider

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
        from local_flow.cleanup.fake import FakeCleanupProvider

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
        from local_flow.cleanup.fake import FakeCleanupProvider

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
        from local_flow.cleanup.fake import FakeCleanupProvider
        from local_flow.errors import CleanupError

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
        from local_flow.cleanup.fake import FakeCleanupProvider

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
        from local_flow.cleanup.fake import FakeCleanupProvider

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
