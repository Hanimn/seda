"""Unit tests for the macOS GUI host (ADR-0001).

No AppKit, no real hardware. The host takes an injectable ``build`` (the overlay
factory), so the run-loop/signal wiring and the fail-open contract are tested
with fakes; the real AppKit panel is exercised only on-device (ADR-0005).
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from typing import Any

import pytest

from seda.gui.host import Overlay, run_with_overlay


class _FakeTimer:
    """Stand-in for the pump NSTimer; records invalidation."""

    def __init__(self) -> None:
        self.invalidated = False

    def invalidate(self) -> None:
        self.invalidated = True


@pytest.fixture(autouse=True)
def _stub_pump_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the signal-pump timer scheduler (needs Foundation, macOS only).

    Keeps every host test importable and runnable on non-macOS CI. Tests that
    care about the pump callback invoke it directly.
    """
    monkeypatch.setattr("seda.gui.host._schedule_pump", lambda _i, _cb: _FakeTimer())


class _SpyController:
    """Records host interactions and exposes the level source."""

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
    return Overlay(
        show=lambda: None,
        hide=lambda: None,
        dispatch_main=lambda fn: fn(),
        teardown=teardown,
        _panel=None,
        _view=None,
        _timer_holder={"timer": None},
    )


def test_returns_false_on_non_macos() -> None:
    ctrl = _SpyController()

    # build must never be invoked off-macOS.
    def _build(_level: Callable[[], float]) -> Overlay:  # pragma: no cover
        raise AssertionError("build must not run on non-macOS")

    assert run_with_overlay(ctrl, build=_build, platform="linux") is False  # type: ignore[arg-type]
    assert run_with_overlay(ctrl, build=_build, platform="win32") is False  # type: ignore[arg-type]


def test_returns_false_when_build_raises_importerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """AppKit import failure inside build fails open (no raise)."""

    def _build(_level: Callable[[], float]) -> Overlay:
        raise ImportError("No module named 'AppKit'")

    ctrl = _SpyController()
    assert run_with_overlay(ctrl, build=_build, platform="darwin") is False  # type: ignore[arg-type]
    assert ctrl.started is False  # never got to start()


def test_returns_false_when_appkit_module_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing AppKit module (the real non-macOS case) fails open, not raises.

    Regression: the `from AppKit import NSApplication` had been placed OUTSIDE
    the fail-open try/except, so on a non-macOS host (or broken pyobjc) the
    ModuleNotFoundError propagated instead of degrading to the terminal path.
    Simulated here by making the AppKit import fail even on macOS.
    """
    import builtins

    real_import = builtins.__import__

    def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "AppKit" or name.startswith("AppKit."):
            raise ModuleNotFoundError("No module named 'AppKit'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    ctrl = _SpyController()
    # build is the real default (build_overlay), which imports AppKit → fails.
    assert run_with_overlay(ctrl, platform="darwin") is False  # type: ignore[arg-type]
    assert ctrl.started is False


def test_never_raises_into_the_caller() -> None:
    """An unexpected error bringing up the host degrades to False, never raises."""

    def _build(_level: Callable[[], float]) -> Overlay:
        raise RuntimeError("unexpected AppKit explosion")

    ctrl = _SpyController()
    assert run_with_overlay(ctrl, build=_build, platform="darwin") is False  # type: ignore[arg-type]


def test_success_runs_the_loop_starts_controller_and_registers_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: build → register → start → NSApp.run (all faked, no AppKit)."""
    events: list[str] = []
    built = _fake_overlay()

    # Fake NSApplication so app.run() returns immediately instead of blocking.
    class _FakeApp:
        def run(self) -> None:
            events.append("run")

        def stop_(self, _sender: Any) -> None:  # noqa: N802
            events.append("stop")

    fake_appkit = types.ModuleType("AppKit")
    fake_appkit.NSApplication = type(  # type: ignore[attr-defined]
        "NSApplication", (), {"sharedApplication": staticmethod(lambda: _FakeApp())}
    )
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)
    # Don't clobber the test runner's SIGINT/SIGTERM handlers.
    monkeypatch.setattr("seda.gui.host.signal.signal", lambda *_a, **_k: None)

    registered: list[Overlay] = []
    ctrl = _SpyController()

    result = run_with_overlay(
        ctrl,  # type: ignore[arg-type]
        build=lambda _level: built,
        register_overlay=registered.append,
        platform="darwin",
    )

    assert result is True
    assert ctrl.started is True, "controller.start() must run before the loop"
    assert registered == [built], "the built overlay must be registered before the loop"
    assert events == ["run"], "the AppKit run loop must be entered"


