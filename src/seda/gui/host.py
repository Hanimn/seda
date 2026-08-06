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

import contextlib
import logging
import signal
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from seda.notifications import (
    HUD_ACTIVE_PANEL_H,
    HUD_ACTIVE_PANEL_W,
    HUD_IDLE_PANEL_H,
    HUD_IDLE_PANEL_W,
    HUD_IDLE_PILL_H,
    HUD_IDLE_PILL_W,
    HudMode,
    hud_idle_shimmer,
    hud_redraw_hz,
    status_label,
    status_symbol,
)

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
        # set_mode(HudMode.IDLE|LISTENING|BUSY) switches the waveform view's draw
        # mode, resizes/recenters the panel, and re-arms the redraw timer at the
        # mode's rate, on the main thread (ADR-0006/ADR-0007). Defaults to a no-op
        # for hand-built test overlays.
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
            # Startup mode is IDLE — the persistent HUD shows at rest on READY
            # before any RECORDING (ADR-0007 §2). IDLE | LISTENING | BUSY.
            self._mode = HudMode.IDLE
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

            mode = getattr(self, "_mode", HudMode.IDLE)

            if mode == HudMode.IDLE:
                # Idle: a single compressed pill with a faint slow alpha breath
                # (the #56 winner). Keeps the bar cluster's horizontal silhouette
                # compressed to one element, so waking reads as the same widget
                # widening back into the 9 bars. Shimmer is a shared knob (ADR-0007
                # §5) so macOS + Windows breathe identically.
                alpha = hud_idle_shimmer(self._frame, HudMode.IDLE)
                NSColor.whiteColor().colorWithAlphaComponent_(alpha).set()
                pill_w, pill_h = float(HUD_IDLE_PILL_W), float(HUD_IDLE_PILL_H)
                cx = bounds.size.width / 2.0
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(cx - pill_w / 2.0, cy - pill_h / 2.0, pill_w, pill_h),
                    pill_h / 2.0,
                    pill_h / 2.0,
                ).fill()
                return

            if mode == HudMode.BUSY:
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

    # The redraw timer lives in a holder so show()/set_mode() can (re)arm it. The
    # HUD is persistent (ADR-0007), so the timer runs the whole session; its RATE
    # is throttled to ~10 Hz in IDLE and ~60 Hz in LISTENING/BUSY (ADR-0007 §5 —
    # a shared cross-platform policy) rather than started/stopped on show/hide.
    timer_holder: dict[str, Any] = {"timer": None}
    # The active panel's center stays fixed as it grows/shrinks (persistent-
    # companion spec): remember it so _set_mode can recenter the shrunk chip.
    _center_x = screen.size.width / 2.0
    _active_bottom_y = 80.0  # the active panel's y-origin (bottom-centre)

    def _interval_for(mode: HudMode) -> float:
        # One shared rate policy (ADR-0007 §5): seconds-per-frame from the shared Hz.
        return 1.0 / hud_redraw_hz(mode)

    def _arm_timer(mode: HudMode) -> None:
        # Invalidate any existing timer, then (re)schedule at the mode's rate.
        timer = timer_holder["timer"]
        if timer is not None:
            timer.invalidate()
        timer_holder["timer"] = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            _interval_for(mode), True, _tick
        )

    def _tick(_timer: Any) -> None:
        view._level = level_source()
        view.setNeedsDisplay_(True)

    def _show() -> None:
        panel.orderFrontRegardless()  # NOT makeKeyAndOrderFront_ / NSApp.activate
        if timer_holder["timer"] is None:
            # Seed at the startup mode's rate (IDLE); set_mode re-arms on each flip.
            _arm_timer(view._mode)

    def _hide() -> None:
        timer = timer_holder["timer"]
        if timer is not None:
            timer.invalidate()
            timer_holder["timer"] = None
        panel.orderOut_(None)

    def _set_mode(mode: HudMode) -> None:
        # Persistent-HUD mode flip (ADR-0007): switch the draw mode, resize +
        # recenter the panel for the idle/active footprint, re-arm the redraw timer
        # at the mode's rate (§5), and force an immediate redraw. Runs on the main
        # thread (OverlayNotifier marshals it), same as the timer — no cross-thread
        # access to view/panel state.
        view._mode = mode
        if mode is HudMode.IDLE:
            new_w, new_h = float(HUD_IDLE_PANEL_W), float(HUD_IDLE_PANEL_H)
        else:
            new_w, new_h = float(HUD_ACTIVE_PANEL_W), float(HUD_ACTIVE_PANEL_H)
        # Recenter on the fixed active-panel center (x) and its vertical midline (y).
        new_x = _center_x - new_w / 2.0
        new_y = _active_bottom_y + (float(HUD_ACTIVE_PANEL_H) - new_h) / 2.0
        panel.setFrame_display_(NSMakeRect(new_x, new_y, new_w, new_h), True)
        view.setFrame_(NSMakeRect(0.0, 0.0, new_w, new_h))
        _arm_timer(mode)
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

    Thin adapter over the shared :func:`seda.gui._hostloop.run_hosted` lifecycle
    skeleton (ADR-0009 §2): it supplies the macOS-specific ``supports`` gate
    (``darwin``), ``build`` factory (:func:`build_overlay`), and ``run_loop``
    (AppKit body). The shared helper owns the fail-open boundary and the
    ``-> bool`` contract; this function owns the AppKit loop and teardown.

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
    from seda.gui._hostloop import run_hosted

    build_fn = build if build is not None else build_overlay

    return run_hosted(
        controller,
        supports=lambda plat: plat == "darwin",
        build=build_fn,
        run_loop=_run_appkit_loop,
        register_overlay=register_overlay,
        platform=platform,
    )


def _run_appkit_loop(
    controller: AppController,
    overlay: Overlay,
    register_overlay: Callable[[Overlay], None] | None,
) -> None:
    """macOS ``run_loop`` body for :func:`run_hosted` (past the fail-open boundary).

    Acquires the shared ``NSApplication`` and drives the AppKit host. The AppKit
    import lives here (not in the fail-open try): by the time ``run_hosted`` calls
    this, ``build`` has already succeeded, which on macOS means AppKit imported
    cleanly inside :func:`build_overlay`. The live ``NSApplication`` handle stays
    inside this loop (ADR-0009 §2) so the hardware-validated
    :func:`_run_appkit_host` keeps its signature untouched.
    """
    from AppKit import NSApplication

    app = NSApplication.sharedApplication()
    _run_appkit_host(controller, app, overlay, register_overlay)


