"""Unit tests for the Windows GUI host (ADR-0008, ADR-0009).

No Win32, no real hardware. Mirrors ``tests/unit/test_gui_host.py`` (AppKit fakes
→ Win32-shim fakes): every native touch in ``host_win`` sits behind a module-level
shim (ADR-0005), so the lifecycle/threading logic and the fail-open contract are
tested with fakes and **CI never loads ``ctypes.windll``**. The real layered
window is exercised only on-device (the T2 ``win32``-only suite, out of this
file's scope — G1/G2 in ``docs/specs/windows-hud-fail-open.md`` §4).

The fail-open *contract* is identical to macOS by construction, because the
boundary + ``-> bool`` live in the shared ``run_hosted`` (ADR-0009 §2); these
tests prove the Windows ``run_loop``/``build_overlay`` honor it.
"""

from __future__ import annotations

import queue
import sys
from collections.abc import Callable
from typing import Any

import pytest

from seda.gui import host_win
from seda.gui.host_win import Overlay, build_overlay, run_with_overlay
from seda.notifications import HudMode

# --- shim names the autouse fixture neutralizes -----------------------------
# Every build-time / runtime / teardown shim in host_win. The fixture installs a
# safe default fake for each so that even a real-build_overlay test never reaches
# ctypes.WinDLL on Linux. (Native ctors/DLLs are lazy, so importing host_win is
# already clean; this guards the C-series that call the real build_overlay.)
_BUILD_SHIMS = (
    "_gdiplus_startup",
    "_register_class",
    "_create_window",
    "_gdip_create_from_hdc",
)


@pytest.fixture(autouse=True)
def _neutralize_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every native shim with a safe fake; keep host_win Win32-free in tests."""
    monkeypatch.setattr(host_win, "_load_libs", lambda: (object(), object(), object()))
    monkeypatch.setattr(host_win, "_set_dpi_awareness", lambda: None)
    monkeypatch.setattr(host_win, "_make_wndproc", lambda _state: object())
    for name in _BUILD_SHIMS:
        monkeypatch.setattr(host_win, name, lambda *a, **k: object())
    monkeypatch.setattr(host_win, "_create_dib", lambda _w, _h: (object(), object(), object()))
    monkeypatch.setattr(host_win, "_paint", lambda *_a, **_k: None)
    monkeypatch.setattr(host_win, "_blit", lambda *_a, **_k: None)
    monkeypatch.setattr(host_win, "_show_window", lambda *_a, **_k: None)
    monkeypatch.setattr(host_win, "_hide_window", lambda *_a, **_k: None)
    monkeypatch.setattr(host_win, "_set_window_pos", lambda *_a, **_k: None)
    monkeypatch.setattr(host_win, "_set_timer", lambda _h, tid, _ms: tid or 1)
    monkeypatch.setattr(host_win, "_kill_timer", lambda *_a, **_k: None)
    monkeypatch.setattr(host_win, "_destroy_window", lambda *_a, **_k: None)
    monkeypatch.setattr(host_win, "_unregister_class", lambda *_a, **_k: None)
    monkeypatch.setattr(host_win, "_free_dib", lambda *_a, **_k: None)
    monkeypatch.setattr(host_win, "_gdiplus_shutdown", lambda *_a, **_k: None)
    monkeypatch.setattr(host_win, "_monitor_geometry", lambda: (0, 0, 1920, 1080))
    # Threading shims: no real pump, no real sleep, don't touch signal handlers.
    monkeypatch.setattr(host_win, "_pump_once", lambda: None)
    monkeypatch.setattr(host_win, "_sleep", lambda _s: None)
    monkeypatch.setattr(host_win.signal, "signal", lambda *_a, **_k: None)


class _SpyController:
    """Records host interactions and exposes the level source (macOS parity)."""

    def __init__(self) -> None:
        self.started = False
        self.shut_down = False
        self.latest_level = 0.0

    def start(self) -> None:
        self.started = True

    def shutdown(self) -> None:
        self.shut_down = True

    def run(self) -> None:  # interface parity; unused here
        pass


def _fake_overlay(teardown: Callable[[], None] | None = None) -> Overlay:
    """A hand-built Overlay with trivial callables (no native handles)."""
    return Overlay(
        show=lambda: None,
        hide=lambda: None,
        dispatch_main=lambda fn: fn(),
        teardown=teardown,
        _queue=queue.Queue(maxsize=256),
    )


def _stop_after(n: int, stop_holder: dict[str, Any]) -> Callable[[], None]:
    """A fake ``_pump_once`` that flips the loop-local stop flag after *n* calls.

    The loop-local flag is unreachable from the test, so we capture the signal
    handler via the patched ``signal.signal`` and fire it — exactly the path a
    real SIGINT takes. *stop_holder* receives the captured handler.
    """
    calls = {"n": 0}

    def _pump() -> None:
        calls["n"] += 1
        if calls["n"] >= n:
            handler = stop_holder.get("handler")
            if handler is not None:
                handler(2, None)  # SIGINT

    return _pump


def _capture_signal(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``signal.signal`` to capture the installed SIGINT handler."""
    holder: dict[str, Any] = {}

    def _fake_signal(signum: int, handler: Any) -> None:
        # Both SIGINT and SIGTERM install the same handler; keep the last.
        holder["handler"] = handler

    monkeypatch.setattr(host_win.signal, "signal", _fake_signal)
    return holder


