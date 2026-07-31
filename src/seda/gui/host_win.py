"""Windows GUI host that owns the main thread (ADR-0008, ADR-0009).

The Windows sibling of :mod:`seda.gui.host` (macOS/AppKit). Same *shape* — a GUI
host owns the main thread, registers the overlay, starts the controller, pumps a
GUI loop, and tears the window down in a ``finally`` on every exit — but
Win32-specific *mechanics*: an interruptible ``PeekMessageW(PM_REMOVE)`` pump
with a bare polled stop flag (ADR-0008 §2/§3), and raw Win32 + GDI+ via stdlib
``ctypes`` (zero new deps).

**Step 3 scope — lifecycle + threading only.** Every Win32/GDI+ touch sits behind
a module-level shim (ADR-0005) so unit tests monkeypatch fakes and **CI never
loads ``ctypes.windll``**. Real per-pixel drawing (:func:`_paint`) is a no-op
stub here; the card/bars render body is Step 4 (spike-gated on #66, parity spec
`docs/specs/windows-hud-parity.md`).

**CI-cleanliness invariant.** ``import ctypes`` at module top is fine (stdlib,
present on Linux), but ``ctypes.WINFUNCTYPE`` does not exist on non-Windows
CPython and ``ctypes.WinDLL`` cannot load Windows DLLs there. So the WNDPROC
functype, the ``ctypes.Structure`` layouts, the DLL handles, and the
``restype``/``argtypes`` declarations are all built **lazily inside**
:func:`_load_libs`, first called from :func:`build_overlay`. Importing this
module on Linux runs zero native code.

**Fail-open is the hard invariant** (epic #15, `docs/specs/windows-hud-fail-open.md`):
this host never lets a missing or broken Win32/GDI+ layer affect dictation. The
fail-open boundary lives in the shared :func:`seda.gui._hostloop.run_hosted`
(build fails → ``False`` → terminal path); this module supplies the ``supports``
gate, the transactional :func:`build_overlay`, and the :func:`_run_win32_loop`
body (past the boundary — a failure there propagates, exactly as macOS).
"""

from __future__ import annotations

import ctypes
import logging
import queue
import signal
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from seda.notifications import HudMode

if TYPE_CHECKING:
    from seda.app import AppController

logger = logging.getLogger(__name__)

# --- Win32 constants (plain ints — safe to define at import) ----------------
# Extended styles for the overlay window (validated on hardware, #41): layered +
# transparent = click-through; no-activate + tool-window + topmost = never steals
# focus, no taskbar/ALT+TAB, always on top. Never SetForegroundWindow.
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
_OVERLAY_EX_STYLE = (
    WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
)
WS_POPUP = 0x80000000

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOWNA = 8

# Panel geometry (1:1 with macOS build_overlay; parity spec Part 2).
_PANEL_W, _PANEL_H = 160, 48

# Redraw cadence (ADR-0007 §5) — shared cross-platform policy. ~60 Hz active for
# LISTENING/BUSY. IDLE's ~10 Hz rate lands with HudMode.IDLE (#56); until then
# both live modes are "active".
_ACTIVE_INTERVAL_MS = 16
_DISPATCH_QUEUE_MAX = 256  # bounded dispatch_main queue (fail-open E6)

# Pump cadence (ADR-0008 §2): drain messages, service the stop flag, sleep ~5 ms.
_PUMP_SLEEP_SECONDS = 0.005


def _interval_ms(mode: HudMode) -> int:
    """Redraw interval for *mode* (ADR-0007 §5).

    LISTENING and BUSY are both "active" (~60 Hz). The idle rate hooks in with
    ``HudMode.IDLE`` once the enum + notifier land.
    """
    # TODO(#56): HudMode.IDLE -> ~100 ms (~10 Hz) once the idle mode exists.
    return _ACTIVE_INTERVAL_MS


