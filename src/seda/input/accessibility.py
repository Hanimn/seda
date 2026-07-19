"""macOS Accessibility (input-monitoring) permission probe.

Global hotkey capture on macOS requires the running process to be granted
**Accessibility** permission (System Settings → Privacy & Security →
Accessibility). Without it, pynput's event tap is installed but receives no key
events — so push-to-talk silently does nothing. pynput logs a single cryptic
line ("This process is not trusted!") and carries on; users have no idea their
terminal needs a permission toggle.

This module exposes a tiny, **non-prompting** probe of that permission state so
the app can surface a clear, actionable message at ``run`` startup and in
``doctor``. It calls ``HIServices.AXIsProcessTrusted()`` — the exact API pynput
itself checks — which returns a bool and never triggers a system dialog. (The
prompting variant, ``AXIsProcessTrustedWithOptions`` with
``kAXTrustedCheckOptionPrompt``, is deliberately **not** used: a background
dictation tool must never pop an unsolicited permission dialog.)

Everything here is best-effort and fail-open: on any non-macOS platform, or if
the probe cannot run, :func:`accessibility_trusted` returns ``None`` ("unknown")
and callers stay silent rather than emit a misleading warning.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

# Shown at run startup and reused (trimmed) by the doctor permission check, so
# the guidance lives in exactly one place.
ACCESSIBILITY_HELP = (
    "macOS Accessibility permission is not granted for this process, so global "
    "hotkeys will not work (push-to-talk will do nothing).\n"
    "  Grant it in System Settings → Privacy & Security → Accessibility:\n"
    "  add and enable the app you launch seda from (Terminal, iTerm, your\n"
    "  IDE, or the Python binary), then restart seda.\n"
    "  See docs/TROUBLESHOOTING.md for details."
)


def accessibility_trusted(platform: str | None = None) -> bool | None:
    """Whether this process is trusted for macOS Accessibility (input monitoring).

    Returns ``True``/``False`` on macOS when the state could be probed, or
    ``None`` ("unknown") on non-macOS platforms or if the probe itself failed —
    so callers can stay silent rather than warn on a guess. ``platform`` is
    injectable for tests (defaults to :data:`sys.platform`).

    Uses the non-prompting ``HIServices.AXIsProcessTrusted()``; it never shows a
    system dialog.
    """
    plat = platform if platform is not None else sys.platform
    if plat != "darwin":
        return None
    try:
        import HIServices

        return bool(HIServices.AXIsProcessTrusted())
    except Exception:  # noqa: BLE001 - probe is best-effort; unknown on failure
        logger.debug("could not probe macOS accessibility trust", exc_info=True)
        return None
