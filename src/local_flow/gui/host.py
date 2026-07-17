"""macOS GUI host that owns the main thread (ADR-0001).

The host is the AppKit-owning side of the main-thread inversion. On macOS with
AppKit available it will (eventually) install signal handlers, call
``controller.start()``, and block in ``NSApplication.run()`` while the overlay
draws — with ``controller.shutdown()`` invoked on quit/signal.

**Fail-open is the hard invariant** (epic #15): this module never lets a missing
or broken AppKit affect dictation. :func:`run_with_overlay` returns ``False``
whenever the overlay could not take over the main thread — non-macOS, AppKit
import failure, or (for now) the not-yet-implemented real panel — and the caller
(:func:`local_flow.cli.run`) then runs the controller's own blocking ``run()``,
which is exactly today's behavior.

Scope note (ADR-0001 implementation step): the real ``NSPanel`` / ``NSApp.run()``
body is deliberately **not** implemented here yet — it depends on the AppKit
selectors flagged ``[uncertain]`` in ``docs/research/nspanel-nonactivating-float-recipe.md``
(#17), which a ``/prototype`` must confirm on a real machine first. Until then
the darwin path fails open like any other unavailable-AppKit case, so the
threading seam and its fail-open wiring are in place and tested without any real
AppKit dependency.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from local_flow.app import AppController

logger = logging.getLogger(__name__)


def run_with_overlay(controller: AppController, *, platform: str | None = None) -> bool:
    """Try to run *controller* under a macOS GUI host that owns the main thread.

    Returns ``True`` only if the GUI host took over the main thread and ran the
    controller to shutdown. Returns ``False`` (fail-open) when the overlay is
    not available — non-macOS, AppKit import failure, or the real panel host is
    not yet implemented — so the caller can fall back to ``controller.run()``.

    ``platform`` is injectable (defaults to :data:`sys.platform`) so tests stay
    deterministic regardless of the host OS, mirroring
    :func:`local_flow.config.select_push_to_talk`.
    """
    plat = platform if platform is not None else sys.platform
    if plat != "darwin":
        # Not macOS: the overlay is macOS-only. Fall back silently.
        return False

    try:
        _run_appkit_host(controller)
    except (ImportError, OSError) as exc:
        # AppKit (pyobjc) not importable, or no window server. Fail open — the
        # caller runs the controller's own blocking loop; dictation is
        # unaffected. Mirrors the lazy-native-import guard in input/hotkeys.py.
        logger.info("overlay unavailable, falling back to terminal mode: %s", exc)
        return False
    except Exception:  # noqa: BLE001
        # Any other failure while bringing up the host must never harm
        # dictation. Log and fall back.
        logger.warning("overlay host failed, falling back to terminal mode", exc_info=True)
        return False
    return True


def _run_appkit_host(controller: AppController) -> None:
    """Own the main thread with AppKit and drive *controller* (NOT YET IMPLEMENTED).

    The real implementation will:
      1. install SIGINT/SIGTERM on the main thread; the handler calls
         ``controller.shutdown()`` then stops ``NSApp``;
      2. call ``controller.start()`` (the non-blocking setup);
      3. build the overlay ``NSPanel`` and block in ``NSApplication.run()``.

    Until #17's ``[uncertain]`` AppKit selectors are confirmed by a ``/prototype``,
    this raises ``ImportError`` so :func:`run_with_overlay` fails open to the
    terminal path. This keeps the threading seam landed and tested with no real
    AppKit dependency.
    """
    # TODO(#15 prototype): implement the real NSPanel host per ADR-0001 +
    # docs/research/nspanel-nonactivating-float-recipe.md. Raising ImportError
    # for now routes callers through the fail-open fallback.
    raise ImportError("macOS overlay host not yet implemented (pending #15 prototype)")