def run_with_menu_bar(
    controller: AppController,
    *,
    build: Callable[[Callable[[], float]], Overlay] | None = None,
    register_overlay: Callable[[Overlay], None] | None = None,
    register_status: Callable[[Callable[[HudMode], None]], None] | None = None,
    on_hotkey_captured: Callable[[str], str | None] | None = None,
    register_phase: Callable[[Callable[[Any], None]], None] | None = None,
    platform: str | None = None,
) -> bool:
    """Run *controller* under the macOS menu-bar app (status item + Quit) — issue #87.

    Like :func:`run_with_overlay` but the AppKit loop also brings up an
    ``NSStatusBar`` item showing live dictation state. Reuses the same
    :func:`build_overlay` factory and the shared fail-open boundary
    (:func:`seda.gui._hostloop.run_hosted`); only the ``run_loop`` differs.

    ``register_status`` is called on the main thread, once the item exists, with
    an ``apply: Callable[[HudMode], None]`` sink bound to the live status item —
    ``cli.gui`` wires it into the composed ``set_mode`` fan-out so the item tracks
    the same ``HudMode`` the HUD does. ``on_hotkey_captured`` is handed to the
    settings window's Record button (#89): it persists + live-applies a captured
    chord, returning an error string or ``None``. Returns ``True`` if the host
    took over and ran to shutdown; ``False`` (fail-open) only when unsupported
    (non-macOS) or an AppKit build failure — ``cli.gui`` treats ``False`` as a
    hard error (unlike ``run``, the menu-bar app has nothing to degrade into).
    """
    from seda.gui._hostloop import run_hosted

    build_fn = build if build is not None else build_overlay

    def _loop(c: AppController, o: Overlay, reg: Callable[[Overlay], None] | None) -> None:
        _run_appkit_menu_bar_loop(c, o, reg, register_status, on_hotkey_captured, register_phase)

    return run_hosted(
        controller,
        supports=lambda plat: plat == "darwin",
        build=build_fn,
        run_loop=_loop,
        register_overlay=register_overlay,
        platform=platform,
    )


def _run_appkit_menu_bar_loop(
    controller: AppController,
    overlay: Overlay,
    register_overlay: Callable[[Overlay], None] | None,
    register_status: Callable[[Callable[[HudMode], None]], None] | None,
    on_hotkey_captured: Callable[[str], str | None] | None = None,
    register_phase: Callable[[Callable[[Any], None]], None] | None = None,
) -> None:
    """macOS ``run_loop`` body for :func:`run_with_menu_bar` (past the fail-open boundary).

    Same as :func:`_run_appkit_loop` but passes a ``build_extra`` that creates the
    ``NSStatusBar`` item inside the host's owned main thread.
    """
    from AppKit import NSApplication

    app = NSApplication.sharedApplication()

    def _build_extra(app_: Any, stop_requested: dict[str, bool]) -> Callable[[], None]:
        # overlay.dispatch_main marshals the capture apply onto a clean main-loop
        # turn. begin/end_hotkey_capture neutralize the controller's hotkey
        # handling while the settings window captures a chord, so those keystrokes
        # (also seen by the global listener) can't drive a phantom recording (#89).
        return _build_status_item(
            app_,
            stop_requested,
            register_status,
            on_hotkey_captured,
            overlay.dispatch_main,
            controller.begin_hotkey_capture,
            controller.end_hotkey_capture,
            register_phase,
            mode_getter=lambda: controller.current_mode,
        )

    _run_appkit_host(controller, app, overlay, register_overlay, build_extra=_build_extra)


# macOS virtual key codes for keys whose typed character is a private-use-area
# glyph (function keys) or ambiguous (space/tab/return/arrows). Mapping them to
# their config token lets the capture widget (#89) serialize e.g. F5 as ``<f5>``
# rather than the U+F708 gibberish AppKit reports for charactersIgnoringModifiers.
_KEYCODE_TO_TOKEN: dict[int, str] = {
    49: "space",
    48: "tab",
    36: "enter",
    53: "esc",
    122: "f1",
    120: "f2",
    99: "f3",
    118: "f4",
    96: "f5",
    97: "f6",
    98: "f7",
    100: "f8",
    101: "f9",
    109: "f10",
    103: "f11",
    111: "f12",
    123: "left",
    124: "right",
    125: "down",
    126: "up",
}


def _trigger_from_event(event: Any) -> str | None:
    """Derive the bare trigger token from a captured key-down NSEvent (#89).

    Returns:
      - a token string (``space`` / ``f5`` / ``d``) for an encodable trigger,
      - ``""`` for a modifier-only key-down (no trigger yet — keep waiting),
      - ``None`` for a key we cannot encode (e.g. an unmapped media key whose
        typed character is a private-use-area glyph), so the caller can reject it
        cleanly instead of serializing an unregisterable chord.

    Prefers the keyCode map (authoritative for function/named keys), then falls
    back to the typed character for ordinary keys. A private-use-area character
    (U+E000–U+F8FF, AppKit's NSFunctionKey range) with no keyCode match is
    treated as unencodable.
    """
    keycode = int(event.keyCode())
    named = _KEYCODE_TO_TOKEN.get(keycode)
    if named is not None:
        return named
    chars = event.charactersIgnoringModifiers()
    if not chars:
        return ""  # modifier-only key-down
    text = str(chars)
    if not text.strip():
        return ""
    # A single private-use-area char is an unmapped function/media key.
    if len(text) == 1 and 0xE000 <= ord(text) <= 0xF8FF:
        return None
    return text.lower()


