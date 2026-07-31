"""Windows-only integration tests for the recording HUD (fail-open §4 G1/G2).

The two **latent-corruption** modes no T1 unit test can catch (`docs/specs/
windows-hud-fail-open.md` §4, catalog G1/G2). Both are ``integration`` +
``skipif(sys.platform != "win32")``, so ordinary CI's ``-m "not integration"``
run skips them; they run only on a real Windows box (``pytest -m integration``).

- **G1 — declared prototypes / no HWND truncation.** T1 stubs every native shim,
  so it can never exercise the real ``ctypes`` boundary. A missing
  ``restype``/``argtypes`` truncates a 64-bit HWND on Win64: ``CreateWindowExW``
  may *succeed* with a corrupted handle, then mis-address a later call and crash
  on the pump thread or at a teardown deref (fail-open §4 — explicitly NOT
  fail-open-able). This test builds the **real** layered window and drives the
  full ``show`` → ``set_mode`` → ``teardown`` lifecycle without raising; a
  truncated handle would fault somewhere in that sequence.

- **G2 — layered + transparent styles actually applied.** ``WS_EX_LAYERED`` /
  ``WS_EX_TRANSPARENT`` silently not sticking would make the window render opaque
  / eat clicks while raising nowhere. This reads the effective ex-style back with
  ``GetWindowLongPtrW(GWL_EXSTYLE)`` and asserts both decisive bits are present.

These mirror the by-eye macOS boundary of ADR-0005: everything provable without
hardware is a T1 unit test; only what genuinely needs a real window lands here.
"""

from __future__ import annotations

import ctypes
import sys

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only overlay integration (G1/G2)"),
]

# Ex-style bits the overlay must carry (fail-open §4 G2). Imported from the host
# module so the test tracks the shipping constants, not a hand-copied literal.
from seda.gui.host_win import WS_EX_LAYERED, WS_EX_TRANSPARENT, build_overlay  # noqa: E402

GWL_EXSTYLE = -20


def _effective_ex_style(hwnd: object) -> int:
    """Read the window's effective extended style via ``GetWindowLongPtrW``.

    Declared locally (not in the shipping module) — this is a test-only diagnostic
    read; the production host never queries its own style back. ``GetWindowLongPtrW``
    is the Win64-correct entry point (``...LongW`` truncates the LONG_PTR result).
    """
    windll = ctypes.windll  # type: ignore[attr-defined, unused-ignore]
    user32 = windll.user32
    from ctypes import wintypes

    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t  # LONG_PTR
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    return int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)) & 0xFFFFFFFF


def test_win_ctypes_prototypes_declared() -> None:
    """G1: build a real layered window and run the full lifecycle without raising.

    Exercises the real ``ctypes`` boundary end-to-end (build → show → set_mode ×
    both live modes → teardown). A truncated HWND or a mis-declared prototype
    faults somewhere in this sequence; a clean pass is the truncation backstop the
    fail-open catalog promises (§4).
    """
    from seda.notifications import HudMode

    overlay = build_overlay(lambda: 0.3)  # real GdiplusStartup + CreateWindowExW + DIB + GDI+
    try:
        overlay.show()
        overlay.set_mode(HudMode.LISTENING)  # re-arms the timer, mutates state
        overlay.set_mode(HudMode.BUSY)
        # Drive one real redraw tick (paint into the DIB + UpdateLayeredWindow) so
        # the draw + blit prototypes are exercised, not just the window lifecycle.
        tick = overlay._backbuffer["tick"] if overlay._backbuffer else None
        if tick is not None:
            tick()
        overlay.hide()
    finally:
        # Teardown order: KillTimer -> DestroyWindow -> UnregisterClassW ->
        # free DIB -> GdiplusShutdown (all guarded, fail-open).
        overlay.teardown()


def test_win_layered_transparent_style_applied() -> None:
    """G2: the created window actually carries WS_EX_LAYERED | WS_EX_TRANSPARENT.

    Read the effective ex-style back from the real HWND and assert both decisive
    click-through bits stuck (a silent drop renders opaque / eats clicks, raising
    nowhere).
    """
    overlay = build_overlay(lambda: 0.0)
    try:
        assert overlay._hwnd is not None, "build_overlay must create a real HWND"
        ex = _effective_ex_style(overlay._hwnd)
        assert ex & WS_EX_LAYERED, "WS_EX_LAYERED missing — window would render opaque"
        assert ex & WS_EX_TRANSPARENT, "WS_EX_TRANSPARENT missing — window would eat clicks"
    finally:
        overlay.teardown()
