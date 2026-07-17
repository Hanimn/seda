# ADR-0003 — A `Notifier` Protocol + fan-out drives the overlay show/hide

- **Status:** Accepted
- **Date:** 2026-07-17
- **Deciders:** grilling session (Hani Momeninia + agent)
- **Issue:** [#20 — Notifier seam for overlay show/hide lifecycle](https://github.com/Hanimn/seda/issues/20)
- **Epic:** [#15 — live recording waveform overlay (macOS)](https://github.com/Hanimn/seda/issues/15)
- **Depends on:** [ADR-0001](0001-gui-host-owns-main-thread.md) (#16 — GUI host owns main thread), [ADR-0002](0002-recorder-latest-level-handoff.md) (#19 — `latest_level`)
- **Informed by:** [#17 research](https://github.com/Hanimn/seda/issues/17)

## Context

The overlay must appear while recording and vanish when the utterance ends. Two facts about
the current code shape the decision:

1. **There is no notifier Protocol and no injection.** `AppController.__init__` hard-codes
   `self._notifier = ConsoleNotifier(...)` (`app.py:75`). `ConsoleNotifier` is a concrete
   class with a `notify(event, **kwargs)` method (`notifications/__init__.py:43`). The state
   machine (`state.py`) has **no listener seam** — just a lock-guarded `transition()`.
2. **The events the overlay needs already fire at the right places.** `RECORDING` fires in
   `_on_press` right after `recorder.start()` (`app.py:202`); `CANCELLED`/`SUCCESS`/`ERROR`
   fire on every terminal path (`_on_cancel`, `_process_audio`). So **show = RECORDING**,
   **hide = {CANCELLED, SUCCESS, ERROR}** maps directly onto existing `notify()` calls — no
   new event taxonomy.

The sharp constraint comes from [ADR-0001](0001-gui-host-owns-main-thread.md): all overlay
AppKit calls must run on the **main thread**. But every `notify()` call runs on either the
**pynput listener thread** (`_on_press`, `_on_cancel`) or the **worker thread**
(`_process_audio`) — **never main**. So whatever consumes the events must marshal the actual
show/hide onto the main thread.

## Decision

Introduce a **`Notifier` Protocol** and a **`FanOutNotifier`**; the overlay is a second
`Notifier` (`OverlayNotifier`) whose `notify()` **marshals idempotent show/hide onto the main
thread**. The overlay is *event-driven* off the existing notification stream, not a state
observer/poller.

### 1. `Notifier` Protocol + `FanOutNotifier` (in the `notifications` package)

- Define a `Notifier` Protocol formalizing `notify(event: NotificationEvent, **kwargs) -> None`
  — the signature `ConsoleNotifier` **already satisfies**, so it becomes a `Notifier` with no
  code change.
- Add a `FanOutNotifier(Notifier)` that holds a list of notifiers and forwards `notify()` to
  each, **swallowing per-notifier exceptions** so one failing notifier never breaks another —
  and never breaks recording. It lives in the `notifications` package (not scattered across
  `app.py` call sites), so the swallow-errors discipline is in one place.
- `AppController` builds a `FanOutNotifier([ConsoleNotifier(...)])` by default; on macOS with
  the overlay enabled, the `OverlayNotifier` is appended. Console stays the always-present
  default.

**Rejected — observe recorder/state directly.** `state.py` has no listener seam, so this
means polling `state_machine.state` from the overlay's timer. It was tempting because that
timer (ADR-0002, reading `latest_level`) already runs *on main* — zero marshalling. But it
couples the overlay to state-machine internals, turns a clean event into a poll, and duplicates
lifecycle logic the notification stream already expresses. The Protocol+fan-out reuses the
existing, tested event stream and is injectable (helps #22). We accept the marshalling cost.

### 2. `OverlayNotifier.notify()` — map, marshal, ignore the rest

```python
class OverlayNotifier:            # satisfies the Notifier Protocol
    def notify(self, event, **kwargs):
        if event is NotificationEvent.RECORDING:
            self._dispatch_main(self._overlay.show)
        elif event in (NotificationEvent.CANCELLED,
                       NotificationEvent.SUCCESS,
                       NotificationEvent.ERROR):
            self._dispatch_main(self._overlay.hide)
        # READY / BUSY / TRANSCRIBING → ignored by the overlay
```

- `_dispatch_main(...)` marshals onto the main thread via
  `performSelectorOnMainThread_withObject_waitUntilDone_(..., False)` / GCD main-queue (per
  #17). The actual `orderFrontRegardless()` / `orderOut_` runs on main.
- `READY`, `BUSY`, `TRANSCRIBING` are **not** show/hide events — the overlay ignores them.
  (Per #15 this is a RECORDING-only HUD: no processing/spinner state.)

### 3. Idempotency + race safety

`notify()` fires from two threads and events can burst (e.g. a fast press→release→cancel
interleaves `RECORDING` on the listener thread with `CANCELLED` on the worker thread). The
overlay's show/hide must therefore be **idempotent and ordering-tolerant**:

- `show()` when already shown is a **no-op**; `hide()` when already hidden is a **no-op**.
- All show/hide ops are marshalled to main and run **serially in dispatch order** — the main
  run loop is the single serialization point, so a raced `hide` after a `show` leaves the
  overlay correctly hidden (and vice versa) rather than stuck visible.
- Because hide covers *all three* terminal events, a path that fires both `ERROR` and
  `CANCELLED` simply hides twice — the second is a no-op.

### Exception discipline

- `FanOutNotifier` swallows each notifier's exceptions (a broken notifier can't harm the
  others or recording).
- `OverlayNotifier.notify()` additionally must not raise into the fan-out: if dispatch or the
  overlay call fails, it is swallowed/logged (fail-open, per #15 — a broken overlay degrades
  to no HUD, never blocks dictation).

## Consequences

**Positive**

- Reuses the existing event stream and `notify()` call sites — no new events, no state-machine
  listener seam, no polling.
- `ConsoleNotifier` becomes a `Notifier` for free (already matches the signature).
- Injectable fan-out ⇒ #22 can assert "RECORDING → show called; SUCCESS → hide called" with a
  fake overlay, no AppKit.
- Fail-open at two layers (fan-out swallow + overlay swallow); the console path is untouched.

**Negative / costs accepted**

- Every overlay show/hide is marshalled cross-thread onto main (accepted — it's a handful of
  events per utterance, not per block; the per-block path is the pull in ADR-0002).
- `AppController` no longer hard-codes its notifier — it builds a `FanOutNotifier`. Small
  constructor change; the `_notifier.notify(...)` call sites are unchanged (fan-out has the
  same `notify` shape).

**Follow-ups (later tickets / implementation)**

- #21 (config / `--no-overlay` / gating) decides *when* the `OverlayNotifier` is appended to
  the fan-out (macOS + `[overlay] enabled` + not `--no-overlay`), and the fail-open construction
  (a failed overlay build ⇒ fan-out with console only).
- #22 (test strategy) must cover: fan-out forwards to all and swallows a raising notifier;
  RECORDING→show / terminal→hide mapping; idempotent double-show/double-hide; and the
  listener/worker → main marshalling (with a fake dispatcher).
- The `OverlayNotifier` holds the overlay object built by the #16 GUI host; wiring the host,
  the notifier, and ADR-0002's `latest_level` timer together is implementation.

## Open questions (for implementation)

- Whether `_dispatch_main` uses `performSelectorOnMainThread_` on an ObjC shim vs a
  `dispatch_get_main_queue()` block — a #17 open-question detail to settle in a `/prototype`.
  `[uncertain]`
- Whether the overlay object exposes `show()`/`hide()` directly or the notifier talks to the
  #16 host which owns the panel (leaning: host owns the panel, notifier calls host). `[uncertain]`