# ---------------------------------------------------------------------------
# Module-level shims (ADR-0005). The ONLY place raw ctypes/windll lives; unit
# tests monkeypatch ``seda.gui.host_win._<name>``. In Step 3 the native bodies
# are intentionally thin and several draw shims are stubs — the point is the
# lifecycle/threading logic *around* them, proven Win32-free on Linux CI.
# ---------------------------------------------------------------------------


def _load_libs() -> tuple[Any, Any, Any]:
    """Lazily load user32/gdi32/gdiplus, build ctypes layouts, declare prototypes.

    Called first from :func:`build_overlay`. Everything Windows-only lives here
    (never at import): ``ctypes.WinDLL`` (raises ``OSError``/``AttributeError``
    on non-Windows — the natural fail-open trigger, catalog B2), the
    ``WINFUNCTYPE`` WNDPROC, the ``Structure`` layouts, and every
    ``restype``/``argtypes`` (mandatory on Win64 — HWND truncation, catalog G1).
    """
    windll = ctypes.windll  # type: ignore[attr-defined]  # absent on non-Windows -> AttributeError
    user32 = windll.user32
    gdi32 = windll.gdi32
    gdiplus = windll.gdiplus
    _declare_prototypes(user32, gdi32, gdiplus)
    return user32, gdi32, gdiplus


def _declare_prototypes(user32: Any, gdi32: Any, gdiplus: Any) -> None:
    """Set ``restype``/``argtypes`` on every windll call (Win64 truncation guard).

    Mandatory — an undeclared ``CreateWindowExW`` truncates its 64-bit HWND to a
    32-bit ``c_int`` (catalog G1). Implemented against the validated prototype
    (`proto/windows-overlay-focus`). Fleshed out in Step 4; the declaration site
    exists now so tests can assert it is called.
    """
    # Step 3: the concrete prototype table lands with the real draw core (Step 4).
    # It is exercised by the T2 win32-only integration test (G1), not T1.


def _make_wndproc(state: dict[str, Any]) -> Any:
    """Build the ``WINFUNCTYPE`` WNDPROC callback (GC-keepalive hazard).

    The returned object MUST outlive :func:`_destroy_window` — ``WM_DESTROY``
    dispatches into it synchronously during the destroy call. It is held on the
    :class:`Overlay` (``_wndproc_ref``) until after teardown destroys the window.
    """
    return object()  # Step 3 placeholder; real WNDPROC lands in Step 4.


def _gdiplus_startup() -> Any:
    """``GdiplusStartup``; returns the token held for teardown (catalog C2)."""
    return object()


def _gdiplus_shutdown(token: Any) -> None:
    """``GdiplusShutdown`` (teardown, catalog F1)."""


def _set_dpi_awareness() -> None:
    """``SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)`` (catalog C1/C1b).

    Benign failures (``E_ACCESSDENIED`` "already set" / an absent entry point on
    down-level Windows) are swallowed *inside this shim* and the build continues
    (C1b allow-list). Every other failure raises → the build fails open (C1).
    """


def _register_class(wndproc_ref: Any) -> Any:
    """``RegisterClassExW``; returns the class atom (catalog C3).

    Register-or-reuse: an already-registered class is a success, not a failure
    (self-healing on relaunch, catalog F1b).
    """
    return object()


def _unregister_class(atom: Any, hinst: Any) -> None:
    """``UnregisterClassW`` (teardown, catalog F1/F1b)."""


def _create_window(atom: Any) -> Any:
    """``CreateWindowExW`` with the overlay ex-style set; NULL raises (catalog C4)."""
    return object()


def _destroy_window(hwnd: Any) -> None:
    """``DestroyWindow`` (teardown, catalog F1). Dispatches ``WM_DESTROY`` into
    the WNDPROC synchronously — the ``_wndproc_ref`` must still be alive here."""


def _create_dib(w: int, h: int) -> tuple[Any, Any, Any]:
    """``CreateDIBSection`` (top-down 32-bit premultiplied ARGB); NULL raises (C5).

    Returns ``(dib, hdc, bits)``.
    """
    return object(), object(), object()


def _gdip_create_from_hdc(hdc: Any) -> Any:
    """``GdipCreateFromHDC`` + smoothing/compositing mode; fail raises (catalog C6)."""
    return object()


