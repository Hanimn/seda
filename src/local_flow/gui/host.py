"""macOS GUI host that owns the main thread (ADR-0001).

The host is the AppKit-owning side of the main-thread inversion. On macOS with
AppKit available it installs signal handlers, calls ``controller.start()``, and
blocks in ``NSApplication.run()`` while the overlay draws — with
``controller.shutdown()`` invoked on quit/signal.

**Fail-open is the hard invariant** (epic #15): this module never lets a missing
or broken AppKit affect dictation. :func:`run_with_overlay` returns ``False``
whenever the overlay could not take over the main thread — non-macOS or an
AppKit import/setup failure — and the caller (:func:`local_flow.cli.run`) then
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

if TYPE_CHECKING:
    from local_flow.app import AppController

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
        _panel: Any,
        _view: Any,
        _timer_holder: dict[str, Any],
    ) -> None:
        self.show = show
        self.hide = hide
        self.dispatch_main = dispatch_main
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
            level = max(0.0, min(1.0, float(getattr(self, "_level", 0.0)) * 4.0))
            self._frame = getattr(self, "_frame", 0) + 1
            phase = self._frame / 60.0  # seconds-ish, for the jitter animation

            # Design: symmetric "mirror" EQ bars — each bar grows up AND down from
            # the vertical center line. Center bars are weighted taller; a little
            # per-bar jitter keeps it lively (mockup #2).
            NSColor.whiteColor().colorWithAlphaComponent_(0.92).set()
            width, gap = 6.0, 3.0
            cluster = _BARS * width + (_BARS - 1) * gap
            start_x = (bounds.size.width - cluster) / 2.0
            cy = bounds.size.height / 2.0
            for i in range(_BARS):
                # Triangular weight: 1.0 at center, tapering to ~0.35 at edges.
                weight = 0.35 + 0.65 * (1.0 - abs(i - (_BARS - 1) / 2.0) / (_BARS / 2.0))
                jitter = 0.7 + 0.3 * math.sin(phase * 9.0 + i)
                half = max(2.0, (bounds.size.height * 0.42) * level * weight * jitter)
                x = start_x + i * (width + gap)
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(x, cy - half, width, half * 2.0), width / 2.0, width / 2.0
                ).fill()

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
    # controller has NOT been started yet, so a retry is clean.
    from AppKit import NSApplication

    try:
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

    # Install SIGINT/SIGTERM on the main thread (Python only allows this on
    # main). The handler stops the controller, then the AppKit run loop, so the
    # main thread unblocks and the process exits cleanly (ADR-0001).
    def _handle_signal(_signum: int, _frame: Any) -> None:
        try:
            controller.shutdown()
        finally:
            app.stop_(None)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Warm the macOS Text Input Source machinery on the MAIN thread before the
    # pynput listener starts. pynput's darwin listener calls the Carbon TIS APIs
    # (TISCopyCurrentKeyboardInputSource in keycode_context) from its own thread;
    # once a full NSApplication owns the main run loop, initializing that TIS
    # context concurrently on the listener thread can abort the process
    # (SIGABRT). Priming it here on the main thread first avoids that race.
    _warm_input_source()

    controller.start()  # non-blocking setup (load model, start hotkeys, notify READY)
    app.run()  # blocks the main thread until app.stop_ is called


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
