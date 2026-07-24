"""macOS GUI host that owns the main thread (ADR-0001).

The host is the AppKit-owning side of the main-thread inversion. On macOS with
AppKit available it installs signal handlers, calls ``controller.start()``, and
blocks in ``NSApplication.run()`` while the overlay draws — with
``controller.shutdown()`` invoked on quit/signal.

**Fail-open is the hard invariant** (epic #15): this module never lets a missing
or broken AppKit affect dictation. :func:`run_with_overlay` returns ``False``
whenever the overlay could not take over the main thread — non-macOS or an
AppKit import/setup failure — and the caller (:func:`seda.cli.run`) then
runs the controller's own blocking ``run()``, which is exactly today's behavior.

The AppKit recipe (borderless non-activating ``NSPanel`` shown via
``orderFrontRegardless`` so it never steals focus, ``NSStatusWindowLevel`` +
all-Spaces/full-screen collection behavior, a layer-backed level-meter view
redrawn by a main-thread ``NSTimer`` reading ``controller.latest_level``) was
verified end-to-end by the #15 prototype
(``docs/research/nspanel-nonactivating-float-recipe.md``).
"""

from __future__ import annotations

import logging
import signal
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from seda.notifications import HudMode

if TYPE_CHECKING:
    from seda.app import AppController

logger = logging.getLogger(__name__)


class Overlay:
    """Handle to a live overlay: main-thread ``show``/``hide`` + a dispatcher.

    Constructed by :func:`build_overlay` on macOS. The show/hide callables and
    ``dispatch_main`` are handed to an ``OverlayNotifier`` (assembled by
    ``cli.run``) so notification events drive the panel on the main thread.
    """

    def __init__(
        self,
        *,
        show: Callable[[], None],
        hide: Callable[[], None],
        dispatch_main: Callable[[Callable[[], None]], None],
        set_mode: Callable[[HudMode], None] | None = None,
        teardown: Callable[[], None] | None = None,
        _panel: Any,
        _view: Any,
        _timer_holder: dict[str, Any],
    ) -> None:
        self.show = show
        self.hide = hide
        self.dispatch_main = dispatch_main
        # set_mode(HudMode.LISTENING|BUSY) switches the waveform view's draw mode
        # on the main thread (ADR-0006). Defaults to a no-op for hand-built test
        # overlays.
        self.set_mode = set_mode if set_mode is not None else (lambda _mode: None)
        # Deterministic teardown: stop the redraw timer and order out + close the
        # panel so the HUD never lingers after the app exits (normal, signal, or
        # crash). Defaults to a no-op for hand-built test overlays.
        self.teardown = teardown if teardown is not None else (lambda: None)
        self._panel = _panel
        self._view = _view
        self._timer_holder = _timer_holder