def _paint(backbuffer: dict[str, Any], state: dict[str, Any]) -> None:
    """Render the card + bars into the backbuffer (catalog E1).

    **Step 3: no-op stub.** The GDI+ draw body (parity spec Parts 2-3, the modes'
    math ported from ``WaveformView.drawRect_``) is Step 4, gated on spike #66.
    """


def _blit(hwnd: Any, backbuffer: dict[str, Any], geom: tuple[int, int, int, int]) -> None:
    """``UpdateLayeredWindow`` (``ULW_ALPHA``, premultiplied) — first blit C7 / runtime E1."""


def _show_window(hwnd: Any, *, first: bool) -> None:
    """``ShowWindow`` without activating: ``SW_SHOWNOACTIVATE`` first, ``SW_SHOWNA`` after."""


def _hide_window(hwnd: Any) -> None:
    """``ShowWindow(SW_HIDE)``."""


def _set_window_pos(hwnd: Any, geom: tuple[int, int, int, int]) -> None:
    """``SetWindowPos(SWP_NOACTIVATE | SWP_NOZORDER)`` for the panel-shrink (catalog E4)."""


def _set_timer(hwnd: Any, timer_id: int, interval_ms: int) -> int:
    """``SetTimer``; returns the timer id (initial arm C-series / re-arm E2)."""
    return timer_id or 1


def _kill_timer(hwnd: Any, timer_id: int) -> None:
    """``KillTimer`` (teardown, catalog F1)."""


def _free_dib(backbuffer: dict[str, Any]) -> None:
    """Delete the GDI+ graphics object + DIB section (teardown, catalog F1)."""


def _monitor_geometry() -> tuple[int, int, int, int]:
    """Primary-monitor work area ``(left, top, right, bottom)`` (catalog E7)."""
    return 0, 0, 1920, 1080


# Last-good monitor work area, so a runtime geometry query failure (E7: RDP,
# headless, hotplug) keeps the last position instead of dropping the HUD.
_last_work_area: tuple[int, int, int, int] = (0, 0, 1920, 1080)


def _pump_once() -> None:
    """One pump drain: ``PeekMessageW(PM_REMOVE)`` + translate + dispatch (ADR-0008 §2)."""


def _sleep(seconds: float) -> None:
    """The pump's ~5 ms sleep (ADR-0008 §2). A shim so tests can neutralize it."""
    import time

    time.sleep(seconds)


def _placement(w: int, h: int) -> tuple[int, int, int, int]:
    """Bottom-centre placement rect on the primary monitor (parity spec Part 2).

    Win32 origin is top-left, so ``y = workArea.bottom - PANEL_H - 80`` (the
    macOS ``y=80``-from-bottom, flipped). A runtime geometry-query failure (E7)
    is swallowed and the **last-good** work area is reused — the HUD keeps its
    last position rather than vanishing.
    """
    global _last_work_area
    try:
        _last_work_area = _monitor_geometry()
    except Exception:  # noqa: BLE001
        logger.debug("monitor geometry query failed; keeping last position", exc_info=True)
    left, _top, right, bottom = _last_work_area
    x = left + ((right - left) - w) // 2
    y = bottom - h - 80
    return x, y, w, h


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------