def _run_capture_apply(
    chord: str,
    on_captured: Callable[[str], str | None],
    dispatch_main: Callable[[Callable[[], None]], None] | None,
    on_ok: Callable[[], None],
    on_err: Callable[[str], None],
) -> None:
    """Apply a captured chord on a clean main-loop turn, then update the UI (#89).

    ``on_captured`` persists the chord and does the LIVE listener chord swap
    (:meth:`AppController.reconfigure_hotkeys` → ``set_push_to_talk``). Two hard
    constraints, both learned from on-device crash reports:

    * It must NOT run *nested inside* the NSEvent monitor callback — that turn is
      the wrong context for touching the listener.
    * It must NOT run on a *background thread* — the swap ultimately touches
      Carbon Text-Input-Source state that asserts the main queue (SIGTRAP).

    The correct home is a **fresh top-level main-loop turn**: marshal the whole
    apply (swap + UI update) via *dispatch_main*, which enqueues it on the main
    run loop rather than running it inline in the current callback. ``on_captured``
    is now cheap and non-blocking (an in-place chord swap + a small TOML write),
    so running it on the main turn is fine — no background thread needed.

    When *dispatch_main* is ``None`` (tests / no overlay wired) it runs inline —
    there is no main run loop to marshal onto. The UI mutations (*on_ok*/*on_err*)
    are injected callbacks, so this is unit-testable on any platform.
    """

    def _apply() -> None:
        try:
            error = on_captured(chord)
        except Exception:  # noqa: BLE001 -- the swap must never crash the app
            logger.warning("hotkey apply failed", exc_info=True)
            error = "could not apply shortcut (see logs)"
        if error:
            on_err(error)
        else:
            on_ok()

    if dispatch_main is not None:
        dispatch_main(_apply)
    else:
        _apply()