def build_overlay(level_source: Callable[[], float]) -> Overlay:
    """Build the macOS overlay panel (imports AppKit — macOS only).

    *level_source* is polled on the main thread to drive the level meter (it is
    ``AppController.latest_level``). Raises ``ImportError``/``OSError`` if AppKit
    is unavailable, which the caller turns into a fail-open fallback.
    """
    import objc
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBackingStoreBuffered,
        NSBezierPath,
        NSColor,
        NSPanel,
        NSScreen,
        NSStatusWindowLevel,
        NSView,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowCollectionBehaviorIgnoresCycle,
        NSWindowCollectionBehaviorStationary,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
    )
    from Foundation import NSMakeRect, NSObject, NSTimer

    _PANEL_W, _PANEL_H = 160.0, 48.0
    _BARS = 9

    class WaveformView(NSView):  # type: ignore[misc]
        def initWithFrame_(self, frame: Any) -> Any:  # noqa: N802
            self = objc.super(WaveformView, self).initWithFrame_(frame)
            if self is None:
                return None
            self._level = 0.0
            self._frame = 0  # advances each redraw, for subtle per-bar motion
            self._mode = HudMode.LISTENING  # LISTENING (level bars) | BUSY (pulse)
            return self

        def needsPanelToBecomeKey(self) -> bool:  # noqa: N802 - never take keyboard focus
            return False

        def isOpaque(self) -> bool:  # noqa: N802
            return False

        def drawRect_(self, _dirty: Any) -> None:  # noqa: N802
            import math

            bounds = self.bounds()
            NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.55).set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 8.0, 8.0).fill()
            self._frame = getattr(self, "_frame", 0) + 1

            # Shared bar geometry — identical in both modes so the listening →
            # busy switch reads as one widget changing state, not a swap.
            width, gap = 6.0, 3.0
            cluster = _BARS * width + (_BARS - 1) * gap
            start_x = (bounds.size.width - cluster) / 2.0
            cy = bounds.size.height / 2.0
            span = bounds.size.height * 0.42  # half-height at full amplitude

            def _bar(i: int, half: float, alpha: float) -> None:
                NSColor.whiteColor().colorWithAlphaComponent_(alpha).set()
                x = start_x + i * (width + gap)
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(x, cy - half, width, half * 2.0), width / 2.0, width / 2.0
                ).fill()

            if getattr(self, "_mode", HudMode.LISTENING) == HudMode.BUSY:
                # Busy: a bright band sweeps L→R; each bar peaks as it passes,
                # over a calm baseline so bars never fully vanish. Purely
                # time-driven — no level input (the mic is stopped by now).
                phase = self._frame / 60.0
                speed = 3.2  # bars/sec the head travels
                head = (phase * speed) % (_BARS + 3)  # +3 = gap between sweeps
                for i in range(_BARS):
                    d = i - head
                    bump = math.exp(-(d * d) / 2.2)
                    h = 0.18 + 0.62 * bump  # baseline + travelling swell
                    _bar(i, max(2.0, span * h), 0.45 + 0.47 * bump)
                return

            # Listening: symmetric "mirror" EQ bars driven by the mic level.
            # Perceptual mapping (spec Part 1): a noise-floor gate + sqrt expand
            # the low end so quiet speech visibly moves the bars; raw/instant,
            # no smoothing. GATE/GAIN are tune-by-eye knobs.
            _GATE = 0.006
            _GAIN = 2.6
            rms = float(getattr(self, "_level", 0.0))
            level = max(0.0, min(1.0, math.sqrt(max(0.0, rms - _GATE)) * _GAIN))
            phase = self._frame / 60.0  # seconds-ish, for the jitter animation
            for i in range(_BARS):
                # Triangular weight: 1.0 at center, tapering to ~0.35 at edges.
                weight = 0.35 + 0.65 * (1.0 - abs(i - (_BARS - 1) / 2.0) / (_BARS / 2.0))
                # Jitter amplitude scales with level so it vanishes at silence —
                # a resting HUD is flat and still, not a shimmering floor.
                jitter = 1.0 + 0.3 * level * math.sin(phase * 9.0 + i)
                half = max(2.0, span * level * weight * jitter)
                _bar(i, half, 0.92)

    class OverlayPanel(NSPanel):  # type: ignore[misc]
        def canBecomeKeyWindow(self) -> bool:  # noqa: N802 - never steal key
            return False

        def canBecomeMainWindow(self) -> bool:  # noqa: N802 - never steal main
            return False

    class _MainThreadRunner(NSObject):  # type: ignore[misc]
        # Runs an arbitrary Python callable on the main thread via
        # performSelectorOnMainThread_ — the dependency-free GCD-to-main path
        # (pyobjc's libdispatch bindings are a separate package we don't pull).
        def runHolder_(self, holder: Any) -> None:  # noqa: N802
            holder["fn"]()

    runner = _MainThreadRunner.alloc().init()

    # Accessory policy: no Dock icon / menu bar, cannot grab activation.
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    rect = NSMakeRect(0, 0, _PANEL_W, _PANEL_H)
    style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
    panel = OverlayPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, NSBackingStoreBuffered, False
    )
    panel.setLevel_(NSStatusWindowLevel)
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorFullScreenAuxiliary
        | NSWindowCollectionBehaviorIgnoresCycle
    )
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setHasShadow_(False)
    panel.setIgnoresMouseEvents_(True)  # click-through HUD

    # Bottom-center on the main screen.
    screen = NSScreen.mainScreen().frame()
    panel.setFrameOrigin_(((screen.size.width - _PANEL_W) / 2.0, 80.0))

    view = WaveformView.alloc().initWithFrame_(rect)
    view.setWantsLayer_(True)
    panel.setContentView_(view)

    # The redraw timer lives in a holder so show()/hide() can start/stop it; it
    # only runs while the panel is visible (no idle 60 Hz churn).
    timer_holder: dict[str, Any] = {"timer": None}

    def _tick(_timer: Any) -> None:
        view._level = level_source()
        view.setNeedsDisplay_(True)

    def _show() -> None:
        panel.orderFrontRegardless()  # NOT makeKeyAndOrderFront_ / NSApp.activate
        if timer_holder["timer"] is None:
            timer_holder["timer"] = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                1.0 / 60.0, True, _tick
            )

    def _hide() -> None:
        timer = timer_holder["timer"]
        if timer is not None:
            timer.invalidate()
            timer_holder["timer"] = None
        panel.orderOut_(None)

    def _set_mode(mode: HudMode) -> None:
        # Switch the waveform view's draw mode and force an immediate redraw.
        # Runs on the main thread (OverlayNotifier marshals it), same as the
        # redraw timer, so there is no cross-thread access to view state.
        view._mode = mode
        view.setNeedsDisplay_(True)

    def _teardown() -> None:
        # Deterministic teardown so the HUD never lingers after the app exits.
        # Stop the redraw timer, then order out AND close the panel — closing
        # releases the window so it does not survive as an orphaned window even
        # briefly. Idempotent and fail-open: each step is guarded so a
        # double-teardown or an already-closed panel is a harmless no-op, and a
        # failure here must never block shutdown. Must run on the main thread.
        timer = timer_holder["timer"]
        if timer is not None:
            try:
                timer.invalidate()
            except Exception:  # noqa: BLE001
                logger.debug("overlay timer invalidate failed", exc_info=True)
            timer_holder["timer"] = None
        try:
            panel.orderOut_(None)
            panel.close()
        except Exception:  # noqa: BLE001
            logger.debug("overlay panel teardown failed", exc_info=True)

    def _dispatch_main(fn: Callable[[], None]) -> None:
        # Marshal a call onto the main thread/run loop (ADR-0001/#17). notify()
        # runs on the hotkey-listener / worker threads; AppKit must run on main.
        runner.performSelectorOnMainThread_withObject_waitUntilDone_(
            "runHolder:", {"fn": fn}, False
        )

    return Overlay(
        show=_show,
        hide=_hide,
        dispatch_main=_dispatch_main,
        set_mode=_set_mode,
        teardown=_teardown,
        _panel=panel,
        _view=view,
        _timer_holder=timer_holder,
    )


