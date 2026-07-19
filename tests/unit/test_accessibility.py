"""Unit tests for the macOS Accessibility permission probe.

No real HIServices call: the probe is exercised via an injected ``platform`` and
a faked ``HIServices`` module, so these run on any OS.
"""

from __future__ import annotations

import sys
import types

import pytest

from seda.input.accessibility import ACCESSIBILITY_HELP, accessibility_trusted


def _fake_hiservices(monkeypatch: pytest.MonkeyPatch, *, trusted: bool | None) -> None:
    """Install a fake ``HIServices`` whose AXIsProcessTrusted returns/raises."""
    mod = types.ModuleType("HIServices")
    if trusted is None:

        def _boom() -> bool:
            raise RuntimeError("HIServices exploded")

        mod.AXIsProcessTrusted = _boom  # type: ignore[attr-defined]
    else:
        mod.AXIsProcessTrusted = lambda: trusted  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "HIServices", mod)


def test_returns_none_on_non_macos() -> None:
    # Off-macOS the answer is "unknown" — never a spurious warning.
    assert accessibility_trusted(platform="linux") is None
    assert accessibility_trusted(platform="win32") is None


def test_returns_true_when_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hiservices(monkeypatch, trusted=True)
    assert accessibility_trusted(platform="darwin") is True


def test_returns_false_when_untrusted(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hiservices(monkeypatch, trusted=False)
    assert accessibility_trusted(platform="darwin") is False


def test_returns_none_when_probe_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A broken probe is "unknown", not a crash and not a false negative.
    _fake_hiservices(monkeypatch, trusted=None)
    assert accessibility_trusted(platform="darwin") is None


def test_help_text_is_actionable() -> None:
    # The guidance must name the exact place to fix it.
    assert "Accessibility" in ACCESSIBILITY_HELP
    assert "Privacy & Security" in ACCESSIBILITY_HELP
