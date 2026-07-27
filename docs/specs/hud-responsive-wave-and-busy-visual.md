# Spec: HUD responsive wave + post-release busy visual

**Status:** ready to implement · **Map:** [HUD map: responsive wave + post-release busy visual](https://github.com/Hanimn/seda/issues/44)
**Verification:** manual, by eye, running Seda locally on macOS.

This spec fixes two HUD bugs, both in the macOS overlay:

1. **Wave sensitivity** — while recording, the EQ bars react instantly and proportionally to voice; quiet speech still visibly moves them (*twitchy & sensitive*).
2. **Post-release busy visual** — the instant keys release, the HUD switches to one distinct *busy* visual (a travelling pulse) that persists until the pasted text lands, so the multi-second `PROCESSING_AUDIO → TRANSCRIBING → CLEANING → PASTING` gap no longer reads as dead.

It consolidates three resolved decisions — [research #45](https://github.com/Hanimn/seda/issues/45), [wave spec #46](https://github.com/Hanimn/seda/issues/46), [state→visual contract #47](https://github.com/Hanimn/seda/issues/47), [busy visual #48](https://github.com/Hanimn/seda/issues/48). Where a decision has a tuning knob, the *form* is fixed here and the constant is settled by eye during implementation.

## Files touched

| File | Change |
|---|---|
| `src/seda/gui/host.py` | `WaveformView`: new gated-sqrt level curve + level-gated jitter (listening); new `_mode` field + `busy` draw branch (travelling pulse). `Overlay` handle + `build_overlay`: new `set_mode` callable marshalled onto main. |
| `src/seda/notifications/__init__.py` | `OverlayNotifier`: take `set_mode` in ctor; `RECORDING`→show+`set_mode("listening")`, `BUSY`→show+`set_mode("busy")`. |
| `src/seda/app.py` | `_on_release`: fire `notify(NotificationEvent.BUSY)` right after the `PROCESSING_AUDIO` transition, before `recorder.stop()`. |
| `src/seda/cli.py` | Wire `overlay.set_mode` into the `OverlayNotifier(...)` construction in `_register`. |

No recorder changes; no new notification-event enum member (reuses `BUSY`). Consistent with ADR-0001 (GUI host owns main thread), ADR-0002 (recorder level handoff), ADR-0003 (notifier seam).

---

## Part 1 — Wave sensitivity (listening mode)

All changes live in `WaveformView.drawRect_`, driven by the existing `latest_level` poll.

### 1a. Level curve — replaces `level = clamp(_level * 4.0, 0, 1)` (~L118)

Gated square-root; form locked, multiplier tuned by eye:

```
GATE = 0.006          # just below the ~0.008 ambient floor / 0.015 VAD threshold
GAIN = 2.6            # TUNE BY EYE in ~2.4–3.2; higher = hotter/more sensitive
level = clamp(sqrt(max(0.0, rms - GATE)) * GAIN, 0.0, 1.0)
```

- The **gate** subtracts the noise floor so silence maps to exactly `0.0` (not a shimmering low value).
- The **sqrt** expands the perceptually-dense low end so quiet-onset speech visibly moves bars.
- `GAIN` is the single tune-by-eye knob (try 2.4 / 2.6 / 3.2); no other constant needs tuning.
- Reference points at GAIN 2.6: RMS 0.015 → ~25% height, 0.05 → ~55%, fills near 0.15. (Background: [research #45](https://github.com/Hanimn/seda/issues/45), `research/wave-levels` branch → `docs/research/wave-rms-scaling.md`.)

### 1b. Response — raw / instant

Each redraw renders the raw current level straight through the curve. Bars snap up and drop with every syllable. **No EMA, no peak-hold, no decay.** The 60 Hz redraw vs 64 ms RMS refresh is already fast enough — the fix is the curve, not the cadence.

### 1c. Silence rests flat & still — level-gate the jitter

Today the per-bar `jitter` sine (`0.7 + 0.3*sin(...)`, ~L133) animates continuously regardless of level, and every bar has a 2px floor (`max(2.0, …)`, ~L134) — so silence shimmers. Fix:

- Scale the jitter's *amount* by `level` so its visible effect → 0 as `level` → 0 (the phase may keep advancing; the amplitude must not). Full liveliness only while speaking.
- At `level == 0`, bars collapse to a flat thin line and stop moving. Keep the ~2px minimum only as a **static** resting line, not an animated one.

### Acceptance (Part 1, by eye)

1. Quiet speech visibly moves the bars — softly speaking lifts them well off the resting line (not the ~6% stub they show today).
2. Normal speech reads as roughly half-to-most height; loud speech fills without sitting pinned flat at max for most of the utterance.
3. Silence rests flat and still — no shimmer, no idle jitter, bars motionless between utterances.
4. Twitchy attack — bars react within a syllable and drop promptly when you pause.

---

## Part 2 — Post-release busy visual

### 2a. Trigger — fire `BUSY` on release

In `AppController._on_release` (`src/seda/app.py`), add one notify right after the successful `transition(AppState.PROCESSING_AUDIO)` (~L246), **before** `recorder.stop()`:

```python
self._state_machine.transition(AppState.PROCESSING_AUDIO)
self._notifier.notify(NotificationEvent.BUSY)   # new: HUD → busy immediately on release
```

This closes the dead gap at its source — the HUD flips to busy the instant keys release, with no lag waiting for `stop()` / silence-trim / worker pickup. Reuses the existing `BUSY` event (no new enum member). Its current use (`_on_press` press-while-busy nudge, ~L242) stays and is harmless — it re-asserts busy idempotently. `TRANSCRIBING` still fires but needs no overlay behaviour of its own.

### 2b. Event → mode/visibility map (`OverlayNotifier`)

| Event | Overlay action |
|---|---|
| `RECORDING` | show (if hidden) + `set_mode("listening")` |
| `BUSY` | show (if hidden, defensive) + `set_mode("busy")` |
| `SUCCESS` / `CANCELLED` / `ERROR` | hide |
| `READY` / `TRANSCRIBING` | ignored |

The HUD stays shown across `PROCESSING_AUDIO → TRANSCRIBING → CLEANING → PASTING`, in busy mode the whole way, until `SUCCESS`/`CANCELLED`/`ERROR` hides it. Hide implicitly ends busy mode; on next show the mode is set explicitly by whichever of RECORDING/BUSY fires.

### 2c. Mechanism — `set_mode` on the overlay (mirrors show/hide)

- The `Overlay` handle gains a `set_mode` callable alongside `show`/`hide`, wired in `build_overlay`, marshalled onto the main thread via the same `dispatch_main` path (AppKit is main-thread only, ADR-0001).
- `WaveformView` stores `self._mode` (default `"listening"`); `set_mode` assigns it on the main thread and calls `setNeedsDisplay_`. `drawRect_` branches: `"listening"` → the Part-1 bars; `"busy"` → the travelling pulse below.
- `OverlayNotifier` takes `set_mode` in its constructor and calls it inside the same `_dispatch_main(_run)` wrapper used for show/hide — same fail-open swallow, same idempotency style. Wire `overlay.set_mode` in `cli.run`'s `_register` alongside show/hide.
- **Idempotency**: `set_mode` is cheap and idempotent (re-setting the same mode just redraws); the `_visible` guard on show/hide is unchanged.

### 2d. Busy visual — travelling pulse (`_mode == "busy"` branch of `drawRect_`)

```python
phase = self._frame / 60.0
speed = 3.2                              # bars/sec the head travels (tune 2.5–4.0)
head  = (phase * speed) % (_BARS + 3)    # +3 = brief gap between sweeps
for i in range(_BARS):
    d    = i - head
    bump = math.exp(-(d * d) / 2.2)      # gaussian around the head (sigma tune)
    h    = 0.18 + 0.62 * bump            # calm baseline + travelling swell
    half = max(2.0, bounds.size.height * 0.42 * h)
    alpha = 0.45 + 0.47 * bump           # band glows as it passes
    # draw bar i at height `half`, white @ `alpha`, SAME x layout as listening
```

- Same bar geometry (`_BARS`, width 6, gap 3, mirror about center) and rounded-chip background as listening — only the per-bar height/alpha source changes, so the morph reads as one widget changing state.
- `self._frame` already advances every redraw → the sweep animates on the existing 60 Hz timer, no new timer.
- **No level input** in busy mode — the pulse is purely time-driven (the mic is stopped by then).

**Tuning knobs (by eye, like GAIN):** `speed` (2.5–4.0); the `+3` sweep gap and `/2.2` sigma (spacing / band width); baseline `0.18` / swell `0.62` (how far bars collapse between passes). Prototype for reference: `proto/busy-visual` branch → `scratch/proto_busy_visual.py`.

### 2e. No flicker / no dead frames

- Busy mode is entered *before* `stop()`, so there is never a window of flat listening bars after release.
- Mode is set on the **same** shown panel — no order-out/order-in, no hide/show flash between listening and busy.
- The redraw timer keeps running across the transition (tied to visibility, not mode), so the pulse animates immediately.

### Acceptance (Part 2, by eye)

1. Releasing keys flips the HUD to the travelling-pulse busy visual **immediately** — no beat of flat bars first, unmistakably "busy" not "listening".
2. The busy visual persists continuously through the whole processing pipeline until the text pastes, then the HUD hides. Loops smoothly (no jump at the wrap).
3. Cancel (or an error) mid-processing hides the HUD without a stuck busy visual.
4. Pressing again while busy does not flicker or reset the busy visual.

---

## Out of scope

- **Per-stage processing visuals** (distinct looks for transcribing vs cleaning vs pasting) — busy is one mode from release to done.
- **Full HUD visual-language redesign** across all states — these two bugs are the scope.
- **Auto-gain / broadcast-style normalization** for the wave — the chosen feel is *twitchy & sensitive*, not normalized.