def run_with_overlay(
    controller: AppController,
    *,
    build: Callable[[Callable[[], float]], Overlay] | None = None,
    register_overlay: Callable[[Overlay], None] | None = None,
    platform: str | None = None,
) -> bool:
    """Run *controller* under a macOS GUI host that owns the main thread.

    Returns ``True`` only if the GUI host took over the main thread and ran the
    controller to shutdown; ``False`` (fail-open) when the overlay is
    unavailable — non-macOS or an AppKit import/setup failure — so the caller
    can fall back to ``controller.run()``.

    ``build`` constructs the overlay from a level source (defaults to
    :func:`build_overlay`; injectable for tests). ``register_overlay`` is called
    with the built :class:`Overlay` before the run loop starts, so ``cli.run``
    can wire its show/hide into the controller's notifier fan-out.
    ``platform`` is injectable (defaults to :data:`sys.platform`).
    """
    plat = platform if platform is not None else sys.platform
    if plat != "darwin":
        return False

    build_fn = build if build is not None else build_overlay

    # Fail-open covers ONLY acquiring AppKit + building the panel. If that
    # fails (non-macOS AppKit, import error, panel build error), the overlay is
    # unavailable and the caller safely falls back to controller.run() — the
    # controller has NOT been started yet, so a retry is clean. The AppKit import
    # is INSIDE the try: on a non-macOS host (or a broken pyobjc install) it
    # raises ModuleNotFoundError, which must fail open rather than propagate.
    try:
        from AppKit import NSApplication

        app = NSApplication.sharedApplication()
        overlay = build_fn(lambda: controller.latest_level)
    except (ImportError, OSError) as exc:
        logger.info("overlay unavailable, falling back to terminal mode: %s", exc)
        return False
    except Exception:  # noqa: BLE001
        logger.warning("overlay setup failed, falling back to terminal mode", exc_info=True)
        return False

    # Past this point the GUI host OWNS the run: it installs signals, starts the
    # controller, and blocks in NSApp.run(). A failure here (e.g. the backend
    # failing to load in controller.start()) is the controller's own error, not
    # an overlay problem — it must NOT fall back to controller.run() (that would
    # re-run start() and fail again). Let it propagate, exactly as controller.run()
    # would surface the same error on the terminal path.
    _run_appkit_host(controller, app, overlay, register_overlay)
    return True


