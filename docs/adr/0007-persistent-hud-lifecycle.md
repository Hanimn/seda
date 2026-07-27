# ADR-0007 — The overlay becomes a persistent companion, removed only on close

- **Status:** Accepted
- **Date:** 2026-07-27
- **Deciders:** wayfinder grilling session (Hani Momeninia + agent)
- **Map:** [#50 — HUD map: persistent companion (idle→listening→busy), removed only on close (macOS)](https://github.com/Hanimn/seda/issues/50)
- **Ticket:** [#55 — Decision: persistent-HUD lifecycle contract](https://github.com/Hanimn/seda/issues/55)
- **Supersedes (in part):** [ADR-0006](0006-overlay-busy-mode-on-release.md) — specifically its event→(show/hide/mode) mapping table (§2) and its two-member `HudMode` enum (§1). The rest of ADR-0006 (BUSY-fires-on-release timing and rationale, `set_mode` marshalled onto main per ADR-0001, the batched single-`dispatch_main` turn, fail-open discipline, the reject-a-new-`PROCESSING`-event and reject-per-stage-visuals decisions, and the orthogonal wave curve/jitter) **stands unchanged**.
- **Depends on:** [ADR-0001](0001-gui-host-owns-main-thread.md), [ADR-0003](0003-notifier-seam-overlay-lifecycle.md), [ADR-0006](0006-overlay-busy-mode-on-release.md)

## Context

ADR-0003 (#15/#20) and ADR-0006 (#44) both modelled the overlay as an **ephemeral** HUD:
it showed on `RECORDING` (or `BUSY`), and **hid** on every terminal event
(`{CANCELLED, SUCCESS, ERROR} → hide`). Between dictations there was no HUD at all — the panel
left the screen and came back on the next press.

The [persistent-companion map (#50)](https://github.com/Hanimn/seda/issues/50) re-scopes this at
the user's request: the HUD should be an **always-present companion** while Seda runs — a
compressed **idle** look when at rest, the **listening** wave while talking, the **busy** pulse
while processing — and it should be **removed only when the app closes**. No more hiding after
each dictation.

That inverts ADR-0006's terminal-event contract (terminals must now settle the HUD *to rest*,
not remove it) and adds a third visual mode (`IDLE`). ADR-0006 froze that contract, so this ADR
amends it. This ADR settles **only the event→mode wiring** — the *lifecycle contract*. Two
things it deliberately does **not** decide: what `IDLE` looks like (that is
[#56](https://github.com/Hanimn/seda/issues/56)'s idle-visual prototype), and how removal is
guaranteed across crash/kill paths (that is the [#51](https://github.com/Hanimn/seda/issues/51)/
[#53](https://github.com/Hanimn/seda/issues/53)/[#54](https://github.com/Hanimn/seda/issues/54)
diagnosis-and-cleanup half of the map).

## Decision

The overlay is a **persistent companion**: shown once at startup, driven through
`IDLE → LISTENING → BUSY → IDLE` by the existing notification stream, and removed **only** by
`Overlay.teardown` on app close. No `NotificationEvent` maps to hide anymore.

### 1. `HudMode` gains a third member, `IDLE`

`HudMode(StrEnum)` becomes `IDLE` (compressed at-rest look) / `LISTENING` (live mic-level EQ
bars) / `BUSY` (time-driven working pulse). `IDLE` is a real *sustained* state — the resting
mode the HUD holds whenever a dictation cycle is not in flight — not a transient. Its **visual
design** (a dot? a short pill?) is out of scope here and is prototyped by #56; this ADR only
establishes that the state exists and how it is entered and left.

> The `HudMode.IDLE` enum member is **not added to `src/` by this ADR** — #55 is a decision
> ticket ("plan, don't do"). It lands with the implement pass (or #56's prototype defines its
> own local constant in `scratch/`). The contract below is written against the member as it will
> exist.

### 2. The lifecycle contract — event → (show / mode)

`OverlayNotifier.notify` maps:

| Event | Contract | Change vs ADR-0006 |
|---|---|---|
| `READY` | show + `set_mode(IDLE)` | **new** — was ignored |
| `RECORDING` | show (idempotent) + `set_mode(LISTENING)` | unchanged |
| `BUSY` | show (idempotent) + `set_mode(BUSY)` | unchanged |
| `TRANSCRIBING` | ignored | unchanged |
| `SUCCESS` | `set_mode(IDLE)` (stays shown) | **was hide** |
| `CANCELLED` | `set_mode(IDLE)` (stays shown) | **was hide** |
| `ERROR` | `set_mode(IDLE)` (stays shown) | **was hide** |
| *app close* | `Overlay.teardown` → `orderOut_`/`close` | the **only** removal path |

Rationale, decision-by-decision:

- **Show on `READY`, in `IDLE`.** `READY` already fires exactly once, at the end of
  `AppController.start()` (`app.py:165`), the moment hotkeys go live and the app is *actually
  usable*. Showing there keeps the contract event-driven (the overlay reacts to the stream,
  never gets shown imperatively from the lifecycle, never polls — consistent with ADR-0003) and
  avoids a "HUD up but hotkeys still dead" window. The model-load gap *before* `READY` (no HUD
  during Whisper/MLX warmup) is a known, accepted cost; a load indicator, if wanted, is a
  separate ticket, not a muddying of this contract.

- **Terminals settle to `IDLE`, identically.** `SUCCESS`, `CANCELLED`, and `ERROR` all map to
  `set_mode(IDLE)` and keep the panel shown. Cancellation is a normal outcome, not punished; the
  paste landing is itself the success feedback and the HUD settling to rest is enough of a
  "done" signal. There is **no success flourish and no dismiss look** — the map's destination is
  three modes (`idle→listening→busy→idle`), and a fourth "done" beat plus its self-clearing
  timer would drag the stateless `OverlayNotifier` into the per-stage visual redesign #50
  fences out of scope.

- **`ERROR` — distinct error beat deliberately deferred.** Treating `ERROR` identically to
  `SUCCESS` is a **known feedback regression**: under the old hide-on-error contract the
  vanishing HUD was a crude "something's wrong" signal, and a silently-settling persistent HUD
  gives no HUD-level indication of failure (the `ConsoleNotifier` still prints `[error]`). We
  accept this **for now** rather than wedge a red-flash-then-idle visual — that needs a fourth
  visual state and a self-clearing timer (the very thing rejected for `SUCCESS`), and getting its
  look and timing right is its own by-eye prototype, not a wiring-contract ADR. A distinct
  error beat is a **deliberately-deferred follow-up**, recorded here so it is not forgotten.

- **`TRANSCRIBING` stays ignored.** It is only reached via `_on_release` → `_process_audio`,
  which fires `BUSY` *before* submitting the worker (`app.py:254`), so `BUSY` always precedes
  `TRANSCRIBING` and has already set busy mode. Mapping `TRANSCRIBING` would be a redundant
  no-op that reads as meaningful. (Same rationale ADR-0006 gave.)

### 3. No event hides — "only teardown removes the panel" is structural

With every former hide-event now settling to `IDLE`, **nothing in the event stream calls
hide**. The `Overlay` handle keeps its `hide` callable (teardown itself uses `panel.orderOut_` /
`close` directly, not via `OverlayNotifier`, so removal is unaffected), but no
`NotificationEvent` maps to it. This turns "the HUD is removed only on app close" from a
convention someone could accidentally break by adding a hide-mapping into a **structural
guarantee** of the notifier.

### 4. `_visible` collapses to a one-shot latch; `show` on mode events self-heals

Because the HUD is shown once and never hidden until teardown, `OverlayNotifier._visible`
becomes a one-shot "have we shown yet?" latch rather than a per-cycle flag. `RECORDING` and
`BUSY` **retain their `show=True`** (unchanged mappings): the latch makes the show a no-op after
the first `READY` show, but if that single `READY` show is ever missed (a startup dispatch race,
a transient AppKit hiccup), the next `RECORDING`/`BUSY` re-asserts it and the HUD self-heals.
For a fail-open component, a redundant idempotent flag is worth the robustness. Every dictation
after the first is therefore **pure flicker-free mode flips on the same continuously-shown
panel** — `IDLE → LISTENING → BUSY → IDLE`, no `orderOut_`/`orderFront` ever — extending
ADR-0006's flicker-free `LISTENING → BUSY` guarantee across the whole cycle.

**Rejected — `READY`-only show (mode events pure `set_mode`).** Cleaner in theory, but a missed
`READY` show would leave the HUD invisible for the entire session with no recovery. The
idempotent self-heal costs nothing and removes that failure mode.

**Rejected — re-assert front/topmost each cycle** (defend against window burial under a newly
launched fullscreen app). Z-order/burial robustness is real but belongs to the
persistence-and-removal half of the map (#51/#54 on-hardware findings + the implement pass), not
the event→mode contract. Layering it in here would blur the boundary and pre-empt findings that
have not landed. If burial proves real on hardware, re-asserting topmost is a targeted addition
then.

## Consequences

**Positive**

- The HUD is now a persistent companion: present the entire session, resting in `IDLE`, never
  flashing in and out between dictations.
- "Removed only on app close" is structurally guaranteed — no event path can hide the panel.
- Reuses ADR-0003's seam and ADR-0006's `set_mode` machinery wholesale: no new event, no
  state-machine listener, no polling. The only new vocabulary is one `HudMode` member (`IDLE`).
- Minimal diff: `RECORDING`/`BUSY`/`TRANSCRIBING` mappings are untouched; the change is the new
  `READY` mapping, retargeting the three terminal events from hide to `set_mode(IDLE)`, and the
  `_visible` latch semantics.

**Negative / costs accepted**

- **Error feedback regression** (§2): a persistent HUD gives no distinct visual for `ERROR`
  until the deferred error-beat follow-up ships. Console/log feedback remains.
- **No HUD during model load** (§2): the HUD appears at `READY`, after warmup, not at process
  start.
- **Removal now matters everywhere.** Because the HUD is up the *entire* session, guaranteeing
  it is gone on every exit path is the central reliability concern — but that is exactly the
  scope of #51/#53/#54 and is out of scope for this contract ADR.
- ADR-0006's mapping table and its two-member `HudMode` are superseded; ADR-0006 carries a
  pointer here.

## Test obligations (supersede the terminal→hide parts of ADR-0006's obligations)

To be discharged by the implement pass, not this ticket:

- `OverlayNotifier`: `READY → show + IDLE`; `RECORDING → show + LISTENING`; `BUSY → show + BUSY`;
  `{SUCCESS, CANCELLED, ERROR} → set_mode(IDLE)` with **no hide**; `TRANSCRIBING` ignored.
- A full cycle after the first show performs **no** hide/show — only `set_mode` — on the same
  panel (flicker-free); `show` on `RECORDING`/`BUSY` is a no-op once `_visible` is latched but
  self-heals if `READY`'s show was missed.
- No `NotificationEvent` maps to `hide`; the panel leaves the screen only via `Overlay.teardown`.
- The `IDLE` visual and mode transitions are **verified by eye** running Seda locally
  (consistent with ADR-0005's "no AppKit in unit tests" boundary and #56's prototype).

## Out of scope / deferred (recorded so they are not lost)

- **The `IDLE` visual design** — dot vs pill vs compressed bars — is [#56](https://github.com/Hanimn/seda/issues/56).
- **A distinct `ERROR` beat** (flash-then-idle) — deliberately deferred follow-up (§2).
- **Reliable removal across crash/SIGKILL** — [#51](https://github.com/Hanimn/seda/issues/51)/[#53](https://github.com/Hanimn/seda/issues/53)/[#54](https://github.com/Hanimn/seda/issues/54).
- **Z-order/burial defense** — deferred to on-hardware findings + implement (§4).
- **A model-load / warmup indicator** before `READY` (§2).
