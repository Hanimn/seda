"""User notifications and feedback (IMPLEMENTATION_PLAN.md §18).

Only console notifications are implemented in Phase 3.  Sound playback
is deferred to Phase 7/8 hardening.  Output lines contain metadata only —
never transcript text, clipboard contents, or secrets.
"""

from __future__ import annotations

import logging
import math
import sys
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Any, Protocol, TextIO, runtime_checkable

logger = logging.getLogger(__name__)


class NotificationEvent(StrEnum):
    """Every event the application can surface to the user."""

    READY = "READY"
    RECORDING = "RECORDING"
    TRANSCRIBING = "TRANSCRIBING"
    CANCELLED = "CANCELLED"
    BUSY = "BUSY"
    ERROR = "ERROR"
    SUCCESS = "SUCCESS"


class HudMode(StrEnum):
    """The overlay's visual mode (ADR-0006 §1, extended by ADR-0007 §1).

    ``IDLE`` draws the compressed at-rest pill (the sustained resting look while
    the app is up but not dictating); ``LISTENING`` draws the live mic-level EQ
    bars; ``BUSY`` draws the time-driven "working" pulse shown after key-release.
    Crossing the :class:`OverlayNotifier` seam as a small enum (not a bare string)
    mirrors :class:`NotificationEvent` house style and lets the type checker catch
    typos.
    """

    IDLE = "idle"
    LISTENING = "listening"
    BUSY = "busy"


# ---------------------------------------------------------------------------
# Shared HUD knobs (ADR-0007 §5). These are ONE pair/family shared by BOTH the
# macOS (``gui/host.py``) and Windows (``gui/host_win.py``) hosts — a per-platform
# copy would let the idle CPU rate and the IDLE shimmer cadence silently diverge,
# which §5 forbids. Both hosts import from here; neither hardcodes its own.
# ---------------------------------------------------------------------------

# Redraw cadence: ~60 Hz while a motion-carrying mode is active (LISTENING/BUSY),
# throttled to ~10 Hz in IDLE (a slow shimmer needs no more), cutting idle wakeups
# ~6×. Re-armed on every ``set_mode``. Tune-by-eye, but one shared pair.
HUD_ACTIVE_HZ = 60
HUD_IDLE_HZ = 10

# IDLE compressed-pill visual (the #56 prototype winner, verified by eye on
# ``proto/idle-visual``): a short horizontal capsule with a faint slow alpha
# breath — floor well above zero so it reads "alive at rest", not pulsing like
# BUSY. The parity spec's pre-prototype "≈0.4/0.1" estimate is superseded by these.
HUD_IDLE_SHIMMER_BASE = 0.55
HUD_IDLE_SHIMMER_AMP = 0.15
HUD_IDLE_SHIMMER_PERIOD_S = 2.6

# Geometry family (px). Active panel is the shipped 160×48; IDLE shrinks to 48×24
# (macOS panel-shrink; Windows renders the pill in the full panel this pass).
HUD_ACTIVE_PANEL_W = 160
HUD_ACTIVE_PANEL_H = 48
HUD_IDLE_PANEL_W = 48
HUD_IDLE_PANEL_H = 24
HUD_IDLE_PILL_W = 28
HUD_IDLE_PILL_H = 6


def hud_redraw_hz(mode: HudMode) -> int:
    """The shared redraw rate for *mode* (ADR-0007 §5): active vs idle."""
    return HUD_IDLE_HZ if mode is HudMode.IDLE else HUD_ACTIVE_HZ


def hud_phase_seconds(frame: int, mode: HudMode) -> float:
    """Convert a redraw-frame counter to wall-clock seconds for *mode*.

    The frame counter advances once per redraw tick, so at the ~10 Hz idle rate it
    runs 6× slower than at ~60 Hz active. Dividing by the *mode's* rate makes the
    shimmer period (``HUD_IDLE_SHIMMER_PERIOD_S``) a real 2.6 s in every mode
    instead of stretching 6× in IDLE — the animation cadence stays a shared knob,
    decoupled from the CPU-saving throttle (ADR-0007 §5).
    """
    return frame / hud_redraw_hz(mode)