class Overlay:
    """Handle to a live Windows overlay: the four-callable ``OverlayNotifier``
    contract (ADR-0009 §1) plus Windows-private fields.

    Duplicated per host (ADR-0009 §4): the private fields genuinely differ from
    macOS. ``set_mode``/``teardown`` default to no-ops for hand-built test
    overlays — which is exactly why the conformance test must be
    *callable-and-effectful*, not an ``inspect.signature`` check.
    """

    def __init__(
        self,
        *,
        show: Callable[[], None],
        hide: Callable[[], None],
        dispatch_main: Callable[[Callable[[], None]], None],
        set_mode: Callable[[HudMode], None] | None = None,
        teardown: Callable[[], None] | None = None,
        _hwnd: Any = None,
        _atom: Any = None,
        _hinst: Any = None,
        _timer_id: int = 0,
        _wndproc_ref: Any = None,
        _gdiplus_token: Any = None,
        _backbuffer: dict[str, Any] | None = None,
        _queue: queue.Queue[Callable[[], None]] | None = None,
        _state: dict[str, Any] | None = None,
        _first_shown: dict[str, bool] | None = None,
    ) -> None:
        self.show = show
        self.hide = hide
        self.dispatch_main = dispatch_main
        self.set_mode = set_mode if set_mode is not None else (lambda _mode: None)
        self.teardown = teardown if teardown is not None else (lambda: None)
        self._hwnd = _hwnd
        self._atom = _atom
        self._hinst = _hinst
        self._timer_id = _timer_id
        # GC-keepalive: the WNDPROC ctypes object must stay reachable until AFTER
        # the window is destroyed (WM_DESTROY dispatches into it during the call).
        self._wndproc_ref = _wndproc_ref
        self._gdiplus_token = _gdiplus_token
        self._backbuffer = _backbuffer
        self._queue = _queue
        self._state = _state
        self._first_shown = _first_shown if _first_shown is not None else {"flag": False}


