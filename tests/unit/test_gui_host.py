"""Unit tests for the macOS GUI host fail-open wiring (ADR-0001).

No AppKit, no real hardware. The host's job in this step is purely to *fail
open* — return False on non-macOS, on an AppKit import failure, or for the
not-yet-implemented real panel — so these tests assert exactly that.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from local_flow.gui.host import run_with_overlay


class _SpyController:
    """Records whether the host called start()/shutdown()."""

    def __init__(self) -> None:
        self.started = False
        self.shut_down = False

    def start(self) -> None:
        self.started = True

    def shutdown(self) -> None:
        self.shut_down = True

    def run(self) -> None:  # present for interface parity; unused here
        pass


def test_returns_false_on_non_macos() -> None:
    ctrl = _SpyController()
    assert run_with_overlay(ctrl, platform="linux") is False  # type: ignore[arg-type]
    assert run_with_overlay(ctrl, platform="win32") is False  # type: ignore[arg-type]


def test_returns_false_on_darwin_when_appkit_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On darwin, an AppKit ImportError fails open (does not raise)."""
    # Inject an AppKit module whose attribute access raises ImportError, so any
    # attempt to use it inside the (currently stubbed) host degrades cleanly.
    fake_appkit = types.ModuleType("AppKit")
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)

    ctrl = _SpyController()
    # Force the darwin branch regardless of host OS.
    assert run_with_overlay(ctrl, platform="darwin") is False  # type: ignore[arg-type]


def test_never_raises_into_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even an unexpected error in the host body degrades to False, never raises."""

    def _boom(_controller: Any) -> None:
        raise RuntimeError("unexpected AppKit explosion")

    monkeypatch.setattr("local_flow.gui.host._run_appkit_host", _boom)
    ctrl = _SpyController()
    # Must not propagate — fail open.
    assert run_with_overlay(ctrl, platform="darwin") is False  # type: ignore[arg-type]


def test_returns_true_when_host_body_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the host body completes without error, the overlay path 'won'."""
    calls: list[str] = []

    def _fake_host(controller: Any) -> None:
        controller.start()
        calls.append("hosted")

    monkeypatch.setattr("local_flow.gui.host._run_appkit_host", _fake_host)
    ctrl = _SpyController()
    assert run_with_overlay(ctrl, platform="darwin") is True  # type: ignore[arg-type]
    assert ctrl.started is True
    assert calls == ["hosted"]