def test_controller_start_failure_propagates_and_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the host owns the run, a controller.start() failure must NOT fall back.

    Regression for the real-run crash: a backend that fails to load raised in
    controller.start(); the old code caught it as 'overlay failed', returned
    False, and cli.run() then re-ran controller.run() -> start() -> same failure
    (a double crash, and start() run twice). The fail-open boundary now only
    covers AppKit setup, so a start() failure propagates once, cleanly.
    """
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

    class _FakeApp:
        def run(self) -> None:  # pragma: no cover - start() raises before this
            pass

        def stop_(self, _sender: Any) -> None:  # noqa: N802
            pass

    fake_appkit = types.ModuleType("AppKit")
    fake_appkit.NSApplication = type(  # type: ignore[attr-defined]
        "NSApplication", (), {"sharedApplication": staticmethod(lambda: _FakeApp())}
    )
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)
    monkeypatch.setattr("seda.gui.host.signal.signal", lambda *_a, **_k: None)

    ctrl = _FailingController()
    # The failure must propagate, NOT be swallowed into a False return.
    with pytest.raises(RuntimeError, match="backend failed to load"):
        run_with_overlay(
            ctrl,  # type: ignore[arg-type]
            build=lambda _level: _fake_overlay(),
            platform="darwin",
        )
    assert started == [1], "controller.start() must run exactly once (no fall-back retry)"


# --- native pre-warm (race prevention on the pynput listener thread) --------


def test_host_pre_warms_native_before_starting_the_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both native warmers must run on the main thread BEFORE controller.start().

    The pynput darwin listener touches the Carbon TIS context (SIGABRT hazard)
    and ``HIServices.AXIsProcessTrusted`` (PyObjC lazy-import ``funcmap.pop``
    race → ``KeyError``) on its own thread. Priming each on the main thread
    before the listener starts (i.e. before ``controller.start()``) is what
    defeats both races; lock that ordering here.
    """
    order: list[str] = []

    class _FakeApp:
        def run(self) -> None:
            order.append("run")

        def stop_(self, _sender: Any) -> None:  # noqa: N802
            pass

    fake_appkit = types.ModuleType("AppKit")
    fake_appkit.NSApplication = type(  # type: ignore[attr-defined]
        "NSApplication", (), {"sharedApplication": staticmethod(lambda: _FakeApp())}
    )
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)
    monkeypatch.setattr("seda.gui.host.signal.signal", lambda *_a, **_k: None)
    monkeypatch.setattr("seda.gui.host._warm_input_source", lambda: order.append("warm_tis"))
    monkeypatch.setattr("seda.gui.host._warm_accessibility_trust", lambda: order.append("warm_ax"))

    class _OrderingController(_SpyController):
        def start(self) -> None:
            order.append("start")
            super().start()

    ctrl = _OrderingController()
    result = run_with_overlay(
        ctrl,  # type: ignore[arg-type]
        build=lambda _level: _fake_overlay(),
        platform="darwin",
    )

    assert result is True
    # Both warmers run, and both precede start() (which precedes the run loop).
    assert order.index("warm_tis") < order.index("start")
    assert order.index("warm_ax") < order.index("start")
    assert order.index("start") < order.index("run")