def _run_appkit_host(
    controller: AppController,
    app: Any,
    overlay: Overlay,
    register_overlay: Callable[[Overlay], None] | None,
) -> None:
    """Own the main thread with AppKit and drive *controller* (macOS only).

    Assumes AppKit is available and *overlay* is already built (the fail-open
    boundary is in :func:`run_with_overlay`). Installs signal handlers, starts
    the controller, and blocks in ``NSApplication.run()``.
    """
    if register_overlay is not None:
        register_overlay(overlay)

    # --- Signal handling under a Cocoa run loop --------------------------------
    # NSApplication.run() blocks in native code and does NOT yield to Python's
    # signal machinery, so a signal.signal() handler installed here would never
    # fire while we are blocked in run() — SIGINT/SIGTERM would be ignored and
    # the app could only be force-killed (which then skips the graceful overlay
    # teardown below, leaving the HUD on screen). Fix: the signal handler only
    # sets a flag; a repeating "pump" timer wakes the run loop ~10x/second so
    # the interpreter regains control and runs the handler, then performs the
    # actual shutdown and stops the loop.
    stop_requested = {"flag": False}

    def _request_stop(_signum: int, _frame: Any) -> None:
        # Runs on the main thread the next time the interpreter gets control
        # (nudged by the pump timer). Keep it tiny — just record the request.
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    def _pump(_timer: Any) -> None:
        # Servicing point: pending Python signal handlers have run by the time we
        # get here, so the flag reflects any SIGINT/SIGTERM. On a stop request,
        # shut the controller down, stop the run loop, and post a dummy event so
        # stop_ takes effect immediately (stop_ is only checked between events;
        # without an event the loop can idle-wait and delay exit).
        if not stop_requested["flag"]:
            return
        try:
            controller.shutdown()
        finally:
            app.stop_(None)
            _post_wakeup_event(app)

    pump = _schedule_pump(0.1, _pump)

    # Warm the macOS native machinery pynput's darwin listener touches on its
    # OWN thread, doing it here on the MAIN thread first so those one-time,
    # non-thread-safe initializations happen uncontended before the listener
    # starts racing the NSApplication we just brought up. Two distinct hazards:
    #   * the Carbon Text Input Source context (keycode_context / TIS) — a
    #     concurrent first-init on the listener thread can abort the process
    #     (SIGABRT);
    #   * HIServices.AXIsProcessTrusted — pynput calls this at the very top of
    #     its _run(); its first access goes through PyObjC's lazy-import
    #     __getattr__, whose funcmap.pop() is not thread-safe, so racing the
    #     main thread's framework loads raises KeyError: 'AXIsProcessTrusted'.
    # Resolving each once on main caches it so the listener hits a plain,
    # already-populated attribute and never re-enters the racy path.
    _warm_input_source()
    _warm_accessibility_trust()

    # Tear the overlay down on EVERY exit from the run loop — normal
    # signal-driven stop, and any exception propagating out of controller.start()
    # or app.run() (a crash). Without this the HUD panel can linger on screen
    # after the app exits, because controller.shutdown() does not touch the
    # overlay and process-exit reclamation is not immediate/guaranteed. We are on
    # the main thread here, so calling teardown directly is safe. teardown() is
    # itself fail-open, but guard again so a teardown error can never mask the
    # original crash.
    try:
        controller.start()  # non-blocking setup (load model, start hotkeys, notify READY)
        app.run()  # blocks the main thread until app.stop_ is called
    finally:
        # Stop the pump timer first (it references app/controller), then tear the
        # overlay down. Both guarded so a cleanup error never masks a crash.
        try:
            pump.invalidate()
        except Exception:  # noqa: BLE001
            logger.debug("pump timer invalidate failed", exc_info=True)
        try:
            overlay.teardown()
        except Exception:  # noqa: BLE001
            logger.warning("overlay teardown failed during shutdown", exc_info=True)