def _build_settings_controller(
    on_hotkey_captured: Callable[[str], str | None] | None = None,
    dispatch_main: Callable[[Callable[[], None]], None] | None = None,
    on_capture_begin: Callable[[], None] | None = None,
    on_capture_end: Callable[[], None] | None = None,
) -> Any:
    """Build the non-modal AppKit settings window controller (#88, #89).

    Runs on the main thread inside :func:`_run_appkit_host`. AppKit is imported
    here (never at module top) so this module stays importable on non-macOS / in
    CI. The controller is an ``NSObject`` with ``open_`` (build-or-reopen the
    window, reading the *current* config from disk each time) and ``save_``
    (collect the controls, apply via :func:`apply_settings_edits`, persist via
    :func:`save_config`, surfacing a :class:`ConfigError` in the window's status
    line rather than crashing). v1 edits: cleanup on/off, copy-only (no-paste),
    notify-on-ready, log-transcripts, and the transcription model.

    The push-to-talk hotkey is **editable** via a Record button (#89): pressing
    it arms a window-local ``NSEvent`` key-down monitor; the next non-modifier
    chord is serialized (:func:`serialize_chord`) and handed to
    *on_hotkey_captured*, which persists it and live-applies it to the running
    controller, returning an error string (shown in the status line) or ``None``
    on success. ``on_hotkey_captured`` is ``None`` in tests / when no controller
    is wired, in which case the Record button is inert. Non-modal + reopen-safe:
    ``open_`` reloads config and brings the existing window forward.

    Edits the **default** config file (:func:`default_config_path` via
    ``load_config(None)`` / ``save_config(config, None)``) — the menu-bar app's
    real target; a ``--config`` passed to ``seda gui`` does not redirect it.
    """
    import objc
    from AppKit import (
        NSApplication,
        NSBackingStoreBuffered,
        NSButton,
        NSEvent,
        NSEventMaskKeyDown,
        NSEventModifierFlagCommand,
        NSEventModifierFlagControl,
        NSEventModifierFlagOption,
        NSEventModifierFlagShift,
        NSFont,
        NSMakeRect,
        NSObject,
        NSPopUpButton,
        NSSwitchButton,
        NSTextField,
        NSWindow,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskTitled,
    )

    from seda.config import (
        apply_settings_edits,
        load_config,
        save_config,
        select_push_to_talk,
    )
    from seda.input.hotkeys import format_chord_display, serialize_chord

    # Curated faster-whisper model list for the popup (#88 rebuild). Free-text was
    # error-prone (a typo only surfaced at restart); a popup prevents that for the
    # common case while "Other…" keeps an escape hatch for a custom / local model.
    _KNOWN_MODELS = (
        "tiny.en",
        "tiny",
        "base.en",
        "base",
        "small.en",
        "small",
        "medium.en",
        "medium",
        "large-v3",
    )
    _MODEL_OTHER = "Other…"

    # NSEvent.modifierFlags is a bitmask; map each flag to the canonical config
    # modifier token serialize_chord expects. Order here is irrelevant —
    # serialize_chord re-sorts into its canonical order.
    _MODIFIER_FLAGS = (
        (NSEventModifierFlagControl, "ctrl"),
        (NSEventModifierFlagOption, "alt"),
        (NSEventModifierFlagShift, "shift"),
        (NSEventModifierFlagCommand, "cmd"),
    )

    def _modifiers_from_flags(flags: int) -> frozenset[str]:
        return frozenset(tok for flag, tok in _MODIFIER_FLAGS if flags & flag)

    # Checkbox rows only (dotted path, label). The model is handled separately as
    # a popup below; copy-only is its own toggle. Grouped under section headers in
    # _build_window rather than laid out as one flat stack (#88 rebuild).
    _CHECK_FIELDS = [
        ("cleanup.enabled", "Clean up transcript with a local LLM"),
        ("app.notify_on_ready", "Notify when ready to dictate"),
        ("app.log_transcripts", "Log transcripts to disk (off for privacy)"),
    ]

    class _SettingsController(NSObject):  # type: ignore[misc]
        def init(self) -> Any:  # noqa: N802
            self = objc.super(_SettingsController, self).init()
            if self is None:
                return None
            self._window: Any = None
            self._controls: dict[str, Any] = {}
            self._status: Any = None
            self._copy_only: Any = None
            self._cfg: Any = None
            # Hotkey capture (#89): the current-chord label, the Record button,
            # the armed NSEvent monitor (None when not recording), and the last
            # captured chord shown in the label.
            self._hotkey_label: Any = None
            self._record_button: Any = None
            self._capture_monitor: Any = None
            self._hotkey_chord: str = ""
            # Model popup + its "Other…" free-text escape hatch (#88 rebuild).
            self._model_popup: Any = None
            self._model_other: Any = None
            return self

        def open_(self, _sender: Any) -> None:  # noqa: N802 -- ObjC selector open:
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            self._cfg = load_config(None)
            if self._window is None:
                self._build_window()
            self._populate()
            self._window.makeKeyAndOrderFront_(None)

        def _build_window(self) -> None:
            # NSWindowStyleMaskTitled is the non-deprecated spelling of the old
            # NSTitledWindowMask (#88 rebuild). Grown to 380h to fit the two
            # section headers + the model popup and its "Other…" field.
            style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, 380, 380), style, NSBackingStoreBuffered, False
            )
            win.setTitle_("Seda Settings")
            win.setReleasedWhenClosed_(False)  # reopen-safe: closing hides, not frees
            content = win.contentView()
            y = 344

            def _label(text: str, yy: int, *, width: int = 150) -> Any:
                lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(16, yy, width, 20))
                lbl.setStringValue_(text)
                lbl.setEditable_(False)
                lbl.setBordered_(False)
                lbl.setDrawsBackground_(False)
                content.addSubview_(lbl)
                return lbl

            def _section(text: str, yy: int) -> None:
                # A bold section header. NSFont.boldSystemFontOfSize_ visually
                # separates the grouped rows below it (#88 rebuild) without a box.
                hdr = _label(text, yy, width=340)
                with contextlib.suppress(Exception):
                    hdr.setFont_(NSFont.boldSystemFontOfSize_(13))

            # Push-to-talk: a current-chord label + a Record button that captures
            # a new chord (#89). The label shows the live chord as macOS key-cap
            # glyphs (⌃⇧Space); Record arms a window-local key monitor (see
            # _record_hotkey_ / _capture_event).
            _label("Push-to-talk", y)
            hk = NSTextField.alloc().initWithFrame_(NSMakeRect(170, y, 110, 20))
            hk.setEditable_(False)
            hk.setBordered_(False)
            hk.setDrawsBackground_(False)
            hk.setSelectable_(True)
            content.addSubview_(hk)
            self._hotkey_label = hk
            # Keep the read-only field reachable under its old key so _populate's
            # existing shape is preserved (and any regression test that looks it up).
            self._controls["_hotkey"] = hk

            record = NSButton.alloc().initWithFrame_(NSMakeRect(284, y - 4, 60, 26))
            record.setTitle_("Record")
            record.setBezelStyle_(1)  # NSBezelStyleRounded
            record.setTarget_(self)
            record.setAction_("recordHotkey:")
            # Inert when no capture sink is wired (tests / no controller).
            record.setEnabled_(on_hotkey_captured is not None)
            content.addSubview_(record)
            self._record_button = record
            y -= 44

            # ── Transcription ────────────────────────────────────────────────
            # Model as a popup of curated faster-whisper names + an "Other…" item
            # that reveals a free-text field for a custom / local model (#88
            # rebuild). The popup's action (modelChanged:) toggles the field.
            _section("Transcription", y)
            y -= 28
            _label("Model", y)
            popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(170, y - 2, 194, 26), False
            )
            popup.addItemsWithTitles_(list(_KNOWN_MODELS))
            popup.addItemWithTitle_(_MODEL_OTHER)
            popup.setTarget_(self)
            popup.setAction_("modelChanged:")
            content.addSubview_(popup)
            self._model_popup = popup
            y -= 30
            other = NSTextField.alloc().initWithFrame_(NSMakeRect(170, y, 194, 22))
            other.setPlaceholderString_("custom model name")
            content.addSubview_(other)
            self._model_other = other
            y -= 36

            # ── Behavior ─────────────────────────────────────────────────────
            _section("Behavior", y)
            y -= 28
            for dotted, label in _CHECK_FIELDS:
                btn = NSButton.alloc().initWithFrame_(NSMakeRect(16, y, 348, 20))
                btn.setButtonType_(NSSwitchButton)
                btn.setTitle_(label)
                content.addSubview_(btn)
                self._controls[dotted] = btn
                y -= 28

            # Copy-only maps to paste.multiline_policy == "copy_only".
            copy_btn = NSButton.alloc().initWithFrame_(NSMakeRect(16, y, 348, 20))
            copy_btn.setButtonType_(NSSwitchButton)
            copy_btn.setTitle_("Copy only (never paste)")
            content.addSubview_(copy_btn)
            self._copy_only = copy_btn
            y -= 32

            save = NSButton.alloc().initWithFrame_(NSMakeRect(274, 12, 90, 28))
            save.setTitle_("Save")
            save.setTarget_(self)
            save.setAction_("save:")
            content.addSubview_(save)

            status = NSTextField.alloc().initWithFrame_(NSMakeRect(16, 14, 220, 20))
            status.setEditable_(False)
            status.setBordered_(False)
            status.setDrawsBackground_(False)
            content.addSubview_(status)
            self._status = status

            # Center on screen. The window is built at origin (0, 0) — the
            # bottom-left corner in AppKit's coordinate space — so without this
            # it opens tucked in the corner. center() places it slightly above
            # the screen's midpoint, the standard macOS position for a fresh
            # window; harmless if it ever raises, so it stays best-effort.
            with contextlib.suppress(Exception):
                win.center()
            # Be our own window delegate so windowWillClose_ can disarm a live
            # capture monitor if the user closes the window mid-recording (#89).
            with contextlib.suppress(Exception):
                win.setDelegate_(self)
            self._window = win

        def windowWillClose_(self, _notification: Any) -> None:  # noqa: N802 -- NSWindowDelegate
            # If the window is closed while a chord capture is armed, the local
            # NSEvent monitor is app-scoped and would otherwise keep swallowing
            # keystrokes. Disarm it on close (idempotent — no-op when not armed).
            self._disarm_capture()

        def _populate(self) -> None:
            # Reads self._cfg (set by open_). Kept parameter-free so pyobjc can
            # register it as a valid selector — a helper method with a non-selector
            # arg signature raises BadPrototypeError on an ObjC subclass.
            cfg = self._cfg
            self._hotkey_chord = select_push_to_talk(cfg.hotkeys)
            # Show the chord as macOS key-cap glyphs (⌃⇧Space) — the same glyphs
            # the menu header uses. The raw config chord is preserved in
            # self._hotkey_chord for the capture path; only the label is glyphs.
            self._controls["_hotkey"].setStringValue_(format_chord_display(self._hotkey_chord))
            self._controls["cleanup.enabled"].setState_(1 if cfg.cleanup.enabled else 0)
            self._controls["app.notify_on_ready"].setState_(1 if cfg.app.notify_on_ready else 0)
            self._controls["app.log_transcripts"].setState_(1 if cfg.app.log_transcripts else 0)
            self._select_model(cfg.transcription.model)
            self._copy_only.setState_(1 if cfg.paste.multiline_policy == "copy_only" else 0)
            self._status.setStringValue_("")

        @objc.python_method  # type: ignore[untyped-decorator]
        def _select_model(self, model: str) -> None:
            # Point the popup at *model*: a curated name selects that item and
            # hides the free-text field; anything else lands on "Other…" with the
            # value pre-filled and the field revealed. Idempotent — safe to call
            # from _populate and from modelChanged_.
            if model in _KNOWN_MODELS:
                self._model_popup.selectItemWithTitle_(model)
                self._model_other.setStringValue_("")
                self._model_other.setHidden_(True)
            else:
                self._model_popup.selectItemWithTitle_(_MODEL_OTHER)
                self._model_other.setStringValue_(model)
                self._model_other.setHidden_(False)

        @objc.python_method  # type: ignore[untyped-decorator]
        def _current_model(self) -> str:
            # The effective model from the popup: the selected preset, or the
            # free-text value when "Other…" is chosen (stripped).
            title = str(self._model_popup.titleOfSelectedItem() or "")
            if title == _MODEL_OTHER:
                return str(self._model_other.stringValue()).strip()
            return title

        def modelChanged_(self, _sender: Any) -> None:  # noqa: N802 -- selector modelChanged:
            # Popup selection changed: reveal the free-text field only for
            # "Other…", hide it otherwise. Focus the field when revealed so the
            # user can type immediately.
            is_other = str(self._model_popup.titleOfSelectedItem() or "") == _MODEL_OTHER
            self._model_other.setHidden_(not is_other)
            if is_other:
                with contextlib.suppress(Exception):
                    self._window.makeFirstResponder_(self._model_other)

        def recordHotkey_(self, _sender: Any) -> None:  # noqa: N802 -- selector recordHotkey:
            """Arm a window-local key-down monitor to capture the next chord (#89).

            Installs a LOCAL NSEvent monitor (window-scoped, not a global tap — no
            new Accessibility surface). The first non-modifier key-down while armed
            is serialized and handed to *on_hotkey_captured*; a bare Esc cancels.
            Fully fail-open: any AppKit error here is logged, never crashes the
            window, and disarms cleanly. Idempotent — re-arming while armed is a
            no-op (the monitor is already live).
            """
            if on_hotkey_captured is None or self._capture_monitor is not None:
                return
            self._status.setStringValue_("Press keys… (Esc to cancel)")
            self._record_button.setTitle_("Recording…")
            # Neutralize the controller's hotkey handling for the duration of the
            # capture: the keys the user presses are ALSO seen by the global
            # listener (the NSEvent local monitor can't shield pynput's global
            # tap), and without this they would drive a phantom recording that
            # wedges the app (#89). Cleared in _disarm_capture.
            if on_capture_begin is not None:
                with contextlib.suppress(Exception):
                    on_capture_begin()

            def _handler(event: Any) -> Any:
                # Runs on the main thread for local key events. Returning None
                # swallows the event (so the chord never leaks to a field);
                # returning the event passes it through. We only ever handle
                # key-downs while armed.
                try:
                    self._capture_event(event)
                except Exception:  # noqa: BLE001 -- capture must never crash the window
                    logger.warning("hotkey capture failed", exc_info=True)
                    self._disarm_capture()
                    self._status.setStringValue_("capture failed (see logs)")
                return None  # swallow — don't let the chord reach other controls

            with contextlib.suppress(Exception):
                self._capture_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                    NSEventMaskKeyDown, _handler
                )
            if self._capture_monitor is None:
                # Monitor could not be installed — restore the idle button state.
                self._disarm_capture()
                self._status.setStringValue_("could not start capture")

        @objc.python_method  # type: ignore[untyped-decorator]
        def _capture_event(self, event: Any) -> None:
            if on_hotkey_captured is None:
                # Defensive: capture is only armed when a sink is wired, but a
                # narrowed local keeps the type-checker honest and no-ops safely.
                self._disarm_capture()
                return
            # Esc (keyCode 53) with no modifiers cancels the capture entirely.
            if int(event.keyCode()) == 53 and not _modifiers_from_flags(int(event.modifierFlags())):
                self._disarm_capture()
                self._status.setStringValue_("capture cancelled")
                return

            mods = _modifiers_from_flags(int(event.modifierFlags()))
            trigger = _trigger_from_event(event)
            if trigger is None:
                # A key we can't encode (an unmapped function/media key reports a
                # private-use-area char). Reject clearly rather than serialize a
                # gibberish chord that would fail later at listener-start.
                self._disarm_capture()
                self._status.setStringValue_("Unsupported key — try another.")
                return
            if not trigger:
                return  # modifier-only key-down; keep waiting for the trigger

            chord = serialize_chord(mods, trigger)
            self._disarm_capture()
            self._status.setStringValue_("Applying…")

            def _on_ok() -> None:
                self._hotkey_chord = chord
                self._hotkey_label.setStringValue_(chord)
                self._status.setStringValue_("Shortcut updated.")

            def _on_err(msg: str) -> None:
                self._status.setStringValue_(msg[:80])

            # Run the sink (persist + LIVE listener re-registration) OFF this
            # NSEvent-callback turn — see _run_capture_apply for why (SIGABRT).
            _run_capture_apply(chord, on_hotkey_captured, dispatch_main, _on_ok, _on_err)

        @objc.python_method  # type: ignore[untyped-decorator]
        def _disarm_capture(self) -> None:
            monitor = self._capture_monitor
            self._capture_monitor = None
            if monitor is not None:
                with contextlib.suppress(Exception):
                    NSEvent.removeMonitor_(monitor)
                # Re-enable the controller's hotkey handling (paired with the
                # on_capture_begin in recordHotkey_). Only when a capture was
                # actually armed, so the end pairs with a begin.
                if on_capture_end is not None:
                    with contextlib.suppress(Exception):
                        on_capture_end()
            with contextlib.suppress(Exception):
                self._record_button.setTitle_("Record")

        def save_(self, _sender: Any) -> None:  # noqa: N802 -- ObjC selector save:
            from seda.config import ConfigError

            # Reload the on-disk config and emit an edit ONLY for a field whose
            # control value actually differs — so a save never rewrites a field the
            # user did not touch. Critically this preserves paste.multiline_policy
            # == "flatten": the copy-only checkbox only toggles copy_only<->preserve,
            # and leaving it unchecked when the current value is "flatten" emits no
            # multiline_policy edit at all (rather than clobbering it to "preserve").
            try:
                cfg = load_config(None)
            except ConfigError as exc:
                self._status.setStringValue_(str(exc).splitlines()[-1][:80])
                return

            edits: dict[str, Any] = {}
            checks = {
                "cleanup.enabled": cfg.cleanup.enabled,
                "app.notify_on_ready": cfg.app.notify_on_ready,
                "app.log_transcripts": cfg.app.log_transcripts,
            }
            for path, current_val in checks.items():
                new_val = bool(self._controls[path].state())
                if new_val != current_val:
                    edits[path] = new_val

            # Model from the popup: a preset title, or the "Other…" free-text
            # (stripped) for a custom / local model. The empty-guard still holds
            # — an "Other…" selection with a blank field is rejected, matching the
            # config validator (transcription.model must be non-empty).
            model = self._current_model()
            if not model:
                self._status.setStringValue_("Model must not be empty.")
                return
            if model != cfg.transcription.model:
                edits["transcription.model"] = model

            # Copy-only maps ONLY the copy_only<->non-copy_only distinction; a
            # non-copy_only current value (preserve OR flatten) is left untouched
            # unless the box is now checked.
            want_copy_only = bool(self._copy_only.state())
            is_copy_only = cfg.paste.multiline_policy == "copy_only"
            if want_copy_only != is_copy_only:
                edits["paste.multiline_policy"] = "copy_only" if want_copy_only else "preserve"

            if not edits:
                # Nothing to write, but the user asked to save — treat as done
                # and close the window (they don't expect it to linger).
                self._close_window()
                return

            try:
                save_config(apply_settings_edits(cfg, edits), None)
            except ConfigError as exc:
                self._status.setStringValue_(str(exc).splitlines()[-1][:80])
                return
            except Exception:  # noqa: BLE001
                logger.warning("settings save failed", exc_info=True)
                self._status.setStringValue_("save failed (see logs)")
                return
            # Saved successfully — close the window. (Non-hotkey fields are
            # save-and-restart; the hotkey applies live via its own capture path.)
            self._close_window()

        @objc.python_method  # type: ignore[untyped-decorator]
        def _close_window(self) -> None:
            # performClose_ runs the standard close (honoring the delegate's
            # windowWillClose_, which disarms any live capture monitor). Guarded
            # so a close failure never leaves save() half-done.
            with contextlib.suppress(Exception):
                if self._window is not None:
                    self._window.performClose_(None)

    return _SettingsController.alloc().init()


