"""macOS-only integration tests for the menu-bar GUI host.

These build the **real** AppKit objects the GUI path constructs at runtime — the
overlay panel and the status-item menu — on a real Mac, catching failures no
headless unit test can: notably that every ``NSObject`` subclass defined inside
the host's functions registers without a **global ObjC class-name collision**
(``objc.error: <name> is overriding existing Objective-C class``). That collision
is invisible to CI's ``-m "not integration"`` run and to mypy — it only fires when
the classes are actually defined on macOS — so it is exactly the class of defect
this file exists to guard (regression: the gui path once defined a second
``_MainThreadRunner`` colliding with ``build_overlay``'s).

``integration`` + ``skipif(sys.platform != "darwin")``, so ordinary CI skips them;
they run via ``pytest -m integration`` on a real Mac (the macos-latest CI job or a
local run). This mirrors the Windows T2 suite (``docs/specs/windows-hud-fail-open``
§4) and the by-eye macOS boundary of ADR-0005.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only AppKit integration"),
]


def test_status_item_builds_without_objc_class_collision() -> None:
    """Build the overlay AND the status item in one process — no ObjC name clash.

    ObjC classes register process-globally by name, so two host functions each
    defining a class of the same name raise ``objc.error: ... is overriding
    existing Objective-C class`` the moment both run; and a helper method on an
    ``NSObject`` subclass with a non-selector arg signature raises
    ``BadPrototypeError`` at class-definition time. ``seda gui`` runs exactly this
    sequence (``build_overlay`` then ``_build_status_item``, which defines the
    menu's action-target classes). This reproduces it and tears down cleanly.

    NOTE: the host's builders register their ObjC classes process-globally and are
    only meant to run ONCE per process (one status item). So this single test
    exercises the whole gui construction path in one go rather than splitting into
    multiple tests that would each re-register the same classes and collide.
    """
    from AppKit import NSApplication

    import seda.gui.host as host

    # 1) build_overlay registers its ObjC classes (WaveformView, its runner, ...).
    overlay = host.build_overlay(lambda: 0.0)
    try:
        # 2) the gui path's classes (status runner, Quit/OpenLogs/Doctor targets)
        #    — this is where a duplicate class name or a bad selector prototype crashes.
        app = NSApplication.sharedApplication()
        teardown_extra = host._build_status_item(app, {"flag": False}, None)
        try:
            assert callable(teardown_extra)
        finally:
            teardown_extra()  # removes the status item; must not raise
    finally:
        overlay.teardown()