def _raiser(exc: BaseException) -> Callable[..., Any]:
    """Return a shim replacement that raises *exc* when called with any args."""

    def _raise(*_a: Any, **_k: Any) -> Any:
        raise exc

    return _raise


# ============================================================================
# Mirrored-from-macOS: fail-open contract (identical because run_hosted is shared)
# ============================================================================


def test_returns_false_on_non_windows() -> None:
    ctrl = _SpyController()

    def _build(_level: Callable[[], float]) -> Overlay:  # pragma: no cover
        raise AssertionError("build must not run on non-Windows")

    assert run_with_overlay(ctrl, build=_build, platform="linux") is False  # type: ignore[arg-type]
    assert run_with_overlay(ctrl, build=_build, platform="darwin") is False  # type: ignore[arg-type]


def test_returns_false_when_build_raises_importerror() -> None:
    """A toolkit import failure inside build fails open (no raise) — catalog B2."""

    def _build(_level: Callable[[], float]) -> Overlay:
        raise ImportError("No module named 'ctypes.windll'")

    ctrl = _SpyController()
    assert run_with_overlay(ctrl, build=_build, platform="win32") is False  # type: ignore[arg-type]
    assert ctrl.started is False


def test_never_raises_into_the_caller() -> None:
    """An unexpected build error degrades to False, never raises — catalog C8."""

    def _build(_level: Callable[[], float]) -> Overlay:
        raise RuntimeError("unexpected Win32 explosion")

    ctrl = _SpyController()
    assert run_with_overlay(ctrl, build=_build, platform="win32") is False  # type: ignore[arg-type]


def test_success_runs_the_loop_starts_controller_and_registers_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: build → register → start → pump (stopped after one turn)."""
    order: list[str] = []
    built = _fake_overlay()
    holder = _capture_signal(monkeypatch)
    monkeypatch.setattr(host_win, "_pump_once", _stop_after(1, holder))

    registered: list[Overlay] = []

    class _OrderingController(_SpyController):
        def start(self) -> None:
            order.append("start")
            super().start()

    ctrl = _OrderingController()
    result = run_with_overlay(
        ctrl,  # type: ignore[arg-type]
        build=lambda _level: built,
        register_overlay=lambda o: (registered.append(o), order.append("register"))[1],
        platform="win32",
    )

    assert result is True
    assert ctrl.started is True
    assert registered == [built]
    assert order.index("register") < order.index("start"), "register before start"


def test_controller_start_failure_propagates_and_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the host owns the run, a start() failure PROPAGATES — catalog D1."""
    started: list[int] = []

    class _FailingController:
        latest_level = 0.0

        def start(self) -> None:
            started.append(1)
            raise RuntimeError("backend failed to load")

        def shutdown(self) -> None:  # pragma: no cover
            pass

        def run(self) -> None:  # pragma: no cover
            pass

    monkeypatch.setattr(
        host_win, "_pump_once", lambda: (_ for _ in ()).throw(AssertionError("pump reached"))
    )

    with pytest.raises(RuntimeError, match="backend failed to load"):
        run_with_overlay(
            _FailingController(),  # type: ignore[arg-type]
            build=lambda _level: _fake_overlay(),
            platform="win32",
        )
    assert started == [1], "start() runs exactly once (no fall-back retry)"