def test_warm_accessibility_trust_resolves_the_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_warm_accessibility_trust`` accesses ``HIServices.AXIsProcessTrusted``.

    Resolving it once on the main thread is what caches it on the module so the
    listener thread never re-enters PyObjC's racy lazy-import path.
    """
    from seda.gui.host import _warm_accessibility_trust

    calls: list[str] = []
    fake_hiservices = types.ModuleType("HIServices")
    fake_hiservices.AXIsProcessTrusted = lambda: calls.append("axtrusted") or False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "HIServices", fake_hiservices)

    _warm_accessibility_trust()
    assert calls == ["axtrusted"], "the trust symbol must be resolved exactly once"


def test_warm_accessibility_trust_swallows_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken/absent HIServices must never raise into the host (fail-open)."""
    from seda.gui.host import _warm_accessibility_trust

    fake_hiservices = types.ModuleType("HIServices")

    def _boom() -> bool:
        raise RuntimeError("HIServices exploded")

    fake_hiservices.AXIsProcessTrusted = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "HIServices", fake_hiservices)

    # Must not raise.
    _warm_accessibility_trust()


# --- overlay teardown on shutdown (HUD must never linger) -------------------


def _appkit_stub(monkeypatch: pytest.MonkeyPatch, run_impl: Callable[[], None]) -> None:
    """Install a fake AppKit whose NSApplication.run() calls ``run_impl``."""

    class _FakeApp:
        def run(self) -> None:
            run_impl()

        def stop_(self, _sender: Any) -> None:  # noqa: N802
            pass

    fake_appkit = types.ModuleType("AppKit")
    fake_appkit.NSApplication = type(  # type: ignore[attr-defined]
        "NSApplication", (), {"sharedApplication": staticmethod(lambda: _FakeApp())}
    )
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)
    monkeypatch.setattr("seda.gui.host.signal.signal", lambda *_a, **_k: None)


def test_teardown_runs_on_normal_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """When app.run() returns normally, the overlay is torn down (HUD removed)."""
    torn: list[str] = []
    _appkit_stub(monkeypatch, run_impl=lambda: None)  # run() returns immediately

    result = run_with_overlay(
        _SpyController(),  # type: ignore[arg-type]
        build=lambda _level: _fake_overlay(teardown=lambda: torn.append("torn")),
        platform="darwin",
    )
    assert result is True
    assert torn == ["torn"], "overlay.teardown() must run when the run loop exits"


def test_teardown_runs_when_controller_start_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash in controller.start() still tears the overlay down (finally)."""
    torn: list[str] = []

    class _FailingController:
        latest_level = 0.0

        def start(self) -> None:
            raise RuntimeError("backend failed to load")

        def shutdown(self) -> None:  # pragma: no cover
            pass

        def run(self) -> None:  # pragma: no cover
            pass

    # app.run() must not be reached; start() raises first.
    _appkit_stub(monkeypatch, run_impl=lambda: torn.append("SHOULD_NOT_RUN"))

    with pytest.raises(RuntimeError, match="backend failed to load"):
        run_with_overlay(
            _FailingController(),  # type: ignore[arg-type]
            build=lambda _level: _fake_overlay(teardown=lambda: torn.append("torn")),
            platform="darwin",
        )
    assert torn == ["torn"], "teardown must run even when start() crashes before the loop"


def test_teardown_runs_when_run_loop_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exception out of app.run() still tears the overlay down."""
    torn: list[str] = []

    def _boom() -> None:
        raise RuntimeError("run loop exploded")

    _appkit_stub(monkeypatch, run_impl=_boom)

    with pytest.raises(RuntimeError, match="run loop exploded"):
        run_with_overlay(
            _SpyController(),  # type: ignore[arg-type]
            build=lambda _level: _fake_overlay(teardown=lambda: torn.append("torn")),
            platform="darwin",
        )
    assert torn == ["torn"], "teardown must run when the run loop raises"


