"""Shared overlay-host lifecycle skeleton (ADR-0009 §2).

The recording HUD ships on macOS (:mod:`seda.gui.host`, AppKit) and is being
brought to Windows (:mod:`seda.gui.host_win`, raw Win32). The two hosts share no
run-loop *body* — macOS blocks opaquely in ``NSApplication.run()``; Windows is
its own interruptible ``PeekMessageW`` poll (ADR-0008) — but they share one
**invariant control flow**:

    gate → build-in-try → ``False`` (fail open) → register → ``controller.start()``
    → block/pump the GUI loop → on stop ``controller.shutdown()`` → ``finally:``
    tear the window down.

This module owns that flow in :func:`run_hosted` so it is written **once**, not
copy-pasted per platform — it is the home of the #37/#38 lingering-HUD invariant
and the epic-#15 fail-open boundary. Each host supplies its own ``supports`` gate,
``build`` factory, and ``run_loop`` body; :func:`run_hosted` guarantees the
fail-open boundary and the ``-> bool`` contract around them.

**The fail-open boundary is exact** (ADR-0009 §2, and the macOS
:func:`seda.gui.host.run_with_overlay` it generalizes): the ``build`` call is the
*only* thing inside the fail-open try — a raise there returns ``False`` for a
clean retry, because ``controller.start()`` has not run yet. Everything the
``run_loop`` does (register, start, pump, teardown) is **past** the boundary: a
failure there is the controller's own and **propagates**, exactly as
``controller.run()`` would surface it on the terminal path. Falling back would
only re-run ``start()`` and fail identically.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from seda.app import AppController

logger = logging.getLogger(__name__)

# The overlay struct is duplicated per host (ADR-0009 §4) — macOS and Windows
# each define their own ``Overlay``; only the four-callable ``OverlayNotifier``
# shape is the shared contract. ``run_hosted`` is therefore generic over the
# concrete overlay type: each host's ``build``/``run_loop``/``register_overlay``
# agree on one ``Overlay`` and mypy infers which per call site.
_Overlay = TypeVar("_Overlay")


def run_hosted(
    controller: AppController,
    *,
    supports: Callable[[str], bool],
    build: Callable[[Callable[[], float]], _Overlay],
    run_loop: Callable[[AppController, _Overlay, Callable[[_Overlay], None] | None], None],
    register_overlay: Callable[[_Overlay], None] | None = None,
    platform: str | None = None,
) -> bool:
    """Run *controller* under a GUI host that owns the main thread (fail-open).

    Returns ``True`` only if the GUI host took over the main thread and ran the
    controller to shutdown; ``False`` (fail open) when the overlay is
    unavailable — an unsupported platform or a toolkit acquire/build failure — so
    the caller (:func:`seda.cli.run`) can fall back to ``controller.run()``.

    - *supports* is the per-host platform gate (e.g. ``darwin`` on macOS). A
      falsy result fails open before any toolkit is touched.
    - *build* constructs the overlay from a level source
      (``controller.latest_level``). It is the **only** call inside the fail-open
      try: an ``ImportError``/``OSError`` (toolkit absent) or any other exception
      degrades to ``False``, never propagates.
    - *run_loop* is the per-platform body run **past** the fail-open boundary. It
      receives ``(controller, overlay, register_overlay)`` and must:
      ``register_overlay(overlay)`` (if given) → ``controller.start()`` →
      block/pump its GUI loop → on stop ``controller.shutdown()`` →
      ``finally: overlay.teardown()``. A failure here propagates.
    - *platform* is injectable (defaults to :data:`sys.platform`).
    """
    plat = platform if platform is not None else sys.platform
    if not supports(plat):
        return False

    # Fail-open covers ONLY acquiring the toolkit + building the overlay. If that
    # fails, the overlay is unavailable and the caller safely falls back to
    # controller.run() — the controller has NOT been started yet, so a retry is
    # clean. The toolkit import lives INSIDE build(): on a host without it (or a
    # broken install) build raises ImportError/ModuleNotFoundError, which must
    # fail open rather than propagate.
    try:
        overlay = build(lambda: controller.latest_level)
    except (ImportError, OSError) as exc:
        logger.info("overlay unavailable, falling back to terminal mode: %s", exc)
        return False
    except Exception:  # noqa: BLE001
        logger.warning("overlay setup failed, falling back to terminal mode", exc_info=True)
        return False

    # Past this point the host OWNS the run: run_loop installs signals, starts the
    # controller, and blocks/pumps its GUI loop. A failure here (e.g. the backend
    # failing to load in controller.start()) is the controller's own error, not an
    # overlay problem — it must NOT fall back to controller.run() (that would
    # re-run start() and fail again). Let it propagate, exactly as controller.run()
    # would surface the same error on the terminal path.
    run_loop(controller, overlay, register_overlay)
    return True
