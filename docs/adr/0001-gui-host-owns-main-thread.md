# ADR-0001 — A macOS GUI host owns the main thread; the controller is driven, not blocking

- **Status:** Accepted
- **Date:** 2026-07-17
- **Deciders:** grilling session (Hani Momeninia + agent)
- **Issue:** [#16 — Main-thread inversion: `NSApplication.run()` vs `AppController.run()`](https://github.com/Hanimn/seda/issues/16)
- **Epic:** [#15 — live recording waveform overlay (macOS)](https://github.com/Hanimn/seda/issues/15)
- **Feeds:** blocks #19, #20, #21, #22 (this is the load-bearing decision for the overlay map)
- **Informed by:** [#17 research](https://github.com/Hanimn/seda/issues/17) — `docs/research/nspanel-nonactivating-float-recipe.md`

## Context

The macOS waveform overlay (epic #15) is drawn with AppKit via PyObjC. The #17 research
established two hard AppKit facts (both `[sourced]` from Apple's Threading Programming Guide):

1. The main thread is the one that must be **blocked in `NSApplication.run()`** for an
   AppKit event loop to exist.
2. **All** `NSView`/`NSWindow`/`NSPanel` work — creating the panel, `setNeedsDisplay_`,
   drawing — must happen on that main thread.

Today the process is structured the opposite way. `cli.py:202` calls
`AppController(...).run()` **on the main thread**, and `run()` (`app.py:126`) ends by
**blocking on `self._shutdown_event.wait()`** (`app.py:155`). That `wait()` *is* the main
thread's whole life. `ARCHITECTURE.md` §"Threads and executors" records it:

> **Main thread** – blocks on a shutdown `threading.Event`; installs SIGINT/SIGTERM handlers.

So both `NSApplication.run()` and `AppController.run()` want to own-and-block the main
thread forever. Only one can. Something has to invert.

Non-negotiable constraint carried from the epic (#15): **fail-open is a hard invariant.**
A missing or broken AppKit (non-macOS, `ImportError`, `NSApp` init failure) must degrade to
**today's exact terminal behavior** — dictation must never be blocked or changed by the
overlay's absence.

## Decision

On the **overlay path (macOS, AppKit available)**, a new **GUI host owns the main thread**
and the AppKit run loop; the `AppController` is refactored so it no longer blocks — the host
*drives* it. On the **fallback path (no AppKit)**, nothing changes: the host delegates to the
**untouched** blocking `AppController.run()` on the main thread, exactly as today.

Three sub-decisions, each grilled:

### 1. Ownership shape — GUI host owns the lifecycle (Option B)

A `GUIHost` (macOS-only, name TBD in implementation) owns the main thread and calls
`NSApp.run()`. It constructs the `AppController` and drives it through a **split lifecycle**:

- `AppController.run()`'s setup half (`load()`, `transition(IDLE)`, `hotkeys.start(...)`,
  `notify(READY)`) becomes a **non-blocking `start()`**.
- The `self._shutdown_event.wait()` block **is not used on the overlay path** — the host
  blocks in `NSApp.run()` instead.
- `AppController.shutdown()` is unchanged (already thread-safe; runs the §22 teardown order)
  and now doubles as the host's "stop the controller" call.

**One owner, one teardown.** The host owns main, installs signals, starts the controller,
blocks in `NSApp.run()`, and on quit calls `controller.shutdown()`.

**Rejected alternative — Option A (controller on a background thread).** Keep `run()`
byte-for-byte and run it on a daemon thread while the main thread runs `NSApp.run()`. Its
appeal was maximum fail-open symmetry (the overlay path reuses today's exact `run()`). It was
rejected because it creates **two independent blocking loops** (`NSApp.run()` on main + the
`Event.wait()` on the daemon) that must be torn down in concert — the signal handler would
have to *both* set the Event *and* stop `NSApp`, and any ordering slip hangs one loop. That
dual-loop teardown is exactly the class of bug #22 (test strategy) would struggle to cover.
Option B's single-owner teardown is the cleaner threading model and the easier one to test.

### 2. Shutdown / signal handling — the GUI owner installs the handlers

With the controller's setup moved off the blocking path, it no longer owns signal
installation on the overlay path. (Python only permits `signal.signal` on the **main
thread** — the existing `in_main_thread` guard at `app.py:140` exists precisely because
registration raises `ValueError` off-main.)

On the overlay path, the **GUI host installs `SIGINT`/`SIGTERM` on the main thread** before
starting the AppKit loop. The handler:

1. calls `AppController.shutdown()` (thread-safe — sets state to `STOPPING`, stops hotkeys,
   cancels the recorder, drains the worker, closes the backend), then
2. stops the AppKit loop (`NSApp.stop_(None)` / terminate), so the main thread unblocks and
   the process exits cleanly.

On the fallback path, the untouched `AppController.run()` installs the handlers itself, on
the main thread, exactly as today — its `in_main_thread` guard is satisfied.

### 3. Fail-open seam — a `try/except` in `cli.run()`

The macOS-vs-fallback decision lives in `cli.run()` (`cli.py`). It **tries** to import and
build the GUI host; on **any** failure — non-`darwin`, `ImportError` on AppKit,
`NSApplication` init failure — it falls back to today's
`AppController(cfg, ...).run()` on the main thread. The inversion lives entirely behind this
`try/except`; the fallback is the current call, unchanged.

> The richer overlay-specific gating — the `[overlay] enabled` config key and the
> `--no-overlay` flag — is **deferred to #21**. #16 only fixes *where* the fail-open branch
> lives (in `cli.run()`) and *what* it degrades to (today's `run()`), not the config surface.

## Consequences

**Positive**

- Satisfies both #17 AppKit constraints: the main thread is blocked in `NSApp.run()` and all
  UI work has a main thread to run on. The overlay's redraw driver (a main-run-loop `NSTimer`
  reading the latest audio level float → `setNeedsDisplay_`, per #17) now has an owner.
- Single lifecycle owner ⇒ single, ordered teardown ⇒ tractable tests for #22.
- Fail-open preserved: no AppKit ⇒ the process is literally today's code path.
- The audio/hotkey threads are unchanged; they keep marshalling any UI work onto main (the
  `NSTimer` reads a shared level float — no lock needed for a single value; per #17).

**Negative / costs accepted**

- `AppController.run()` is **refactored** into `start()` + the (fallback-only) blocking
  `run()`. The controller now exposes two entry shapes: `run()` (blocking, fallback) and
  `start()` + `shutdown()` (host-driven, overlay). This is a real surface increase, accepted
  in exchange for the cleaner threading model.
- The overlay path and the fallback path exercise **different** controller code (`start()`
  vs `run()`). Mitigation: keep the fallback `run()` as `start()` + `_shutdown_event.wait()`
  so `run()` is a thin blocking wrapper over the same `start()` the host calls — the two
  paths then share the setup half and diverge only in who blocks.

**Follow-ups (implementation flow, not this ADR)**

- `docs/ARCHITECTURE.md` §"Threads and executors" must be updated once implemented: on
  macOS the **main thread runs `NSApp.run()`**; the shutdown `Event`/signal handling moves to
  the GUI host. (Do not edit it now — this ADR records the decision; the wording changes when
  the code lands.)
- #20 (notifier/host seam for overlay show/hide) builds *on* this host — the host is the
  natural place the overlay lifecycle hangs off.
- #22 (test strategy) must cover: signal → `shutdown()` → `NSApp` stop ordering; the
  fallback branch taken on a simulated `ImportError`; and that non-macOS never constructs a
  host.

## Open questions (for #21 / implementation)

- Exact host class name and module location (`gui/host.py`? `overlay/host.py`?).
- Whether `start()` is a genuine new public method or `run()` is internally split into
  `start()` + `_block()` with `run()` retained as the fallback wrapper (the mitigation above
  leans toward the latter).
- `NSApp.stop_(None)` vs `terminate_(None)` for the cleanest Python-side exit after
  `shutdown()` — a `/prototype` detail, flagged in #17's open questions.