def hud_idle_shimmer(frame: int, mode: HudMode = HudMode.IDLE) -> float:
    """The IDLE pill's alpha at *frame* — a slow low-amplitude breath (ADR-0007 §5).

    Shared by both hosts so the resting shimmer looks identical cross-platform.
    """
    phase = hud_phase_seconds(frame, mode)
    return HUD_IDLE_SHIMMER_BASE + HUD_IDLE_SHIMMER_AMP * math.sin(
        phase * (2.0 * math.pi / HUD_IDLE_SHIMMER_PERIOD_S)
    )


# Menu-bar status surface (#87). The macOS status item shows the same three
# HudMode states the HUD does; these two pure helpers are the single home for its
# label + glyph so the AppKit side (host._build_status_item) stays a thin
# consumer and the mapping is unit-testable without AppKit. macOS-agnostic on
# purpose — pure Python, importable on any platform / in CI.
_STATUS_LABELS = {HudMode.IDLE: "Idle", HudMode.LISTENING: "Listening", HudMode.BUSY: "Busy"}
# SF Symbol names (present on macOS 11+). A distinct glyph per state so the menu
# bar reads at a glance; the AppKit side falls back to text if a name is missing.
_STATUS_SYMBOLS = {
    HudMode.IDLE: "circle",
    HudMode.LISTENING: "waveform",
    HudMode.BUSY: "hourglass",
}


def status_label(mode: HudMode) -> str:
    """Menu-bar status text for *mode* (``Idle`` / ``Listening`` / ``Busy``)."""
    return _STATUS_LABELS.get(mode, "Idle")


def status_symbol(mode: HudMode) -> str:
    """SF Symbol name for *mode*'s menu-bar glyph (distinct per state)."""
    return _STATUS_SYMBOLS.get(mode, "circle")


@runtime_checkable
class Notifier(Protocol):
    """Anything that can surface a :class:`NotificationEvent` to the user.

    ``ConsoleNotifier`` already satisfies this. The seam lets a GUI overlay be
    added as a second notifier (ADR-0003's fan-out) without the controller
    knowing which concrete notifiers are present.
    """

    def notify(self, event: NotificationEvent, **kwargs: Any) -> None: ...