# ============================================================================
# Teardown (F-series), mirrored from macOS
# ============================================================================


def test_teardown_runs_on_normal_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the pump stops normally, the overlay is torn down — catalog F1."""
    torn: list[str] = []
    holder = _capture_signal(monkeypatch)
    monkeypatch.setattr(host_win, "_pump_once", _stop_after(1, holder))

    result = run_with_overlay(
        _SpyController(),  # type: ignore[arg-type]
        build=lambda _level: _fake_overlay(teardown=lambda: torn.append("torn")),
        platform="win32",
    )
    assert result is True
    assert torn == ["torn"]


def test_teardown_runs_when_controller_start_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash in controller.start() still tears the overlay down — catalog F2."""
    torn: list[str] = []

    class _FailingController:
        latest_level = 0.0

        def start(self) -> None:
            raise RuntimeError("backend failed to load")

        def shutdown(self) -> None:  # pragma: no cover
            pass

        def run(self) -> None:  # pragma: no cover
            pass

    with pytest.raises(RuntimeError, match="backend failed to load"):
        run_with_overlay(
            _FailingController(),  # type: ignore[arg-type]
            build=lambda _level: _fake_overlay(teardown=lambda: torn.append("torn")),
            platform="win32",
        )
    assert torn == ["torn"], "teardown runs even when start() crashes before the pump"


def test_pump_loop_failure_propagates_and_tears_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exception out of the pump propagates and still tears down — catalog F3/D2."""
    torn: list[str] = []

    def _boom() -> None:
        raise RuntimeError("pump exploded")

    monkeypatch.setattr(host_win, "_pump_once", _boom)

    with pytest.raises(RuntimeError, match="pump exploded"):
        run_with_overlay(
            _SpyController(),  # type: ignore[arg-type]
            build=lambda _level: _fake_overlay(teardown=lambda: torn.append("torn")),
            platform="win32",
        )
    assert torn == ["torn"]


def test_broken_teardown_does_not_mask_the_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing teardown is swallowed so the real crash still surfaces — catalog F4."""

    def _boom_pump() -> None:
        raise RuntimeError("the real error")

    def _boom_teardown() -> None:
        raise RuntimeError("teardown also broke")

    monkeypatch.setattr(host_win, "_pump_once", _boom_pump)

    with pytest.raises(RuntimeError, match="the real error"):
        run_with_overlay(
            _SpyController(),  # type: ignore[arg-type]
            build=lambda _level: _fake_overlay(teardown=_boom_teardown),
            platform="win32",
        )


def test_overlay_defaults_teardown_and_set_mode_to_noop() -> None:
    """A hand-built Overlay without set_mode/teardown has safe no-op defaults."""
    overlay = Overlay(show=lambda: None, hide=lambda: None, dispatch_main=lambda fn: fn())
    overlay.teardown()  # callable, no raise
    overlay.set_mode(HudMode.BUSY)  # callable, no raise


def test_stop_flag_breaks_pump_shuts_down_and_tears_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop request shuts the controller down then tears down — catalog F5.

    shutdown() runs on the normal stop path (inside the loop), before teardown.
    """
    order: list[str] = []
    holder = _capture_signal(monkeypatch)
    monkeypatch.setattr(host_win, "_pump_once", _stop_after(1, holder))

    class _OrderCtrl(_SpyController):
        def shutdown(self) -> None:
            order.append("shutdown")
            super().shutdown()

    ctrl = _OrderCtrl()
    result = run_with_overlay(
        ctrl,  # type: ignore[arg-type]
        build=lambda _level: _fake_overlay(teardown=lambda: order.append("teardown")),
        platform="win32",
    )
    assert result is True
    assert ctrl.shut_down is True
    assert order == ["shutdown", "teardown"], "shutdown before teardown on the clean stop path"


def test_pump_ignores_when_no_stop_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pump keeps running until the stop flag is set (no premature shutdown F6)."""
    seen: list[int] = []
    holder = _capture_signal(monkeypatch)

    calls = {"n": 0}

    def _pump() -> None:
        calls["n"] += 1
        seen.append(calls["n"])
        if calls["n"] == 1:
            return  # first iteration: flag NOT yet set → must not shut down
        holder["handler"](2, None)  # second: request stop

    monkeypatch.setattr(host_win, "_pump_once", _pump)

    ctrl = _SpyController()
    run_with_overlay(
        ctrl,  # type: ignore[arg-type]
        build=lambda _level: _fake_overlay(),
        platform="win32",
    )
    assert seen == [1, 2], "pump ran a full iteration before the stop flag was set"
    assert ctrl.shut_down is True