def _schedule_pump(interval: float, callback: Callable[[Any], None]) -> Any:
    """Schedule a repeating main-run-loop timer that services Python signals.

    Lazily imports Foundation (macOS only) so this module stays importable on
    non-macOS hosts and the timer creation can be monkeypatched in tests.
    Returns the timer (so it can be invalidated on shutdown).
    """
    from Foundation import NSTimer

    return NSTimer.scheduledTimerWithTimeInterval_repeats_block_(interval, True, callback)


def _post_wakeup_event(app: Any) -> None:
    """Post a no-op event so a pending ``app.stop_`` takes effect promptly.

    ``NSApplication.stop_`` only ends the run loop when it is next checked
    *between events*; if the loop is idle-waiting for input it may not notice
    until some event arrives. Posting a dummy application-defined event at the
    front of the queue guarantees the loop wakes and observes the stop. Best-
    effort: never raise into shutdown.
    """
    try:
        from AppKit import NSApplicationDefined, NSEvent
        from Foundation import NSPoint

        event = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(  # noqa: E501
            NSApplicationDefined, NSPoint(0, 0), 0, 0.0, 0, None, 0, 0, 0
        )
        app.postEvent_atStart_(event, True)
    except Exception:  # noqa: BLE001
        logger.debug("could not post wakeup event", exc_info=True)


def _warm_input_source() -> None:
    """Prime pynput's Carbon keycode/Text-Input-Source context on the main thread.

    Best-effort: any failure is swallowed (the listener would just initialize it
    lazily as before). See :func:`_run_appkit_host` for why this matters.
    """
    try:
        from pynput._util.darwin import keycode_context

        with keycode_context():
            pass
    except Exception:  # noqa: BLE001
        logger.debug("could not pre-warm keyboard input source", exc_info=True)


def _warm_accessibility_trust() -> None:
    """Prime ``HIServices.AXIsProcessTrusted`` on the main thread.

    pynput's darwin listener calls ``HIServices.AXIsProcessTrusted()`` as the
    first line of its ``_run()`` (on its own thread). That first access resolves
    the symbol through PyObjC's lazy-import ``__getattr__``, whose
    ``funcmap.pop(name)`` mutates shared, non-thread-safe state; racing the main
    thread's framework loads (from the ``NSApplication`` we just started) can
    raise ``KeyError: 'AXIsProcessTrusted'`` on the listener thread. Resolving it
    once here caches it on the module so the listener hits a plain attribute and
    never re-enters the racy path.

    Best-effort: any failure is swallowed (the listener would resolve it lazily
    as before). See :func:`_run_appkit_host` for why this matters.
    """
    try:
        import HIServices

        HIServices.AXIsProcessTrusted()
    except Exception:  # noqa: BLE001
        logger.debug("could not pre-warm accessibility-trust check", exc_info=True)