def build_overlay(level_source: Callable[[], float]) -> Overlay:
    """Build the Windows overlay (transactional; fail-open catalog §3 C0).

    The **only** call inside :func:`run_hosted`'s fail-open try. Native loading is
    lazy (:func:`_load_libs`), so on a non-Windows host it raises
    ``OSError``/``AttributeError`` (catalog B2) and the caller fails open. The
    seven native build steps (fail-open §3) run in order; on **any** partial
    failure the already-allocated resources are disposed **in reverse order** and
    the exception re-raised — a failed build leaks nothing (GDI+ token, class
    atom, HWND, DIB). This is the build-time twin of teardown's ``finally``.

    *level_source* is polled on the pump thread to drive the meter (it is
    ``AppController.latest_level``).
    """
    _load_libs()  # lazy; declares prototypes. Non-Windows -> raises -> fail open (B2).

    state: dict[str, Any] = {"level": 0.0, "frame": 0, "mode": HudMode.LISTENING}
    first_shown = {"flag": False}
    w, h = _PANEL_W, _PANEL_H

    # LIFO of guarded dispose thunks for the transactional unwind (C0). Each
    # allocation appends its undo immediately after it succeeds, so a failure at
    # step N unwinds exactly steps 1..N-1 in reverse.
    undo: list[Callable[[], None]] = []
    # wndproc_ref stays a local (referenced) through the whole build; on failure
    # it must still be alive when _destroy_window runs its WM_DESTROY dispatch.
    wndproc_ref = _make_wndproc(state)
    hinst: Any = None  # TODO(step 4): capture the real HINSTANCE for register/create/unregister
    try:
        token = _gdiplus_startup()  # 1  (C2)
        undo.append(lambda: _gdiplus_shutdown(token))

        _set_dpi_awareness()  # 2  (C1/C1b — benign swallowed inside the shim; allocates nothing)

        atom = _register_class(wndproc_ref)  # 3  (C3 / F1b)
        undo.append(lambda: _unregister_class(atom, hinst))

        hwnd = _create_window(atom)  # 4  (C4)
        undo.append(lambda: _destroy_window(hwnd))

        dib, hdc, bits = _create_dib(w, h)  # 5  (C5)
        backbuffer: dict[str, Any] = {
            "dib": dib,
            "hdc": hdc,
            "bits": bits,
            "graphics": None,
            "w": w,
            "h": h,
        }
        undo.append(lambda: _free_dib(backbuffer))

        backbuffer["graphics"] = _gdip_create_from_hdc(hdc)  # 6  (C6)

        _paint(backbuffer, state)  # no-op stub in Step 3
        _blit(hwnd, backbuffer, _placement(w, h))  # 7  first blit, cleared buffer (C7)

        # D0: the initial timer arm sits pre-boundary, INSIDE build, so a failure
        # here is a plain build failure that run_hosted fails open (the frozen
        # run_hosted always returns True after run_loop, so D0 cannot fail open
        # any other way — HITL-confirmed).
        timer_id = _set_timer(hwnd, 0, _interval_ms(HudMode.LISTENING))
    except BaseException:
        # Reverse-order dispose of everything allocated so far, then re-raise.
        # wndproc_ref is still a live local, so _destroy_window's WM_DESTROY
        # dispatch is safe. Each dispose is guarded so unwind never masks the
        # original error.
        for dispose in reversed(undo):
            try:
                dispose()
            except Exception:  # noqa: BLE001
                logger.debug("overlay build unwind step failed", exc_info=True)
        raise

    q: queue.Queue[Callable[[], None]] = queue.Queue(maxsize=_DISPATCH_QUEUE_MAX)

    def _dispatch(fn: Callable[[], None]) -> None:
        # Producer side: runs on listener/worker threads. Fire-and-forget onto a
        # bounded queue (parity Part 4, fail-open E6). Never block a live producer
        # if the pump is dead — drop the newest frame and log.
        try:
            q.put_nowait(fn)
        except queue.Full:
            logger.warning("overlay dispatch queue full; dropping frame")

    def _show() -> None:
        _show_window(hwnd, first=not first_shown["flag"])
        first_shown["flag"] = True

    def _hide() -> None:
        _hide_window(hwnd)

    def _set_mode(mode: HudMode) -> None:
        # Runs on the pump thread (marshalled via _dispatch), so mutating _state
        # is single-threaded. Re-arm the redraw timer for the mode; a failed
        # re-arm is swallowed (E2 — static/last-frame HUD beats a broken run).
        state["mode"] = mode
        try:
            _set_timer(hwnd, timer_id, _interval_ms(mode))
        except Exception:  # noqa: BLE001
            logger.debug("overlay timer re-arm failed", exc_info=True)
        # Panel-shrink on active<->idle would go here (E4/E4b) once IDLE exists.

    def _teardown() -> None:
        # Deterministic, idempotent, fail-open teardown (ADR-0008 §4, parity
        # Part 4). Order: KillTimer -> DestroyWindow -> UnregisterClassW ->
        # free DIB -> GdiplusShutdown. Each step guarded so one failure never
        # skips the rest (F1) and never masks a real crash (F4). The window is
        # destroyed BEFORE wndproc_ref can be dropped (held on the Overlay).
        steps: tuple[tuple[str, Callable[[], None]], ...] = (
            ("kill_timer", lambda: _kill_timer(hwnd, timer_id)),
            ("destroy_window", lambda: _destroy_window(hwnd)),
            ("unregister_class", lambda: _unregister_class(atom, hinst)),
            ("free_dib", lambda: _free_dib(backbuffer)),
            ("gdiplus_shutdown", lambda: _gdiplus_shutdown(token)),
        )
        for step, fn in steps:
            try:
                fn()
            except Exception:  # noqa: BLE001
                logger.debug("overlay teardown step %s failed", step, exc_info=True)

    def _tick() -> None:
        # WM_TIMER body (guarded, E1): sample the level and re-blit. Runs on the
        # pump thread. Draw failures are swallowed so the pump survives.
        state["level"] = level_source()
        state["frame"] = state["frame"] + 1
        _paint(backbuffer, state)
        _blit(hwnd, backbuffer, _placement(w, h))

    backbuffer["tick"] = _tick  # kept reachable for the WM_TIMER path (Step 4)

    return Overlay(
        show=_show,
        hide=_hide,
        dispatch_main=_dispatch,
        set_mode=_set_mode,
        teardown=_teardown,
        _hwnd=hwnd,
        _atom=atom,
        _hinst=hinst,
        _timer_id=timer_id,
        _wndproc_ref=wndproc_ref,
        _gdiplus_token=token,
        _backbuffer=backbuffer,
        _queue=q,
        _state=state,
        _first_shown=first_shown,
    )