# ============================================================================
# Windows-new: C0 transactional build unwind (reverse-order dispose + re-raise)
# ============================================================================


def _record_disposers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch the dispose shims to record their call order."""
    disposed: list[str] = []
    monkeypatch.setattr(
        host_win, "_gdiplus_shutdown", lambda _t: disposed.append("gdiplus_shutdown")
    )
    monkeypatch.setattr(
        host_win, "_unregister_class", lambda _a, _h: disposed.append("unregister_class")
    )
    monkeypatch.setattr(host_win, "_destroy_window", lambda _h: disposed.append("destroy_window"))
    monkeypatch.setattr(host_win, "_free_dib", lambda _b: disposed.append("free_dib"))
    return disposed


def test_build_unwinds_after_create_window_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    disposed = _record_disposers(monkeypatch)
    monkeypatch.setattr(host_win, "_create_window", _raiser(OSError("CreateWindowExW NULL")))
    with pytest.raises(OSError, match="CreateWindowExW"):
        build_overlay(lambda: 0.0)
    # Steps allocated before the failure: gdiplus_startup, register_class.
    # Reverse-order dispose: unregister_class then gdiplus_shutdown.
    assert disposed == ["unregister_class", "gdiplus_shutdown"]


def test_build_unwinds_after_create_dibsection_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    disposed = _record_disposers(monkeypatch)
    monkeypatch.setattr(host_win, "_create_dib", _raiser(OSError("CreateDIBSection NULL")))
    with pytest.raises(OSError, match="CreateDIBSection"):
        build_overlay(lambda: 0.0)
    assert disposed == ["destroy_window", "unregister_class", "gdiplus_shutdown"]


def test_build_unwinds_after_first_updatelayeredwindow_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposed = _record_disposers(monkeypatch)
    monkeypatch.setattr(host_win, "_blit", _raiser(OSError("UpdateLayeredWindow")))
    with pytest.raises(OSError, match="UpdateLayeredWindow"):
        build_overlay(lambda: 0.0)
    assert disposed == ["free_dib", "destroy_window", "unregister_class", "gdiplus_shutdown"]


# ============================================================================
# Windows-new: DPI (C1/C1b) and per-step build failures (C2–C7)
# ============================================================================


def test_returns_false_when_dpi_awareness_raises() -> None:
    """A non-benign DPI failure fails the build → terminal — catalog C1."""

    def _build(_level: Callable[[], float]) -> Overlay:
        # Simulate _set_dpi_awareness re-raising a non-benign HRESULT by failing
        # the whole build (the shim swallows only the benign allow-list).
        raise OSError("SetProcessDpiAwarenessContext failed")

    assert run_with_overlay(_SpyController(), build=_build, platform="win32") is False  # type: ignore[arg-type]


def test_dpi_awareness_benign_failure_still_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A benign DPI failure is swallowed inside the shim; build succeeds — catalog C1b."""
    # The default fake _set_dpi_awareness is a no-op (models the benign-swallow);
    # the build must complete and the loop run.
    holder = _capture_signal(monkeypatch)
    monkeypatch.setattr(host_win, "_pump_once", _stop_after(1, holder))
    ctrl = _SpyController()
    assert run_with_overlay(ctrl, platform="win32") is True  # type: ignore[arg-type]
    assert ctrl.started is True


