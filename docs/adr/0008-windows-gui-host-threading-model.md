# ADR-0008 — The Windows GUI host owns the main thread and pumps Win32 messages

- **Status:** Accepted
- **Date:** 2026-07-27
- **Deciders:** wayfinder grilling session (Hani Momeninia + agent)
- **Issue:** [#59 — Decision: Windows overlay threading / event-loop model](https://github.com/Hanimn/seda/issues/59)
- **Map:** [#39 — Map: Windows recording HUD (cross-platform overlay)](https://github.com/Hanimn/seda/issues/39)
- **Mirrors:** [ADR-0001](0001-gui-host-owns-main-thread.md) — the macOS main-thread inversion. This is the Windows counterpart: same *shape* (a GUI host owns the main thread and drives a non-blocking controller), Win32-specific *mechanics* (a message pump instead of `NSApp.run()`).
- **Informed by:** [#40 research](https://github.com/Hanimn/seda/issues/40) (`docs/research/windows-overlay-recipe.md`) and [#41 prototype](https://github.com/Hanimn/seda/issues/41) — the raw-Win32 recipe **validated on real Windows hardware** (no-focus-steal, click-through, exclusive-fullscreen, clean teardown all PASS; win32 mode of `scratch/proto_windows_overlay.py`).
- **Feeds:** the shared-vs-duplicated host boundary decision (#39 frontier) — unblocked by this ADR.

## Context

The Windows recording HUD (map #39) is drawn with raw Win32 via stdlib `ctypes` (toolkit
settled by #40, hardware-validated by #41). Win32 imposes one threading rule relevant here:

1. **Window-thread affinity** — a window's messages (`GetMessage`/`PeekMessage`,
   `WM_PAINT`, `WM_TIMER`, teardown) must be pumped **by the thread that created the
   window**. Unlike macOS/AppKit, that thread need **not** be the process main thread — Win32
   admits a GUI loop on any thread.
2. **Python signal delivery** — `signal.signal` handlers run only on the **main thread**
   (the same constraint ADR-0001 and `app.py`'s `in_main_thread` guard already navigate).

So Windows genuinely admits **two** architectures where macOS admitted only one: the pump on
the main thread (mirroring ADR-0001), or the pump on a dedicated GUI thread while the
controller keeps its blocking `run()` on main. This ADR settles which, plus the pump
mechanics, the signal→stop path, and teardown.

The **fail-open invariant** carried from the epic holds unchanged: a missing or broken Win32
overlay must degrade to **today's exact terminal behavior** — dictation is never blocked or
altered by the overlay's absence.

## Decision

On the **Windows overlay path**, a GUI host **owns the main thread**, creates the overlay
window there, and runs an **interruptible Win32 message pump** there, driving a non-blocking
`AppController.start()`. On the **fallback path** (non-Windows, or broken Win32), nothing
changes: the host delegates to the untouched blocking `AppController.run()`, exactly as today
and exactly as the macOS fallback does.

Five sub-decisions, each grilled.

### 1. The GUI host owns the main thread (not a dedicated GUI thread)

The Windows host owns the main thread, creates the window and runs the pump there, and drives
the controller through the same split lifecycle ADR-0001 defined: the setup half
(`load()`, `transition(IDLE)`, `hotkeys.start(...)`, `notify(READY)`) is the non-blocking
`start()`; the `shutdown_event.wait()` block is unused on the overlay path (the host blocks in
the pump instead); `AppController.shutdown()` is unchanged and doubles as the host's "stop the
controller" call.

**Decisive reason — signal locality.** Python delivers SIGINT/SIGTERM only to the main
thread, and the pump must service that signal promptly for a clean Ctrl-C. Owning main puts
window creation, the pump, signal servicing, and teardown **all on one thread** — no
cross-thread coordination anywhere in the stop path.

**Rejected alternative — a dedicated GUI thread.** Keep `AppController.run()` blocking on
main (byte-for-byte, like the non-overlay path) and create+pump the overlay window on a
separate thread. Its appeal was maximum fail-open symmetry (the controller's main-thread
`run()`, including its own signal handling, stays wholly untouched). Rejected because
window-thread affinity means the main thread **cannot** tear down a window the GUI thread
created: a SIGINT landing on main would have to signal the GUI thread to stop pumping and
dispose *its own* window — reintroducing the exact cross-thread teardown handshake ADR-0001
rejected for macOS's Option A. The "untouched controller" appeal is undercut because the
overlay still needs a clean cross-thread stop. Owning main is the single-owner model, and it
is the one #41 validated on hardware.

### 2. An interruptible `PeekMessage` pump (not blocking `GetMessage`)

The pump is our own loop: each iteration drains pending messages with
`PeekMessageW(..., PM_REMOVE)` (translate + dispatch), checks the stop flag (§3), and sleeps
~5 ms. This is the loop the #41 prototype validated on hardware.

**Rejected alternative — blocking `GetMessage`.** The textbook Win32 loop parks in a kernel
wait until a message arrives. It has lower idle CPU but **swallows SIGINT**: the thread sits
in a C syscall, so the pending Python signal handler never runs and Ctrl-C is dead until some
unrelated message happens to wake the loop. Making it responsive would require a
`PostThreadMessage`-from-the-signal-handler bridge — fragile machinery we avoid by polling.

The polling cost is negligible: the overlay already runs a **60 Hz redraw timer** while
visible (the waveform animation), so the thread wakes every ~16 ms regardless; a 5 ms poll
sleep is the same order and dwarfed by the redraw. This is the **Win32-specific divergence
from ADR-0001**: macOS blocks in the opaque `NSApp.run()` and needs a *separate* ~10 Hz pump
timer to service its stop flag; here the pump **is** the servicing loop, so no separate timer
is needed.

### 3. Signal → stop is a bare polled flag (not a posted window message)

`signal.signal(SIGINT/SIGTERM, handler)` where the handler does the one thing a signal handler
should — set a module-level `stop_requested = True`. Each pump iteration checks it and breaks
to teardown. Zero Win32 in the stop path.

**Rejected alternative — the handler posts a Win32 message**
(`PostMessage(hwnd, WM_CLOSE)` / `PostThreadMessage`). Only justified under a *blocking*
`GetMessage` pump (which we rejected in §2); with an interruptible pump already checking a
condition every iteration, a message round-trip buys nothing over a flag and adds failure
surface (a stale `hwnd`, a failed post). This mirrors ADR-0001's shutdown model: the macOS
handler sets state via `shutdown()` then stops the loop; ours sets a flag the pump services —
same model, different loop mechanics. (The #41 prototype's `WM_CLOSE`-post is a **different**
trigger — a console-quit input thread that cannot touch the window directly — and does not
exist in the real app.)

### 4. Teardown: `controller.shutdown()` then `finally:` window disposal

On a stop request the pump breaks and the sequence is:

1. `AppController.shutdown()` — the unchanged §22 order (stop hotkeys, cancel recorder, drain
   the worker, close the backend);
2. then, in a **`finally`** block, the Win32 window teardown: `KillTimer` → `DestroyWindow` →
   `UnregisterClass`.

The `finally` guarantees the window is disposed even if `shutdown()` raises — the same
defensive ordering ADR-0001 chose. Controller-first quiesces hotkeys/recorder/worker before
the window vanishes, so nothing reacts to a half-torn-down window. Because window creation,
pump, and teardown are all on main (§1), the main thread disposes its **own** window — no
cross-thread issue.

Per #51's macOS reclaim finding and the Win32 equivalent, a **dead process's windows are
destroyed by the OS on termination** — so this in-app teardown is the *clean path* over a
guaranteed OS backstop, not the only safety net. The #41 prototype measured `IsWindow(after)
= False` after this exact sequence, confirming no lingering window on the clean path.

### 5. Fail-open seam in `cli.run()`; host abstraction deferred

The platform selection lives in `cli.run()`, extending the existing pattern: on Windows,
build+run the Windows host; on darwin, the macOS host (`run_with_overlay`, today); otherwise —
or on **any** host construction failure — fall through to the untouched blocking
`AppController.run()`. Each host is independently fail-open: a broken Win32 call returns a
False-equivalent / raises-caught, and dictation proceeds on the terminal path. The #41
`ctypes` sketch already wraps window creation in try/except for exactly this.

**Explicitly out of scope — the shared-vs-duplicated host boundary.** Whether the macOS and
Windows hosts share a platform-neutral `OverlayHost`/`Overlay` interface with per-platform
backends, or stay independent hosts sharing only the `Notifier`/`OverlayNotifier` seam, is the
**next** #39 ticket — the one this ADR unblocks. Deciding it here would pre-empt a decision
this ADR exists to enable. For now the Windows host is a platform-selected **sibling** of the
macOS host behind `cli.run()`'s try/fallthrough.

## Consequences

**Positive**

- One thread owns window creation, the pump, signal servicing, and teardown — the simplest
  correct model, with no cross-thread stop handshake. Hardware-validated (#41).
- Ctrl-C works without a signal→window-message bridge (the interruptible pump).
- Shutdown *model* is genuinely shared with macOS (ADR-0001): flag/handler → quiesce
  controller → dispose window in `finally`. Only the loop mechanics differ.
- Fail-open preserved: any broken/missing Win32 → terminal behavior, unchanged.

**Negative / costs accepted**

- The pump polls (~5 ms sleep) rather than blocking — a small idle-CPU cost, dwarfed by the
  60 Hz redraw the HUD runs while visible, and only while the overlay path is active.
- A second platform host now sits behind `cli.run()`; the risk of macOS/Windows host
  divergence is real but deferred to the boundary ticket (which may unify them).

## Notes for implementation (non-binding; the boundary ticket may reshape)

- The pump, signal flag, and teardown are the shape already exercised by
  `scratch/proto_windows_overlay.py` (win32 mode) — the implementation can lift that structure
  rather than rediscover it.
- `_declare_prototypes()`-style `restype`/`argtypes` on every `ctypes` call is mandatory on
  Win64 (the prototype documents why — HWND truncation) and must carry into production.
- Whether any of this is shared with the macOS `run_with_overlay` is the boundary ticket's
  call, not this one's.
