# ADR-0006 — The overlay gains a `busy` mode driven by `BUSY` on release

- **Status:** Accepted
- **Date:** 2026-07-24
- **Deciders:** wayfinder + implement session (Hani Momeninia + agent)
- **Spec:** [`docs/specs/hud-responsive-wave-and-busy-visual.md`](../specs/hud-responsive-wave-and-busy-visual.md)
- **Map:** [#44 — HUD map: responsive wave + post-release busy visual](https://github.com/Hanimn/seda/issues/44)
- **Supersedes (in part):** [ADR-0003](0003-notifier-seam-overlay-lifecycle.md) — specifically its "RECORDING-only HUD; the overlay ignores `BUSY`" decision (§2). The rest of ADR-0003 (the `Notifier` Protocol, `FanOutNotifier`, main-thread marshalling, idempotency, fail-open) stands unchanged.
- **Depends on:** [ADR-0001](0001-gui-host-owns-main-thread.md), [ADR-0002](0002-recorder-latest-level-handoff.md), [ADR-0003](0003-notifier-seam-overlay-lifecycle.md)

## Context

ADR-0003 (#15/#20) deliberately scoped the overlay as a **RECORDING-only HUD**: it showed on
`RECORDING`, hid on the terminal events, and **ignored `BUSY`/`TRANSCRIBING`** — "no
processing/spinner state." In practice that left a multi-second dead gap: the instant the user
releases the push-to-talk key, recording stops and the level meter flatlines, but transcription
+ cleanup + paste still take seconds. The flat-but-visible HUD read as "nothing is happening,"
and the wave itself was too insensitive to quiet speech.

The [busy-visual map (#44)](https://github.com/Hanimn/seda/issues/44) resolved this as a spec.
Two of its decisions change the overlay's event contract, which ADR-0003 froze — hence this ADR.

## Decision

The overlay is no longer RECORDING-only. It has **two visual modes** and consumes `BUSY` to
switch between them, still entirely off the existing notification stream (ADR-0003's seam is
reused, not replaced).

### 1. A `HudMode` vocabulary

`notifications` gains a `HudMode(StrEnum)` — `LISTENING` (live mic-level EQ bars) and `BUSY`
(a time-driven "working" pulse). It lives beside `NotificationEvent` because the
`OverlayNotifier` seam is where mode crosses threads; a small enum (not a bare string) mirrors
the existing `NotificationEvent` house style and lets mypy catch typos.

### 2. `BUSY` fires on release, and the overlay maps it to `busy` mode

- `AppController._on_release` fires `notify(NotificationEvent.BUSY)` **immediately after** the
  `PROCESSING_AUDIO` transition and **before** `recorder.stop()` — so the HUD flips to the busy
  visual at the source of the gap, with no lag waiting for silence-trim/worker-pickup. The
  existing `BUSY` event is reused; **no new event enum member**. The pre-existing `_on_press`
  press-while-busy nudge (which also fires `BUSY`) is harmless — it idempotently re-asserts the
  busy mode.
- `OverlayNotifier.notify` now maps: `RECORDING → show + set_mode(LISTENING)`,
  `BUSY → show + set_mode(BUSY)`, `{CANCELLED, SUCCESS, ERROR} → hide`. `READY`/`TRANSCRIBING`
  remain ignored. The HUD therefore stays shown across
  `PROCESSING_AUDIO → TRANSCRIBING → CLEANING → PASTING`, in busy mode the whole way, until a
  terminal event hides it.

### 3. `set_mode` marshalled onto main, like show/hide (ADR-0001 preserved)

The `Overlay` handle gains a `set_mode(HudMode)` callable alongside `show`/`hide`, wired in
`build_overlay`. `OverlayNotifier` batches show + set_mode into **one** `dispatch_main` call, so
the panel is shown and its mode set in a single main-thread turn — no cross-thread view access,
no hide/show flash between listening and busy (mode flips on the *same* shown panel). `set_mode`
is idempotent (re-setting the same mode is a cheap redraw) and fail-open (a raising overlay is
swallowed/logged, never blocks dictation) — the same discipline ADR-0003 established.

**Rejected — a new `PROCESSING` event.** Reusing `BUSY` (already fired on press-while-busy)
avoids growing the event taxonomy for a state that is, semantically, exactly "busy." A dedicated
event would have separated "entered processing" from "pressed while busy," but nothing in the
overlay contract needs that distinction.

**Rejected — per-stage visuals / polling state.** Distinct looks for transcribing vs cleaning
vs pasting, and a full HUD visual-language redesign, are explicitly out of scope (spec). Polling
`state_machine.state` from the redraw timer was already rejected by ADR-0003 for the same
coupling reasons; nothing here revisits that.

## Consequences

**Positive**

- The post-release gap now shows a distinct "working" pulse instead of a dead flat meter.
- Reuses ADR-0003's seam wholesale: no new event, no state-machine listener, no polling. The
  only new vocabulary is `HudMode`, confined to the overlay draw path.
- The wave's listening look also gains a perceptual (gated-sqrt) level curve and level-gated
  jitter (spec Part 1) — orthogonal to this ADR's event contract, but shipped together.

**Negative / costs accepted**

- `OverlayNotifier` is no longer strictly show/hide — it carries a mode. The `_request` helper
  gained a `mode` parameter and now batches two effects per dispatch. Idempotency is preserved
  and covered by tests.
- ADR-0003's "ignore `BUSY`" line and ADR-0005's matching test note are now superseded; both
  carry a pointer here.

## Test obligations (supersede the `BUSY`-ignored parts of ADR-0005)

- `OverlayNotifier`: `RECORDING → show + LISTENING`, `BUSY → show + BUSY`,
  `{CANCELLED, SUCCESS, ERROR} → hide`, `READY`/`TRANSCRIBING` ignored; a `LISTENING → BUSY`
  switch does **not** re-show or hide (same panel); show + set_mode go through **one**
  `dispatch_main`; set_mode failure is swallowed. (`tests/unit/test_notifications.py`.)
- `AppController._on_release` emits `BUSY` **before** `TRANSCRIBING`.
  (`tests/unit/test_controller.py`.)
- The busy pulse draw math and the wave curve/jitter feel are **verified by eye** running Seda
  locally (spec acceptance criteria) — not asserted in unit tests, consistent with ADR-0005's
  "no AppKit in unit tests" boundary.