class ConsoleNotifier:
    """Writes one-line status messages to *stream* (default: stderr).

    Lines contain only state and metadata (duration, char count).  Transcript
    text must never be passed through to the output — any ``transcript=`` kwarg
    in :meth:`notify` is silently discarded.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        enabled: bool = True,
    ) -> None:
        self._stream: TextIO = stream if stream is not None else sys.stderr
        self._enabled = enabled

    def notify(self, event: NotificationEvent, **kwargs: Any) -> None:
        """Emit a single status line for *event*.

        Accepted metadata kwargs:
        - ``duration_seconds`` (float) — shown for TRANSCRIBING.
        - ``char_count`` (int) — shown for SUCCESS.

        Any other kwargs (including ``transcript``) are ignored.
        """
        if not self._enabled:
            return
        line = self._format(event, kwargs)
        print(line, file=self._stream)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _format(self, event: NotificationEvent, meta: dict[str, Any]) -> str:
        if event is NotificationEvent.READY:
            return "[ready]"
        if event is NotificationEvent.RECORDING:
            return "[recording]"
        if event is NotificationEvent.TRANSCRIBING:
            duration = meta.get("duration_seconds")
            if duration is not None:
                return f"[transcribing] {duration:.1f}s audio"
            return "[transcribing]"
        if event is NotificationEvent.CANCELLED:
            return "[cancelled]"
        if event is NotificationEvent.BUSY:
            return "[busy]"
        if event is NotificationEvent.ERROR:
            return "[error]"
        if event is NotificationEvent.SUCCESS:
            char_count = meta.get("char_count")
            if char_count is not None:
                return f"[done] {char_count} characters"
            return "[done]"
        return f"[{event.value.lower()}]"


class FanOutNotifier:
    """Forwards each event to several notifiers (ADR-0003).

    The controller holds a single :class:`Notifier`; this lets a GUI overlay be
    added alongside the console notifier without the controller knowing. Each
    child notifier's exceptions are **swallowed** so one failing notifier never
    breaks another — and never breaks recording.
    """

    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        self._notifiers: list[Notifier] = list(notifiers)

    def add(self, notifier: Notifier) -> None:
        """Append a notifier after construction.

        Lets the GUI host register its :class:`OverlayNotifier` once the panel
        is built on the main thread (ADR-0001/#20), without the controller
        rebuilding its notifier.
        """
        self._notifiers.append(notifier)

    def notify(self, event: NotificationEvent, **kwargs: Any) -> None:
        for notifier in self._notifiers:
            try:
                notifier.notify(event, **kwargs)
            except Exception:  # noqa: BLE001
                # A broken notifier must not affect the others or dictation.
                logger.warning("notifier %r failed for %s", notifier, event, exc_info=True)


class OverlayNotifier:
    """Drives the persistent overlay's show + mode off the notification stream.

    The persistent-companion contract (ADR-0007 §2): the HUD is shown **once**, on
    ``READY``, in ``IDLE``, and is thereafter driven ``IDLE → LISTENING → BUSY →
    IDLE`` by the event stream. **No event hides it** — it leaves the screen only
    via ``Overlay.teardown`` on app close (ADR-0007 §3, a structural guarantee):

    - ``READY`` → show + ``set_mode(IDLE)`` (was ignored)
    - ``RECORDING`` → show + ``set_mode(LISTENING)``
    - ``BUSY`` → show + ``set_mode(BUSY)`` (fired on key-release, before
      ``recorder.stop()``, so the HUD flips to busy the instant recording ends and
      stays busy through PROCESSING_AUDIO → TRANSCRIBING → CLEANING → PASTING)
    - ``SUCCESS`` / ``CANCELLED`` / ``ERROR`` → ``set_mode(IDLE)``, **stays shown**
      (was hide; a distinct ERROR beat is deferred — ADR-0007 §2)
    - ``TRANSCRIBING`` → ignored

    ``_visible`` is a one-shot "have we shown yet?" latch (ADR-0007 §4): the first
    event to arrive shows the panel; every event after is a pure flicker-free
    ``set_mode`` on the same continuously-shown window. Because every event carries
    ``show=True``, a missed ``READY`` show self-heals on the next ``RECORDING`` /
    ``BUSY`` / terminal.

    Because ``notify`` runs on the hotkey-listener / worker threads but AppKit must
    run on the main thread (ADR-0001), the show **and** set_mode calls are
    **marshalled onto the main thread** via an injected ``dispatch_main`` callable,
    batched into a single turn. The ``hide`` callable is retained on the contract
    (teardown and hand-built overlays reference it) but is never called from the
    event stream. A broken overlay is swallowed (fail-open — no HUD, never a
    blocked dictation).
    """

    # Terminal events settle the HUD back to IDLE (ADR-0007 §2) — they no longer
    # hide it. READY is handled separately (it is the first-show trigger).
    _IDLE_EVENTS = frozenset(
        {NotificationEvent.CANCELLED, NotificationEvent.SUCCESS, NotificationEvent.ERROR}
    )

    def __init__(
        self,
        *,
        show: Callable[[], None],
        hide: Callable[[], None],
        set_mode: Callable[[HudMode], None],
        dispatch_main: Callable[[Callable[[], None]], None],
    ) -> None:
        self._show = show
        self._hide = hide  # retained on the contract (ADR-0007 §3); never called here
        self._set_mode = set_mode
        self._dispatch_main = dispatch_main
        self._visible = False  # one-shot latch: have we shown the panel yet?

    def notify(self, event: NotificationEvent, **kwargs: Any) -> None:
        if event is NotificationEvent.READY:
            self._request(HudMode.IDLE)
        elif event is NotificationEvent.RECORDING:
            self._request(HudMode.LISTENING)
        elif event is NotificationEvent.BUSY:
            self._request(HudMode.BUSY)
        elif event in self._IDLE_EVENTS:
            self._request(HudMode.IDLE)
        # TRANSCRIBING is neither a show nor a mode event — ignore.

    def _request(self, mode: HudMode) -> None:
        # Persistent contract: every event shows-if-not-yet-shown (one-shot latch)
        # then sets the mode. Nothing hides. The show + set_mode are batched into a
        # single main-thread turn so a mode switch happens on the same window with
        # no flicker; the latch makes the show a no-op after the first, and a missed
        # first show self-heals on the next event (ADR-0007 §4).
        def _run() -> None:
            try:
                if not self._visible:
                    self._visible = True
                    self._show()
                self._set_mode(mode)
            except Exception:  # noqa: BLE001
                # Fail-open: a broken overlay must never harm dictation.
                logger.warning("overlay show/set_mode failed", exc_info=True)

        try:
            self._dispatch_main(_run)
        except Exception:  # noqa: BLE001
            logger.warning("overlay dispatch_main failed", exc_info=True)
