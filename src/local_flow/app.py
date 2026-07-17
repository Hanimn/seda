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
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from local_flow.audio.recorder import RecorderConfig, SounddeviceRecorder
from local_flow.cleanup.base import CleanupCounters
from local_flow.config import Config
from local_flow.errors import (
    CleanupError,
    InvalidTransitionError,
    LocalFlowError,
)
from local_flow.notifications import ConsoleNotifier, NotificationEvent, Notifier
from local_flow.state import AppState, StateMachine
from local_flow.text.pipeline import (
    PipelineResult,
    finalize_after_cleanup,
    process_transcript,
)
from local_flow.text.technical_tokens import ProtectionError
from local_flow.transcription.factory import create_backend

if TYPE_CHECKING:
    from local_flow.audio.recorder import RecordedAudio
    from local_flow.cleanup.base import CleanupProvider
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
        cleanup_provider: CleanupProvider | None = None,
        notifier: Notifier | None = None,
        copy_only: bool = False,
        cleanup_enabled: bool | None = None,
    ) -> None:
        self._config = config
        self._copy_only = copy_only
        # Cleanup is on only when config enables it AND it isn't force-disabled
        # (e.g. `run --no-cleanup`). ``cleanup_enabled`` overrides the config
        # flag when provided.
        if cleanup_enabled is None:
            self._cleanup_enabled = config.cleanup.enabled
        else:
            self._cleanup_enabled = cleanup_enabled and config.cleanup.enabled
        self._state_machine = StateMachine()
        # A notifier can be injected (ADR-0003's fan-out will use this seam);
        # default to the plain console notifier so behavior is unchanged.
        self._notifier: Notifier = notifier or ConsoleNotifier(
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

        # Aggregate, content-free cleanup counters (§28).
        self._cleanup_counters = CleanupCounters()

        if text_inserter is None:
            from local_flow.input.paste import build_text_inserter

            self._inserter: TextInserter = build_text_inserter(config.paste)
        else:
            self._inserter = text_inserter

        # Build the cleanup provider only when cleanup is actually enabled, so a
        # plain dictation setup never constructs an HTTP provider.
        self._cleanup_provider: CleanupProvider | None
        if cleanup_provider is not None:
            self._cleanup_provider = cleanup_provider
        elif self._cleanup_enabled:
            from local_flow.cleanup.factory import create_cleanup_provider

            self._cleanup_provider = create_cleanup_provider(config)
        else:
            self._cleanup_provider = None

        if hotkey_provider is None:
            from local_flow.input.hotkeys import PynputHotkeyProvider

            self._hotkeys: HotkeyProvider = PynputHotkeyProvider(config.hotkeys)
        else:
            self._hotkeys = hotkey_provider

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def latest_level(self) -> float:
        """Most recent per-block audio RMS level (0.0 when idle).

        A read-only pass-through to the recorder (ADR-0002), so a GUI host
        (ADR-0001) can drive the overlay's level meter without reaching into
        the controller's internals.
        """
        return self._recorder.latest_level

    def start(self) -> None:
        """Load the model and start hotkey listening — the non-blocking setup.

        This is the setup half of :meth:`run`, split out so a GUI host that
        owns the main thread (macOS overlay, ADR-0001) can drive the controller
        without the blocking wait: the host calls ``start()`` then, on quit,
        :meth:`shutdown`. On the fallback path ``run()`` calls ``start()`` and
        then blocks on the shutdown event exactly as before.
        """
        self._backend.load()
        # A shutdown (e.g. Ctrl-C) can race in *during* the slow load() — the
        # signal handler runs on the main thread and moves the state to
        # STOPPING. If so, STARTING->IDLE is no longer valid; bail cleanly
        # rather than raising, and don't start hotkeys on an app that's already
        # stopping.
        try:
            self._state_machine.transition(AppState.IDLE)
        except InvalidTransitionError:
            logger.info("shutdown raced with startup; aborting start()")
            return

        self._hotkeys.start(
            on_press=self._on_press,
            on_release=self._on_release,
            on_cancel=self._on_cancel,
        )
        self._notifier.notify(NotificationEvent.READY)

    def run(self) -> None:
        """Load the model, start hotkey listening, block until shutdown."""
        self.start()

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
        cycle_start = time.monotonic()
        try:
            self._state_machine.transition(AppState.TRANSCRIBING)
        except InvalidTransitionError:
            return

        self._notifier.notify(
            NotificationEvent.TRANSCRIBING,
            duration_seconds=audio.duration_seconds,
        )

        t0 = time.monotonic()
        try:
            result = self._backend.transcribe(audio.samples, audio.sample_rate)
        except LocalFlowError as exc:
            logger.error("transcription failed: %s", exc)
            with contextlib.suppress(InvalidTransitionError):
                self._state_machine.transition(AppState.ERROR)
                self._notifier.notify(NotificationEvent.ERROR)
                self._state_machine.transition(AppState.IDLE)
            return
        t_transcribe = time.monotonic() - t0
        # Aggregate timing — available in debug mode (§25 "Expose aggregate
        # timing in debug mode"); never contains transcript content.
        logger.debug(
            "perf: audio=%.2fs transcribe=%.2fs chars=%d",
            audio.duration_seconds,
            t_transcribe,
            len(result.text),
        )

        # Deterministic text processing (Phase 4): spoken commands, technical
        # token protection, filler handling, normalization. A beginning-of-
        # transcript "cancel" command discards the dictation entirely.
        t0 = time.monotonic()
        pipeline = process_transcript(
            result.text,
            mode=self._config.app.mode,
            spoken_commands_enabled=self._config.text.spoken_commands_enabled,
        )
        logger.debug(
            "perf: pipeline=%.3fs commands=%d", time.monotonic() - t0, pipeline.commands_applied
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

        # Optional LLM cleanup (Phase 6). Fail-open: any error or rejected
        # output falls back to the deterministic transcript so a successful
        # transcription is never lost. Bypassed entirely in literal mode.
        t0 = time.monotonic()
        final_text = self._maybe_clean(pipeline)
        logger.debug("perf: cleanup=%.3fs", time.monotonic() - t0)

        # Insert the text at the cursor (Phase 5). Insertion never presses
        # Enter; a paste failure leaves the transcript on the clipboard.
        try:
            self._state_machine.transition(AppState.PASTING)
        except InvalidTransitionError:
            return

        t0 = time.monotonic()
        insertion = self._inserter.insert(final_text, copy_only=self._copy_only)
        t_paste = time.monotonic() - t0

        if insertion.error is not None:
            # §16 paste failure: transcript is on the clipboard; notify without
            # revealing content, then return to idle (no arbitrary retry).
            logger.warning("paste failed; transcript left on clipboard")
            self._notifier.notify(NotificationEvent.ERROR)
        else:
            self._notifier.notify(
                NotificationEvent.SUCCESS,
                char_count=len(final_text),
            )
            logger.debug(
                "perf: paste=%.3fs total=%.2fs",
                t_paste,
                time.monotonic() - cycle_start,
            )

        with contextlib.suppress(InvalidTransitionError):
            self._state_machine.transition(AppState.IDLE)

    def _maybe_clean(self, pipeline: PipelineResult) -> str:
        """Run optional LLM cleanup, returning the text to paste.

        Fail-open (§15): returns the cleaned text only when cleanup is enabled,
        the mode is not literal, the provider succeeds, and the output passes
        validation; otherwise returns the deterministic ``pipeline.text``. Only
        aggregate, content-free metrics are logged (§21, §26).
        """
        if (
            not self._cleanup_enabled
            or self._cleanup_provider is None
            or pipeline.effective_mode == "literal"
        ):
            return pipeline.text

        # Enter CLEANING; if the transition is illegal (e.g. shutting down),
        # skip cleanup rather than crash.
        try:
            self._state_machine.transition(AppState.CLEANING)
        except InvalidTransitionError:
            return pipeline.text

        from local_flow.cleanup.validation import ValidationReason, validate_cleanup

        protected = pipeline.protected_text
        registry = pipeline.token_registry

        try:
            cleaned = self._cleanup_provider.clean(
                protected,
                pipeline.effective_mode,
                self._config.text.custom_vocabulary,
            )
        except CleanupError:
            self._cleanup_counters.failed += 1
            logger.info("cleanup failed; using deterministic transcript")
            return pipeline.text

        reason = validate_cleanup(cleaned, protected, registry)
        if reason is ValidationReason.OK:
            try:
                final = finalize_after_cleanup(cleaned, registry)
            except ProtectionError:
                # Defensive: validation passed but restore still objected. Treat
                # as a validation failure so we fall back rather than raise.
                reason = ValidationReason.PLACEHOLDER_MISSING
                final = pipeline.text
        else:
            final = pipeline.text

        self._record_cleanup_metrics(protected, cleaned, reason)
        return final

    def _record_cleanup_metrics(self, protected: str, cleaned: str, reason: object) -> None:
        """Update counters and log aggregate, content-free cleanup metrics (§15)."""
        from local_flow.cleanup.base import CleanupMetrics
        from local_flow.cleanup.validation import (
            ValidationReason,
            edit_ratio,
            placeholder_count,
        )

        accepted = reason is ValidationReason.OK
        metrics = CleanupMetrics(
            input_chars=len(protected),
            output_chars=len(cleaned),
            edit_ratio=edit_ratio(protected, cleaned),
            placeholder_count=placeholder_count(cleaned),
            validation=reason.value if isinstance(reason, ValidationReason) else str(reason),
        )
        if accepted:
            self._cleanup_counters.succeeded += 1
        else:
            self._cleanup_counters.failed += 1
            self._cleanup_counters.validation_failures += 1
            logger.info("cleanup rejected (%s); using deterministic transcript", metrics.validation)
        # Aggregate metrics only — never the transcript or model output (§15).
        # Logged at DEBUG so they only appear when log_level is DEBUG (§25).
        logger.debug(
            "cleanup metrics: in_chars=%d out_chars=%d edit_ratio=%.2f placeholders=%d result=%s",
            metrics.input_chars,
            metrics.output_chars,
            metrics.edit_ratio,
            metrics.placeholder_count,
            metrics.validation,
        )