def test_broken_teardown_does_not_mask_the_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing teardown is swallowed so the real crash still surfaces."""

    def _boom_run() -> None:
        raise RuntimeError("the real error")

    def _boom_teardown() -> None:
        raise RuntimeError("teardown also broke")

    _appkit_stub(monkeypatch, run_impl=_boom_run)

    # The ORIGINAL error must propagate, not the teardown error.
    with pytest.raises(RuntimeError, match="the real error"):
        run_with_overlay(
            _SpyController(),  # type: ignore[arg-type]
            build=lambda _level: _fake_overlay(teardown=_boom_teardown),
            platform="darwin",
        )


def test_overlay_defaults_teardown_to_noop() -> None:
    """A hand-built Overlay without teardown has a safe no-op default."""
    overlay = Overlay(
        show=lambda: None,
        hide=lambda: None,
        dispatch_main=lambda fn: fn(),
        _panel=None,
        _view=None,
        _timer_holder={"timer": None},
    )
    # Must be callable and not raise.
    overlay.teardown()


# --- signal handling under the Cocoa run loop (no hang on Ctrl-C) -----------


def test_signal_pump_shuts_down_and_stops_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop request serviced by the pump shuts the controller down and stops run().

    Regression: NSApplication.run() does not yield to Python's signal handler, so
    the app used to hang on SIGINT/SIGTERM and could only be force-killed — which
    skipped overlay teardown and left the HUD on screen. The pump timer services
    the signal, runs controller.shutdown(), and stops the loop.
    """
    events: list[str] = []
    captured: dict[str, Callable[[Any], None]] = {}
    handlers: dict[int, Callable[[int, Any], None]] = {}

    class _SpyCtrl:
        latest_level = 0.0

        def start(self) -> None:
            events.append("start")

        def shutdown(self) -> None:
            events.append("shutdown")

        def run(self) -> None:  # pragma: no cover
            pass

    class _FakeApp:
        def run(self) -> None:
            # Simulate the run loop: a SIGINT arrives, then the pump fires.
            events.append("run")
            handlers[__import__("signal").SIGINT](__import__("signal").SIGINT, None)
            captured["pump"](None)  # pump services the pending stop

        def stop_(self, _sender: Any) -> None:  # noqa: N802
            events.append("stop")

    fake_appkit = types.ModuleType("AppKit")
    fake_appkit.NSApplication = type(  # type: ignore[attr-defined]
        "NSApplication", (), {"sharedApplication": staticmethod(lambda: _FakeApp())}
    )
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)
    # Capture installed signal handlers instead of touching the real ones.
    monkeypatch.setattr(
        "seda.gui.host.signal.signal",
        lambda signum, handler: handlers.__setitem__(signum, handler),
    )
    # Capture the pump callback; return a fake timer.
    monkeypatch.setattr(
        "seda.gui.host._schedule_pump",
        lambda _i, cb: (captured.__setitem__("pump", cb), _FakeTimer())[1],
    )
    # Neutralize the wakeup-event post (needs AppKit event classes).
    monkeypatch.setattr("seda.gui.host._post_wakeup_event", lambda _app: None)

    result = run_with_overlay(
        _SpyCtrl(),  # type: ignore[arg-type]
        build=lambda _level: _fake_overlay(),
        platform="darwin",
    )

    assert result is True
    # The pump serviced the signal: controller shut down and the loop was stopped.
    assert "shutdown" in events, "pump must run controller.shutdown() on a stop request"
    assert "stop" in events, "pump must stop the run loop on a stop request"
    assert events.index("shutdown") < events.index("stop"), "shutdown before stopping the loop"


def test_pump_ignores_when_no_stop_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pump is a no-op until a signal sets the stop flag (no premature shutdown)."""
    events: list[str] = []
    captured: dict[str, Callable[[Any], None]] = {}

    class _SpyCtrl:
        latest_level = 0.0

        def start(self) -> None:
            pass

        def shutdown(self) -> None:  # pragma: no cover - must NOT run
            events.append("shutdown")

        def run(self) -> None:  # pragma: no cover
            pass

    class _FakeApp:
        def run(self) -> None:
            # Pump fires with no pending signal → must do nothing.
            captured["pump"](None)

        def stop_(self, _sender: Any) -> None:  # noqa: N802 pragma: no cover
            events.append("stop")

    fake_appkit = types.ModuleType("AppKit")
    fake_appkit.NSApplication = type(  # type: ignore[attr-defined]
        "NSApplication", (), {"sharedApplication": staticmethod(lambda: _FakeApp())}
    )
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)
    monkeypatch.setattr("seda.gui.host.signal.signal", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "seda.gui.host._schedule_pump",
        lambda _i, cb: (captured.__setitem__("pump", cb), _FakeTimer())[1],
    )

    run_with_overlay(
        _SpyCtrl(),  # type: ignore[arg-type]
        build=lambda _level: _fake_overlay(),
        platform="darwin",
    )
    assert events == [], "pump must not shut down or stop when no signal is pending"
