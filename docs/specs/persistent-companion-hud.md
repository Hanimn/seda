# Spec: persistent-companion HUD (idle→listening→busy, removed only on close)

**Status:** ready to implement · **Map:** [HUD map: persistent companion (macOS)](https://github.com/Hanimn/seda/issues/50)
**Verification:** manual, by eye + exit-path runs, on macOS. Both prototypes below already validated the behaviour on real hardware.

The macOS recording HUD becomes a **persistent companion**: always on screen while Seda runs — a **compressed idle** look at rest, the **listening wave** while talking, the **busy pulse** while processing — shown once on startup and **removed only when the app closes**. No more hiding after each dictation.

It consolidates the map's resolved decisions:
- [Research #52](https://github.com/Hanimn/seda/issues/52) — how macOS reclaims/orphans an overlay on exit.
- [Diagnosis #51](https://github.com/Hanimn/seda/issues/51) — the leak is a *surviving suspended process*, not a cleanup bug.
- [Lifecycle contract #55 → ADR-0007](../adr/0007-persistent-hud-lifecycle.md) — the event→mode wiring.
- [Cleanup scope #53](https://github.com/Hanimn/seda/issues/53) — best-effort; accept the suspended survivor.
- [Idle look #56](https://github.com/Hanimn/seda/issues/56) — compressed pill + panel-shrink (by-eye).
- [Cleanup proof #54](https://github.com/Hanimn/seda/issues/54) — every targeted exit path verified on macOS.

This spec fixes the *form*; by-eye tuning constants (pill dimensions, shimmer amplitude, shrink geometry) are settled during implementation. It **builds on** the shipped busy-visual + wave work (ADR-0006) and **implements ADR-0007's contract** — it does not revisit the listening/busy visuals.

## Files touched

| File | Change |
|---|---|
| `src/seda/notifications/__init__.py` | `HudMode` gains `IDLE`. `OverlayNotifier`: map `READY`→show+`set_mode(IDLE)`; `{SUCCESS, CANCELLED, ERROR}`→`set_mode(IDLE)` (no longer hide); `_visible` becomes a one-shot "shown yet?" latch; **no event maps to hide**. |
| `src/seda/gui/host.py` | `WaveformView`: new `IDLE` draw branch (compressed pill + faint shimmer). `Overlay`: panel-resize between idle (48×24) and active (160×48), recentred on a fixed point; `set_mode(IDLE)` triggers the shrink, `LISTENING`/`BUSY` the grow. `build_overlay`: show the panel in `IDLE` at startup. |
| `src/seda/app.py` | No new events. `READY` already fires at the end of `start()` (`app.py:165`) — it now drives the initial show. |
| `src/seda/cli.py` | No change beyond what ADR-0006 already wired (`set_mode` is already injected). |

No recorder changes; no new `NotificationEvent` member. Consistent with ADR-0001 (GUI host owns main + teardown), ADR-0003 (notifier seam), ADR-0006 (busy mode), and implements ADR-0007.

---

## Part 1 — The lifecycle contract (ADR-0007)

The overlay is shown once on `READY` in `IDLE` and driven `IDLE → LISTENING → BUSY → IDLE` by the existing notification stream. **No `NotificationEvent` maps to hide** — the panel leaves the screen only via `Overlay.teardown` on app close.

### 1a. Event → (show / mode) map

| Event | Overlay action | vs today (ADR-0006) |
|---|---|---|
| `READY` | show + `set_mode(IDLE)` | **new** — was ignored |
| `RECORDING` | show (idempotent) + `set_mode(LISTENING)` | unchanged |
| `BUSY` | show (idempotent) + `set_mode(BUSY)` | unchanged |
| `TRANSCRIBING` | ignored | unchanged (`BUSY` on release already covers it) |
| `SUCCESS` | `set_mode(IDLE)`, stays shown | **was hide** |
| `CANCELLED` | `set_mode(IDLE)`, stays shown | **was hide** |
| `ERROR` | `set_mode(IDLE)`, stays shown | **was hide** (distinct error beat deferred) |
| *app close* | `Overlay.teardown` → `orderOut_`/`close` | the **only** removal path |

### 1b. `_visible` becomes a one-shot latch

Because the HUD shows once and never hides until teardown, `OverlayNotifier._visible` is a "shown yet?" latch, not a per-cycle flag. `RECORDING`/`BUSY` keep `show=True` (idempotent) so a missed `READY` show self-heals on the next mode event. Every dictation after the first is a **pure flicker-free mode flip** on the same shown panel.

### Acceptance (Part 1, by eye)
1. On startup (once `[ready]`), the HUD appears bottom-center in the compressed idle look and stays.
2. Talking → listening wave; releasing → busy pulse; text lands → settles back to idle. Never hides between dictations.
3. Cancel or error settles to idle (no distinct error visual yet — deferred), never hides.
4. Mode flips are flicker-free — the panel never blinks out and back.

---

## Part 2 — The idle-compressed look (#56)

`HudMode.IDLE` draws a **compressed pill**: a short horizontal capsule at panel center with a faint, slow alpha shimmer (a low-amplitude breath, floor well above zero so it reads "alive at rest", not pulsing like busy). It keeps the bar cluster's horizontal silhouette compressed to one element, so waking reads as the same widget widening back into the 9 bars.

### 2a. Panel-shrink

The `NSPanel` itself resizes: **48×24 in idle**, **160×48 in listening/busy**, recentred on a fixed point (the active panel's center stays put as it grows/shrinks). `set_mode` drives the resize on the main thread alongside the redraw. The translucent rounded chip background tracks the panel size (corner radius clamps to half-height so the shrunk chip stays rounded).

*Rejected in the prototype:* the breathing-dot variant (less silhouette continuity), and a same-size 160×48 panel with visual-only compression (the real shrink felt calmer/more intentional by eye).

### Acceptance (Part 2, by eye)
1. Idle reads as clearly at-rest but present — "asleep, alive, waiting" — small and unobtrusive enough to sit on screen the whole session.
2. Waking (idle→listening) and settling (busy→idle) are smooth — the pill widens into / the bars collapse back to the pill, no flicker or pop.
3. Idle, listening, and busy read as one widget in three states (the panel-shrink and shared silhouette hold them together).

---

## Part 3 — Removal / cleanup (ADR-0007 lifecycle + #53 scope, proven by #54)

Because the HUD is up the entire session, guaranteeing it is gone on exit is the central concern. Scope (#53): **best-effort** — cover every exit path Python can intercept; accept a suspended survivor as unrecoverable.

### 3a. The mechanism (already in `run_with_overlay`, no new machinery)

The existing `try/finally` + signal-pump **is** the cleanup mechanism — #53 rejected adding `atexit`/`faulthandler`/a watchdog as redundant. On stop: signal-flag → pump services it → `controller.shutdown()` → `finally: Overlay.teardown()` (invalidate timer, `orderOut_`/`close`).

### 3b. Proven coverage (#54, verified on macOS)

| Exit path | teardown runs? | HUD cleared? | how |
|---|---|---|---|
| normal / SIGINT / SIGTERM / exception | ✅ | ✅ | `finally` / pump teardown |
| abort (SIGABRT) / SIGKILL | ❌ | ✅ | process dies → WindowServer reclaims (macOS) |
| suspended (SIGSTOP / Ctrl-Z) | ❌ | **lingers → `kill -9` clears** | accepted limitation — a stopped process runs no code |

A *dead* process's surface is always reclaimed by macOS; a leak requires a *surviving suspended* process (#51). The in-app teardown is the clean path over that OS backstop.

### Acceptance (Part 3)
1. Every non-suspended exit path leaves no HUD on screen (teardown or OS-reclaim). Verified on macOS (#54).
2. A suspended Seda process holding the HUD is documented as recoverable with `kill -9 <pid>` — not a bug to fix in-process.

---

## Out of scope

- **A distinct `ERROR` visual** (flash-then-idle) — a deliberately-deferred follow-up (ADR-0007 §2); `ERROR` settles to `IDLE` like success for now.
- **An out-of-process watchdog** for suspended survivors — rejected by #53.
- **Model-load / warmup indicator** before `READY` — separate concern.
- **Re-tuning the listening wave or busy pulse** — settled in map #44 / ADR-0006; this spec only adds the idle mode + persistent lifecycle.
- **The Windows overlay** — tracked separately by map #39.
- **Z-order / burial defense** (re-assert topmost per cycle) — deferred to implement if it surfaces on hardware.