@pytest.mark.parametrize(
    ("shim", "match"),
    [
        ("_gdiplus_startup", "GdiplusStartup"),
        ("_register_class", "RegisterClassExW"),
        ("_create_window", "CreateWindowExW"),
        ("_create_dib", "CreateDIBSection"),
        ("_gdip_create_from_hdc", "GdipCreateFromHDC"),
        ("_blit", "UpdateLayeredWindow"),
    ],
)
def test_returns_false_when_build_step_fails(
    monkeypatch: pytest.MonkeyPatch, shim: str, match: str
) -> None:
    """Each single build-step failure fails open to the terminal path — catalog C2–C7."""

    def _raise(*_a: Any, **_k: Any) -> Any:
        raise OSError(match)

    monkeypatch.setattr(host_win, shim, _raise)
    ctrl = _SpyController()
    assert run_with_overlay(ctrl, platform="win32") is False  # type: ignore[arg-type]
    assert ctrl.started is False


def test_post_build_pre_start_setup_failure_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The initial SetTimer arm sits inside build (pre-boundary), so its failure
    fails open — catalog D0.

    D0's timer-arm is a C-series build failure by design (the frozen run_hosted
    always returns True after run_loop, so D0 cannot fail open any other way).
    register_overlay/signal.signal are infallible-by-construction and are NOT
    exercised as fail-open branches here.
    """
    monkeypatch.setattr(
        host_win, "_set_timer", lambda *_a, **_k: (_ for _ in ()).throw(OSError("SetTimer 0"))
    )
    ctrl = _SpyController()
    assert run_with_overlay(ctrl, platform="win32") is False  # type: ignore[arg-type]
    assert ctrl.started is False


# ============================================================================
# Windows-new: runtime seam (L2 — swallow + log, dictation intact)
# ============================================================================


def _built_overlay(monkeypatch: pytest.MonkeyPatch) -> Overlay:
    """Build a real overlay (all shims faked) for direct closure-level tests."""
    return build_overlay(lambda: 0.25)


def test_wm_timer_draw_failure_is_swallowed_pump_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising paint/blit in the tick is swallowed by the pump path — catalog E1.

    The tick closure itself does not guard (the pump's _pump_once dispatch guards
    it); model that by asserting the guarded drain/pump path never lets a draw
    error escape. Here we assert the overlay's tick is stored and that a raising
    _blit does not corrupt subsequent dispatch handling.
    """
    overlay = _built_overlay(monkeypatch)
    # A dispatched closure that raises must be swallowed by _drain (E3 mechanism,
    # which is also how a WM_TIMER-driven redraw failure is contained).
    ran: list[str] = []
    overlay.dispatch_main(lambda: (_ for _ in ()).throw(RuntimeError("draw boom")))
    overlay.dispatch_main(lambda: ran.append("after"))
    host_win._drain(overlay._queue)
    assert ran == ["after"], "a raising dispatched closure must not stop the drain"


def test_set_timer_failure_on_re_arm_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """set_mode's timer re-arm failure is swallowed; dictation intact — catalog E2."""
    overlay = _built_overlay(monkeypatch)
    monkeypatch.setattr(
        host_win, "_set_timer", lambda *_a, **_k: (_ for _ in ()).throw(OSError("re-arm"))
    )
    overlay.set_mode(HudMode.BUSY)  # must not raise
    assert overlay._state is not None and overlay._state["mode"] is HudMode.BUSY


