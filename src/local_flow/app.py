"""Application controller — wires hotkeys, state machine, recorder, and
transcription into the push-to-talk loop (IMPLEMENTATION_PLAN.md §22).

Thread model:
  - Main thread: runs ``run()``, blocks on the shutdown event.
  - Hotkey listener thread: managed by pynput inside ``HotkeyProvider``.
  - Audio callback thread: managed by sounddevice inside ``SounddeviceRecorder``.
  - Worker thread: single-slot ``ThreadPoolExecutor`` for ``_process_audio``.

Model inference only ever runs on the worker thread.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from local_flow.audio.recorder import RecorderConfig, SounddeviceRecorder
from local_flow.config import Config
from local_flow.errors import InvalidTransitionError, LocalFlowError
from local_flow.notifications import ConsoleNotifier, NotificationEvent
from local_flow.state import AppState, StateMachine
from local_flow.text.pipeline import process_transcript
from local_flow.transcription.factory import create_backend

if TYPE_CHECKING:
    from local_flow.audio.recorder import RecordedAudio
    from local_flow.input.hotkeys import HotkeyProvider
    from local_flow.input.paste import TextInserter
    from local_flow.transcription.base import TranscriptionBackend

logger = logging.getLogger(__name__)


class AppController:
    """Top-level push-to-talk controller."""

    def __init__(
        self,
        config: Config,
        *,
        hotkey_provider: HotkeyProvider | None = None,
        backend: TranscriptionBackend | None = None,
        text_inserter: TextInserter | None = None,
        copy_only: bool = False,
    ) -> None:
        self._config = config
        self._copy_only = copy_only
        self._state_machine = StateMachine()
        self._notifier = ConsoleNotifier(
            enabled=config.notifications.console_enabled,
        )
        recorder_cfg = RecorderConfig(
            device=config.audio.device or None,
            sample_rate=config.audio.sample_rate,
            min_duration_ms=config.audio.minimum_duration_ms,
            max_duration_seconds=float(config.audio.maximum_duration_seconds),
            vad_threshold=config.audio.vad_threshold,
            leading_padding_ms=config.audio.leading_padding_ms,
            trailing_padding_ms=config.audio.trailing_padding_ms,
        )
        self._recorder = SounddeviceRecorder(recorder_cfg)
        self._backend: TranscriptionBackend = backend or create_backend(config)
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._shutdown_event = threading.Event()
        self._pending_future: Future[None] | None = None

        if text_inserter is None:
            from local_flow.input.paste import build_text_inserter

            self._inserter: TextInserter = build_text_inserter(config.paste)
        else:
            self._inserter = text_inserter

        if hotkey_provider is None:
            from local_flow.input.hotkeys import PynputHotkeyProvider

            self._hotkeys: HotkeyProvider = PynputHotkeyProvider(config.hotkeys)
        else:
            self._hotkeys = hotkey_provider

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Load the model, start hotkey listening, block until shutdown."""
        self._backend.load()
        self._state_machine.transition(AppState.IDLE)

        self._hotkeys.start(
            on_press=self._on_press,
            on_release=self._on_release,
            on_cancel=self._on_cancel,
        )
        self._notifier.notify(NotificationEvent.READY)

        # Install signal handlers only when running on the main thread — signal
        # registration raises ValueError from non-main threads (e.g. in tests).
        in_main_thread = threading.current_thread() is threading.main_thread()
        original_sigint: Any = None
        original_sigterm: Any = None

        if in_main_thread:
            original_sigint = signal.getsignal(signal.SIGINT)
            original_sigterm = signal.getsignal(signal.SIGTERM)

            def _handle_signal(signum: int, frame: Any) -> None:
                self.shutdown()

            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)

        try:
            self._shutdown_event.wait()
        finally:
            if in_main_thread:
                signal.signal(signal.SIGINT, original_sigint)
                signal.signal(signal.SIGTERM, original_sigterm)

    def shutdown(self) -> None:
        """Gracefully stop everything (§22 order)."""
        with contextlib.suppress(InvalidTransitionError):
            self._state_machine.transition(AppState.STOPPING)

        # 1. Stop hotkeys.
        self._hotkeys.stop()

        # 2. If recording, cancel.
        with contextlib.suppress(Exception):
            self._recorder.cancel()

        # 3. Cancel queued worker and wait.
        self._executor.shutdown(wait=True, cancel_futures=True)

        # 4 & 5. Close backend (recorder already cancelled above).
        with contextlib.suppress(Exception):
            self._backend.close()

        # 6. Signal the main thread to exit.
        self._shutdown_event.set()

    # ------------------------------------------------------------------
    # Hotkey callbacks (run on pynput listener thread)
    # ------------------------------------------------------------------

    def _on_press(self) -> None:
        state = self._state_machine.state
        if state is AppState.IDLE:
            try:
                self._state_machine.transition(AppState.RECORDING)
            except InvalidTransitionError:
                return
            try:
                self._recorder.start()
            except LocalFlowError as exc:
                logger.error("could not start recorder: %s", exc)
                self._state_machine.transition(AppState.ERROR)
                self._notifier.notify(NotificationEvent.ERROR)
                self._state_machine.transition(AppState.IDLE)
                return
            self._notifier.notify(NotificationEvent.RECORDING)
        elif state in (
            AppState.PROCESSING_AUDIO,
            AppState.TRANSCRIBING,
            AppState.CLEANING,
            AppState.PASTING,
        ):
            self._notifier.notify(NotificationEvent.BUSY)

    def _on_release(self) -> None:
        try:
            self._state_machine.transition(AppState.PROCESSING_AUDIO)
        except InvalidTransitionError:
            return

        try:
            audio = self._recorder.stop()
        except LocalFlowError as exc:
            logger.warning("recorder stop error: %s", exc)
            self._notifier.notify(NotificationEvent.CANCELLED)
            with contextlib.suppress(InvalidTransitionError):
                self._state_machine.transition(AppState.CANCELLED)
                self._state_machine.transition(AppState.IDLE)
            return

        self._pending_future = self._executor.submit(self._process_audio, audio)

    def _on_cancel(self) -> None:
        state = self._state_machine.state
        # Cancel is valid from RECORDING, PROCESSING_AUDIO, and TRANSCRIBING.
        if state not in (AppState.RECORDING, AppState.PROCESSING_AUDIO, AppState.TRANSCRIBING):
            return
        with contextlib.suppress(InvalidTransitionError):
            self._state_machine.transition(AppState.CANCELLED)
        # Discard in-flight audio; cancel any queued worker.
        self._recorder.cancel()
        if self._pending_future is not None:
            self._pending_future.cancel()
        self._notifier.notify(NotificationEvent.CANCELLED)
        with contextlib.suppress(InvalidTransitionError):
            self._state_machine.transition(AppState.IDLE)

    # ------------------------------------------------------------------
    # Worker (runs on ThreadPoolExecutor thread — never on listener/callback)
    # ------------------------------------------------------------------

    def _process_audio(self, audio: RecordedAudio) -> None:
        try:
            self._state_machine.transition(AppState.TRANSCRIBING)
        except InvalidTransitionError:
            return

        self._notifier.notify(
            NotificationEvent.TRANSCRIBING,
            duration_seconds=audio.duration_seconds,
        )

        try:
            result = self._backend.transcribe(audio.samples, audio.sample_rate)
        except LocalFlowError as exc:
            logger.error("transcription failed: %s", exc)
            with contextlib.suppress(InvalidTransitionError):
                self._state_machine.transition(AppState.ERROR)
                self._notifier.notify(NotificationEvent.ERROR)
                self._state_machine.transition(AppState.IDLE)
            return

        # Deterministic text processing (Phase 4): spoken commands, technical
        # token protection, filler handling, normalization. A beginning-of-
        # transcript "cancel" command discards the dictation entirely.
        pipeline = process_transcript(
            result.text,
            mode=self._config.app.mode,
            spoken_commands_enabled=self._config.text.spoken_commands_enabled,
        )

        if pipeline.cancelled:
            with contextlib.suppress(InvalidTransitionError):
                self._state_machine.transition(AppState.CANCELLED)
                self._notifier.notify(NotificationEvent.CANCELLED)
                self._state_machine.transition(AppState.IDLE)
            return

        if not pipeline.text:
            # Empty transcript after processing — nothing to paste. Skip the
            # PASTING state and return to IDLE without inserting.
            self._notifier.notify(NotificationEvent.CANCELLED)
            with contextlib.suppress(InvalidTransitionError):
                self._state_machine.transition(AppState.PASTING)
                self._state_machine.transition(AppState.IDLE)
            return

        # Insert the text at the cursor (Phase 5). Insertion never presses
        # Enter; a paste failure leaves the transcript on the clipboard.
        try:
            self._state_machine.transition(AppState.PASTING)
        except InvalidTransitionError:
            return

        insertion = self._inserter.insert(pipeline.text, copy_only=self._copy_only)

        if insertion.error is not None:
            # §16 paste failure: transcript is on the clipboard; notify without
            # revealing content, then return to idle (no arbitrary retry).
            logger.warning("paste failed; transcript left on clipboard")
            self._notifier.notify(NotificationEvent.ERROR)
        else:
            self._notifier.notify(
                NotificationEvent.SUCCESS,
                char_count=len(pipeline.text),
            )

        with contextlib.suppress(InvalidTransitionError):
            self._state_machine.transition(AppState.IDLE)