def run_with_overlay(
    controller: AppController,
    *,
    build: Callable[[Callable[[], float]], Overlay] | None = None,
    register_overlay: Callable[[Overlay], None] | None = None,
    platform: str | None = None,
) -> bool:
    """Run *controller* under a Windows GUI host that owns the main thread.

    Thin adapter over the shared :func:`seda.gui._hostloop.run_hosted` (ADR-0009
    §2): supplies the ``win32`` gate, the transactional :func:`build_overlay`, and
    the :func:`_run_win32_loop` body. Signature matches the macOS
    :func:`seda.gui.host.run_with_overlay` because ``cli.run`` calls
    ``module.run_with_overlay(controller, register_overlay=...)`` on either host.

    Returns ``True`` only if the host took over the main thread and ran the
    controller to shutdown; ``False`` (fail-open) when the overlay is unavailable
    — non-Windows or a Win32/GDI+ build failure — so the caller falls back to
    ``controller.run()``.
    """
    from seda.gui._hostloop import run_hosted

    build_fn = build if build is not None else build_overlay

    return run_hosted(
        controller,
        supports=lambda plat: plat == "win32",
        build=build_fn,
        run_loop=_run_win32_loop,
        register_overlay=register_overlay,
        platform=platform,
    )


def _run_win32_loop(
    controller: AppController,
    overlay: Overlay,
    register_overlay: Callable[[Overlay], None] | None,
) -> None:
    """Windows ``run_loop`` body for :func:`run_hosted` (past the fail-open boundary).

    Mirrors the macOS ``_run_appkit_host`` shape (ADR-0009 §2): register the
    overlay, install signal handlers, start the controller (a failure here is the
    controller's own and PROPAGATES — the boundary is *before* this, in
    ``run_hosted``), then run the interruptible pump (ADR-0008 §2), and tear the
    overlay down in a ``finally`` on every exit.

    Divergence from macOS: no separate pump timer — the ``PeekMessageW`` pump
    *is* the servicing loop (ADR-0008 §2), and the stop flag is a bare loop-local
    the signal handler sets (§3).
    """
    if register_overlay is not None:
        register_overlay(overlay)

    # Loop-local stop flag (matches macOS's loop-local stop_requested — no
    # module-level singleton, so nothing leaks across runs/tests). The signal
    # handler does the one thing a handler should: record the request.
    stop_requested = {"flag": False}

    def _request_stop(_signum: int, _frame: Any) -> None:
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    q = overlay._queue

    try:
        # Non-blocking setup: load model, start hotkeys, notify READY. A failure
        # here PROPAGATES (D1) — falling back would re-run start() and fail again.
        controller.start()
        while not stop_requested["flag"]:
            _pump_once()  # PeekMessageW(PM_REMOVE) drain; WM_TIMER -> _tick (guarded, E1)
            _drain(q)  # run enqueued dispatch_main closures (E3)
            _sleep(_PUMP_SLEEP_SECONDS)
        # Normal stop path: quiesce the controller BEFORE the window vanishes, so
        # nothing reacts to a half-torn-down window (ADR-0008 §4). Inside the try,
        # after the loop breaks — NOT in the finally (which is teardown only).
        controller.shutdown()
    finally:
        # Tear the overlay down on EVERY exit — normal stop, a controller.start()
        # crash (D1/F2), or a pump raise (D2/F3). teardown() is itself fail-open,
        # but guard again so a teardown error never masks the original crash (F4).
        try:
            overlay.teardown()
        except Exception:  # noqa: BLE001
            logger.warning("overlay teardown failed during shutdown", exc_info=True)


def _drain(q: queue.Queue[Callable[[], None]] | None) -> None:
    """Drain and run every queued ``dispatch_main`` closure (pump thread, E3).

    Each closure is guarded so a raising one never stops the drain — a broken
    overlay callback degrades to "no HUD update", dictation intact.
    """
    if q is None:
        return
    while True:
        try:
            fn = q.get_nowait()
        except queue.Empty:
            return
        try:
            fn()
        except Exception:  # noqa: BLE001
            logger.warning("dispatched overlay closure failed", exc_info=True)
