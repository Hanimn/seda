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

from local_flow.gui.host import Overlay, run_with_overlay


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


def _fake_overlay() -> Overlay:
    return Overlay(
        show=lambda: None,
        hide=lambda: None,
        dispatch_main=lambda fn: fn(),
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
    monkeypatch.setattr("local_flow.gui.host.signal.signal", lambda *_a, **_k: None)

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
    monkeypatch.setattr("local_flow.gui.host.signal.signal", lambda *_a, **_k: None)

    ctrl = _FailingController()
    # The failure must propagate, NOT be swallowed into a False return.
    with pytest.raises(RuntimeError, match="backend failed to load"):
        run_with_overlay(
            ctrl,  # type: ignore[arg-type]
            build=lambda _level: _fake_overlay(),
            platform="darwin",
        )
    assert started == [1], "controller.start() must run exactly once (no fall-back retry)"