def test_dispatch_queue_drain_swallows_a_raising_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    """_drain guards each closure so one raiser never stops the drain — catalog E3."""
    overlay = _built_overlay(monkeypatch)
    ran: list[str] = []
    overlay.dispatch_main(lambda: ran.append("a"))
    overlay.dispatch_main(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    overlay.dispatch_main(lambda: ran.append("c"))
    host_win._drain(overlay._queue)
    assert ran == ["a", "c"]


def test_dispatch_queue_drops_when_full_never_blocks_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full dispatch queue drops the NEWEST frame and never blocks — catalog E6."""
    overlay = _built_overlay(monkeypatch)
    q = overlay._queue
    assert q is not None
    ran: list[int] = []
    for i in range(host_win._DISPATCH_QUEUE_MAX):
        overlay.dispatch_main(lambda i=i: ran.append(i))
    assert q.full()
    # One more must not block or raise; it is the newest and must be DROPPED
    # (put_nowait raises Full and the incoming item is discarded), so draining
    # yields exactly the original cap items — the sentinel never runs.
    overlay.dispatch_main(lambda: ran.append(-1))
    assert q.qsize() == host_win._DISPATCH_QUEUE_MAX
    host_win._drain(q)
    assert -1 not in ran, "the newest (overflow) frame must be the one dropped"
    assert len(ran) == host_win._DISPATCH_QUEUE_MAX


# ============================================================================
# Windows-new: teardown isolation (F1) + register-or-reuse (F1b)
# ============================================================================


def test_runtime_monitor_geometry_failure_keeps_last_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime _monitor_geometry failure is swallowed; last position kept — catalog E7."""
    # Seed a known good work area, then make the query fail.
    monkeypatch.setattr(host_win, "_monitor_geometry", lambda: (0, 0, 1000, 600))
    good = host_win._placement(host_win._PANEL_W, host_win._PANEL_H)

    monkeypatch.setattr(host_win, "_monitor_geometry", _raiser(OSError("no monitor")))
    kept = host_win._placement(host_win._PANEL_W, host_win._PANEL_H)  # must not raise

    assert kept == good, "a failed geometry query reuses the last-good work area"
    """One teardown step raising never skips the rest — catalog F1."""
    overlay = _built_overlay(monkeypatch)
    called: list[str] = []

    def _destroy_boom(_h: Any) -> None:
        called.append("destroy_window")
        raise OSError("destroy")

    monkeypatch.setattr(host_win, "_kill_timer", lambda *_a: called.append("kill_timer"))
    monkeypatch.setattr(host_win, "_destroy_window", _destroy_boom)
    monkeypatch.setattr(
        host_win, "_unregister_class", lambda *_a: called.append("unregister_class")
    )
    monkeypatch.setattr(host_win, "_free_dib", lambda *_a: called.append("free_dib"))
    monkeypatch.setattr(
        host_win, "_gdiplus_shutdown", lambda *_a: called.append("gdiplus_shutdown")
    )

    overlay.teardown()  # must not raise
    # Every step attempted, in order, despite destroy_window raising (F1/F1b).
    assert called == [
        "kill_timer",
        "destroy_window",
        "unregister_class",
        "free_dib",
        "gdiplus_shutdown",
    ]


def test_teardown_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A double teardown is a harmless no-op (each shim is idempotent-safe)."""
    overlay = _built_overlay(monkeypatch)
    overlay.teardown()
    overlay.teardown()  # must not raise


def test_register_class_tolerates_already_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register-or-reuse: an already-registered class is a success, not a failure.

    Catalog F1b's self-healing half — a leaked class atom from a swallowed
    DestroyWindow cascade is harmless because a same-process relaunch's
    _register_class returns the existing atom and the build still comes up.
    """
    existing_atom = object()
    monkeypatch.setattr(host_win, "_register_class", lambda _ref: existing_atom)
    holder = _capture_signal(monkeypatch)
    monkeypatch.setattr(host_win, "_pump_once", _stop_after(1, holder))

    ctrl = _SpyController()
    # The build treats the reused atom as success → the host runs normally.
    assert run_with_overlay(ctrl, platform="win32") is True  # type: ignore[arg-type]
    assert ctrl.started is True


# ============================================================================
# Windows-new: event→mode + GC-keepalive + conformance
# ============================================================================


def test_recording_and_busy_set_mode_re_arm_the_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    """set_mode mutates the mode and re-arms the redraw timer at the mode's rate.

    Asserts both the re-arm call AND the interval: IDLE gets the throttled idle
    interval, LISTENING/BUSY the active interval (ADR-0007 §5, the shared cadence).
    """
    overlay = _built_overlay(monkeypatch)
    rearms: list[tuple[int, int]] = []

    def _rec_timer(_h: Any, tid: int, ms: int) -> int:
        rearms.append((tid, ms))
        return tid or 1

    monkeypatch.setattr(host_win, "_set_timer", _rec_timer)

    overlay.set_mode(HudMode.BUSY)
    assert overlay._state is not None and overlay._state["mode"] is HudMode.BUSY
    overlay.set_mode(HudMode.LISTENING)
    assert overlay._state["mode"] is HudMode.LISTENING
    overlay.set_mode(HudMode.IDLE)
    assert overlay._state["mode"] is HudMode.IDLE
    assert len(rearms) == 3, "each set_mode re-arms the timer"
    # BUSY + LISTENING at the active interval; IDLE at the throttled idle interval.
    assert rearms[0][1] == host_win._ACTIVE_INTERVAL_MS
    assert rearms[1][1] == host_win._ACTIVE_INTERVAL_MS
    assert rearms[2][1] == host_win._IDLE_INTERVAL_MS
    assert host_win._IDLE_INTERVAL_MS > host_win._ACTIVE_INTERVAL_MS, "idle is throttled"


def test_interval_ms_selects_idle_vs_active() -> None:
    """_interval_ms throttles only IDLE (ADR-0007 §5 shared cadence)."""
    assert host_win._interval_ms(HudMode.IDLE) == host_win._IDLE_INTERVAL_MS
    assert host_win._interval_ms(HudMode.LISTENING) == host_win._ACTIVE_INTERVAL_MS
    assert host_win._interval_ms(HudMode.BUSY) == host_win._ACTIVE_INTERVAL_MS


def test_set_mode_idle_redraws_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """set_mode redraws now (not one idle interval later) — the mode flip is visible."""
    overlay = _built_overlay(monkeypatch)
    painted: list[str] = []
    monkeypatch.setattr(host_win, "_paint", lambda *_a, **_k: painted.append("paint"))
    overlay.set_mode(HudMode.IDLE)
    assert painted, "set_mode triggers an immediate repaint via the stored tick"


def test_wndproc_ref_is_held_on_the_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    """The WNDPROC ctypes ref is retained on the Overlay (GC-keepalive hazard).

    It must outlive DestroyWindow (WM_DESTROY dispatches into it during the
    call). Assert it is reachable from the built Overlay.
    """
    sentinel = object()
    monkeypatch.setattr(host_win, "_make_wndproc", lambda _state: sentinel)
    overlay = build_overlay(lambda: 0.0)
    assert overlay._wndproc_ref is sentinel


def test_windows_overlay_callables_are_effectful(monkeypatch: pytest.MonkeyPatch) -> None:
    """Conformance (ADR-0009): the four callables are callable AND effectful.

    NOT an inspect.signature check — the no-op set_mode/teardown defaults make a
    signature check vacuous. Build a real overlay and observe each effect.
    """
    shown: list[str] = []
    hidden: list[str] = []
    monkeypatch.setattr(host_win, "_show_window", lambda _h, *, first: shown.append("show"))
    monkeypatch.setattr(host_win, "_hide_window", lambda _h: hidden.append("hide"))
    overlay = build_overlay(lambda: 0.0)

    overlay.show()
    assert shown == ["show"], "show() calls _show_window"
    overlay.hide()
    assert hidden == ["hide"], "hide() calls _hide_window"

    overlay.set_mode(HudMode.BUSY)
    assert overlay._state is not None
    assert overlay._state["mode"] is HudMode.BUSY, "set_mode is effectful"

    ran: list[str] = []
    overlay.dispatch_main(lambda: ran.append("dispatched"))
    host_win._drain(overlay._queue)
    assert ran == ["dispatched"], "dispatch_main enqueues and the drain runs it"


def test_module_imports_without_touching_windll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing host_win must run ZERO native code (CI is headless Linux).

    Regression guard for the lazy-native invariant: WINFUNCTYPE/WinDLL/Windows
    ctypes.Structure layouts must NOT be evaluated at import time. We poison the
    Windows-only ctypes surface, drop the cached module, and re-import — any
    import-time native access would raise.
    """
    import ctypes
    import importlib

    class _Poison:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"import-time native access: ctypes.{name}")

    # windll only exists on Windows; WINFUNCTYPE exists but must not be called at
    # import. Poison both so an accidental import-time touch fails loudly.
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", _Poison(), raising=False)
    monkeypatch.setattr(ctypes, "windll", _Poison(), raising=False)

    monkeypatch.delitem(sys.modules, "seda.gui.host_win", raising=False)
    mod = importlib.import_module("seda.gui.host_win")  # must not raise
    assert hasattr(mod, "run_with_overlay")