def _mode_row_title(mode: str) -> str:
    """Menu-row title for the active dictation mode (#115), e.g. 'Mode:  Polished'."""
    return f"Mode:  {mode.title()}"


def _build_status_item(
    app: Any,
    stop_requested: dict[str, bool],
    register_status: Callable[[Callable[[HudMode], None]], None] | None,
    on_hotkey_captured: Callable[[str], str | None] | None = None,
    dispatch_main: Callable[[Callable[[], None]], None] | None = None,
    on_capture_begin: Callable[[], None] | None = None,
    on_capture_end: Callable[[], None] | None = None,
    register_phase: Callable[[Callable[[Any], None]], None] | None = None,
    mode_getter: Callable[[], str] | None = None,
) -> Callable[[], None]:
    """Create the ``NSStatusBar`` item + menu (Quit) and wire live status — issue #87.

    Runs on the main thread inside :func:`_run_appkit_host`. AppKit is imported
    here (never at module top) so this module stays importable on non-macOS / in
    CI. The item's title comes from :func:`status_label` and its glyph from
    :func:`status_symbol` (image swap best-effort — a missing symbol degrades to
    text-only, never a crash). Quit routes through the existing shutdown path by
    flipping ``stop_requested["flag"]`` + posting a wakeup, so the pump does
    ``controller.shutdown() -> app.stop_``. Returns a teardown thunk that removes
    the item.

    *on_hotkey_captured* is threaded to the settings window's Record button (#89);
    ``None`` leaves the button inert. *register_phase* (if given) receives an
    ``apply_phase(NotificationEvent)`` sink that advances the menu-bar label
    through the busy phases (Working… → Transcribing… → Cleaning up… →
    Inserting…) — a finer signal than the coarse ``HudMode`` the HUD tracks.
    """
    # Bound before the menu-header delegate class statement below evaluates its
    # @objc.python_method decorators. Local import keeps the module importable on
    # non-macOS.
    import objc
    from AppKit import (
        NSAlert,
        NSApplication,  # noqa: F401 -- ensures AppKit is up (imported by the loop already)
        NSImage,
        NSMenu,
        NSMenuItem,
        NSObject,
        NSStatusBar,
        NSVariableStatusItemLength,
        NSWorkspace,
    )
    from Foundation import NSTimer

    from seda.notifications import status_phase_label, status_phase_symbol

    status_bar = NSStatusBar.systemStatusBar()
    item = status_bar.statusItemWithLength_(NSVariableStatusItemLength)

    def _set_button(title: str, symbol: str) -> None:
        # Shared title+glyph writer. Glyph is best-effort; a missing SF Symbol
        # degrades to text-only rather than crashing (older macOS / typo).
        button = item.button()
        if button is None:
            return
        button.setTitle_(title)
        try:
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, title)
            if image is not None:
                button.setImage_(image)
        except Exception:  # noqa: BLE001
            logger.debug("status-item glyph update failed; text-only", exc_info=True)

    # Last coarse HUD mode applied to the label, so the #115 mode-flash can
    # revert to the correct status label once the flash expires.
    hud_mode_holder: dict[str, HudMode] = {"mode": HudMode.IDLE}

    def _apply(mode: HudMode) -> None:
        # Coarse HUD-mode sink (kept for back-compat + the READY/RECORDING/IDLE
        # transitions the HUD drives). Runs on the main thread.
        hud_mode_holder["mode"] = mode
        _set_button(status_label(mode), status_symbol(mode))

    def _apply_phase(event: Any) -> None:
        # Fine phase sink (#87 follow-up): advance the label through the busy
        # phases. Runs on the main thread (marshalled by StatusPhaseNotifier).
        label = status_phase_label(event)
        if label is None:
            return  # unmapped event — leave the label as-is
        symbol = status_phase_symbol(event) or "circle"
        _set_button(label, symbol)

    # Quit target: an NSObject exposing an action that requests stop through the
    # EXISTING pump path (no new shutdown logic). Held on the item so it outlives
    # this function (the menu item's target is a weak ref in AppKit).
    class _QuitTarget(NSObject):  # type: ignore[misc]
        def quit_(self, _sender: Any) -> None:  # noqa: N802 -- ObjC selector quit:
            stop_requested["flag"] = True
            _post_wakeup_event(app)

    # Open Logs: reveal the log file (or its dir) in Finder (#90). Best-effort —
    # a reveal failure is logged, never crashes the menu.
    class _OpenLogsTarget(NSObject):  # type: ignore[misc]
        def openLogs_(self, _sender: Any) -> None:  # noqa: N802 -- ObjC selector openLogs:
            from seda.logging_config import log_reveal_target

            try:
                target = str(log_reveal_target())
                NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(target, "")
            except Exception:  # noqa: BLE001
                logger.warning("Open Logs failed", exc_info=True)

    # A tiny main-thread marshaller for actions that must compute off-thread but
    # present UI on the main thread (dependency-free GCD-to-main path). Named
    # distinctly from build_overlay's runner: ObjC classes register GLOBALLY by
    # name, so two classes sharing a name in one process raise
    # "overriding existing Objective-C class" (regression: this was _MainThreadRunner,
    # colliding with build_overlay's; caught by the #90 on-hardware by-eye).
    class _StatusItemRunner(NSObject):  # type: ignore[misc]
        def runHolder_(self, holder: Any) -> None:  # noqa: N802
            holder["fn"]()

    runner = _StatusItemRunner.alloc().init()

    # Doctor: run diagnostics (run_checks is CLI-decoupled) and show the same
    # report the CLI prints, in an NSAlert (#90). Never shells out. The probes run
    # on a BACKGROUND thread — run_checks does bounded I/O (audio enumeration, a
    # ~1.5s Ollama HTTP probe when cleanup is enabled) that must not block the
    # main run loop (HUD redraw + the SIGINT/SIGTERM pump). The alert is then
    # marshalled back to the main thread, where the accessory app is activated
    # first so the modal comes to the front (an accessory app's modal is not
    # reliably raised otherwise — the no-focus-steal invariant governs the passive
    # HUD, not an explicit user-initiated menu action).
    class _DoctorTarget(NSObject):  # type: ignore[misc]
        def runDoctor_(self, _sender: Any) -> None:  # noqa: N802 -- ObjC selector runDoctor:
            import threading

            from seda.diagnostics import format_diagnostics, run_checks, worst_status

            def _work() -> None:
                try:
                    results = run_checks(None)
                    text = format_diagnostics(results, worst_status(results))
                except Exception:  # noqa: BLE001
                    logger.warning("Doctor checks failed", exc_info=True)
                    return

                def _present() -> None:
                    try:
                        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                        alert = NSAlert.alloc().init()
                        alert.setMessageText_("Seda diagnostics")
                        alert.setInformativeText_(text)
                        alert.runModal()
                    except Exception:  # noqa: BLE001
                        logger.warning("Doctor view failed", exc_info=True)

                runner.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "runHolder:", {"fn": _present}, False
                )

            threading.Thread(target=_work, name="seda-doctor", daemon=True).start()

    quit_target = _QuitTarget.alloc().init()
    open_logs_target = _OpenLogsTarget.alloc().init()
    doctor_target = _DoctorTarget.alloc().init()

    # A disabled header item that shows the current push-to-talk chord as macOS
    # key-cap glyphs (#87 follow-up): the single most-needed fact — how to
    # dictate — is now zero clicks deep, right at the top of the menu, instead of
    # buried in Settings. Refreshed each time the menu opens (menuNeedsUpdate:)
    # so it tracks a live rebind. Reads config off the main thread on menu-open,
    # which is a cheap TOML read; fail-open to a neutral label.
    class _MenuHeaderDelegate(NSObject):  # type: ignore[misc]
        def menuNeedsUpdate_(self, menu: Any) -> None:  # noqa: N802 -- NSMenuDelegate
            with contextlib.suppress(Exception):
                self._refresh_header()

        @objc.python_method  # type: ignore[untyped-decorator]
        def _refresh_header(self) -> None:
            from seda.config import load_config, select_push_to_talk
            from seda.input.hotkeys import format_chord_display

            try:
                cfg = load_config(None)
                chord = format_chord_display(select_push_to_talk(cfg.hotkeys))
            except Exception:  # noqa: BLE001 -- never let a config read break the menu
                logger.debug("menu header chord read failed", exc_info=True)
                chord = "—"
            self._header_item.setTitle_(f"Push-to-talk:  {chord}")
            # #115: refresh the active-mode row too, when wired.
            getter = getattr(self, "_mode_getter", None)
            mode_item = getattr(self, "_mode_item", None)
            if getter is not None and mode_item is not None:
                try:
                    mode_item.setTitle_(_mode_row_title(getter()))
                except Exception:  # noqa: BLE001 -- never let a mode read break the menu
                    logger.debug("menu mode-row read failed", exc_info=True)

    header_delegate = _MenuHeaderDelegate.alloc().init()

    menu = NSMenu.alloc().init()
    # A disabled, non-selectable header row showing the live chord.
    header_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Push-to-talk:  —", "", "")
    header_item.setEnabled_(False)
    menu.addItem_(header_item)
    # #115: a disabled row showing the active dictation mode (the session
    # override the toggle_mode chord cycles, #109). Refreshed on menu-open and
    # on change by the poll timer below.
    mode_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Mode:  —", "", "")
    mode_item.setEnabled_(False)
    menu.addItem_(mode_item)
    menu.addItem_(NSMenuItem.separatorItem())
    header_delegate._header_item = header_item
    header_delegate._mode_item = mode_item
    header_delegate._mode_getter = mode_getter
    menu.setDelegate_(header_delegate)
    with contextlib.suppress(Exception):
        header_delegate._refresh_header()  # seed before first open

    settings_controller = _build_settings_controller(
        on_hotkey_captured, dispatch_main, on_capture_begin, on_capture_end
    )
    settings_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Settings…", "open:", ","
    )
    settings_item.setTarget_(settings_controller)
    menu.addItem_(settings_item)
    menu.addItem_(NSMenuItem.separatorItem())
    logs_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Open Logs", "openLogs:", "")
    logs_item.setTarget_(open_logs_target)
    menu.addItem_(logs_item)
    doctor_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Diagnostics…", "runDoctor:", ""
    )
    doctor_item.setTarget_(doctor_target)
    menu.addItem_(doctor_item)
    menu.addItem_(NSMenuItem.separatorItem())
    quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit Seda", "quit:", "q")
    quit_item.setTarget_(quit_target)
    menu.addItem_(quit_item)
    item.setMenu_(menu)

    # Seed the initial glyph/title and hand the apply sink to cli.gui so it can be
    # composed into set_mode. The sink is only valid now that the item exists.
    _apply(HudMode.IDLE)
    if register_status is not None:
        register_status(_apply)
    # Hand the fine phase sink to cli.gui too, so the busy-phase labels advance.
    if register_phase is not None:
        register_phase(_apply_phase)

    # #115: poll the active dictation mode ~5x/s on the main thread; on a change
    # (the toggle_mode chord cycled it at IDLE, #109) briefly flash the status
    # label with the new mode, then revert to the current HUD-mode label. The
    # persistent Mode row above also shows it whenever the menu is open. The
    # timer runs on the main run loop, so touching AppKit here is main-thread-safe.
    mode_timer: Any = None
    if mode_getter is not None:
        getter = mode_getter
        try:
            initial_mode = getter()
        except Exception:  # noqa: BLE001
            initial_mode = ""
        mode_state: dict[str, Any] = {"last": initial_mode, "flash_until": None}

        def _poll_mode(_timer: Any) -> None:
            try:
                mode = getter()
            except Exception:  # noqa: BLE001
                return
            now = time.monotonic()
            if mode != mode_state["last"]:
                mode_state["last"] = mode
                mode_state["flash_until"] = now + 1.2
                with contextlib.suppress(Exception):
                    mode_item.setTitle_(_mode_row_title(mode))
                _set_button(mode.title(), "slider.horizontal.3")
            elif mode_state["flash_until"] is not None and now >= mode_state["flash_until"]:
                mode_state["flash_until"] = None
                _apply(hud_mode_holder["mode"])  # revert to the live status label

        mode_timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.2, True, _poll_mode)

    def _teardown() -> None:
        # Keep the menu-action targets + settings controller + main-thread runner
        # + the menu header delegate referenced until teardown so a menu action
        # or a menuNeedsUpdate: never dispatches into a freed ObjC object; drop
        # the item from the bar.
        _ = (
            quit_target,
            open_logs_target,
            doctor_target,
            settings_controller,
            runner,
            header_delegate,
        )
        if mode_timer is not None:
            with contextlib.suppress(Exception):
                mode_timer.invalidate()
        status_bar.removeStatusItem_(item)

    return _teardown


def _run_appkit_host(
    controller: AppController,
    app: Any,
    overlay: Overlay,
    register_overlay: Callable[[Overlay], None] | None,
    *,
    build_extra: Callable[[Any, dict[str, bool]], Callable[[], None]] | None = None,
) -> None:
    """Own the main thread with AppKit and drive *controller* (macOS only).

    Assumes AppKit is available and *overlay* is already built (the fail-open
    boundary is in :func:`run_with_overlay`). Installs signal handlers, starts
    the controller, and blocks in ``NSApplication.run()``.

    ``build_extra`` (the ``gui`` path only) is an optional main-thread hook that
    adds an extra surface — the ``NSStatusBar`` menu-bar item — inside this owned
    run loop. It is called with ``(app, stop_requested)`` after the warm calls and
    before ``controller.start()``, and returns a ``teardown_extra`` callable run in
    the ``finally:`` alongside ``overlay.teardown()``. When ``None`` (the ``run``
    path) nothing extra is built and the behavior is unchanged.
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
    # Also warm the SENDER-side Carbon TIS init: the paste backend builds a pynput
    # Controller (get_unicode_to_keycode_map) whose TIS call asserts the main
    # queue. Built lazily it would run on the worker thread at first paste and
    # crash (SIGTRAP, #89); build it here on the main thread. Best-effort inside
    # warm_inserter (a headless/pynput failure is swallowed).
    with contextlib.suppress(Exception):
        controller.warm_inserter()

    # gui path: build the extra main-thread surface (the NSStatusBar item) now —
    # after warming, before controller.start(), on the owned main thread. Fail-open
    # WITHIN macOS: a status-item failure must never break dictation, so a raising
    # build_extra is swallowed and its teardown becomes a no-op. Returns the
    # teardown thunk run in the finally: below.
    def _no_teardown() -> None:
        return None

    teardown_extra: Callable[[], None] = _no_teardown
    if build_extra is not None:
        try:
            teardown_extra = build_extra(app, stop_requested)
        except Exception:  # noqa: BLE001
            logger.warning("menu-bar status item setup failed; continuing", exc_info=True)
            teardown_extra = _no_teardown

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
        # Remove the menu-bar status item last (gui path). Guarded so a lingering
        # item is the only failure mode, never a masked crash (the status-item
        # analogue of the #37/#38 lingering-HUD invariant).
        try:
            teardown_extra()
        except Exception:  # noqa: BLE001
            logger.warning("menu-bar status item teardown failed during shutdown", exc_info=True)


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
