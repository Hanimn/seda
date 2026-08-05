# Research: how macOS reclaims / orphans an overlay NSPanel on exit & crash

**Ticket:** [#52](https://github.com/Hanimn/seda/issues/52) · **Branch:** `research/hud-window-reclaim` · Research only — no `src/` changes.

## Goal

Surface concrete, citable facts a cleanup decision can rest on, for making Seda's
borderless always-on-top `NSPanel` HUD (`src/seda/gui/host.py`) reliably disappear
after the owning process:

- exits **normally** (graceful stop, `app.stop_()` → run loop returns → `finally: teardown()`),
- **crashes** on a native fault (SIGABRT / segfault — the pynput Carbon TIS hazard noted in `host.py`),
- is **SIGKILL**ed (uncatchable).

The panel in question: `NSWindowStyleMaskNonactivatingPanel | Borderless`, `setLevel_(NSStatusWindowLevel)`,
collection behavior `CanJoinAllSpaces | Stationary | FullScreenAuxiliary | IgnoresCycle`, click-through,
shown via `orderFrontRegardless()`. Its only cleanup path today is `overlay.teardown()`
(`timer.invalidate()` + `panel.orderOut_(None)` + `panel.close()`) in the `finally` around `app.run()`.
`host.py`'s own comment concedes "process-exit reclamation is not immediate/guaranteed."

---

## 1. WindowServer behavior: teardown, promptness, and orphaning

### The ownership model (why windows normally vanish on exit)

macOS windows are not owned by the app in isolation — every GUI process holds a **connection to the
window server** (WindowServer / Quartz Compositor, the display server + compositing window manager;
private CoreGraphics/SkyLight API surface exposes this as a `CGSConnectionID`). Each on-screen window is
a server-side surface **created and owned within that client connection**. WindowServer receives a bitmap
of each window's contents plus its position and composites it
([Quartz Compositor](https://en.wikipedia.org/wiki/Quartz_Compositor)).

The load-bearing consequence: **window lifetime is bound to the client connection, not to whether the app
ran any teardown code.** When a process dies — normally, on a native abort, or via SIGKILL — the kernel
tears down that process's Mach ports, the WindowServer connection drops, and the server destroys the
surfaces that belonged to that connection. This is *connection-based reclamation* and it does **not**
require `atexit`, `applicationWillTerminate:`, or `panel.close()` to have run. It is the reason a hard-killed
Cocoa app's windows normally disappear on their own.

**Practical read for Seda:** in the common case, even a SIGKILL of the Seda process removes the HUD, because
the server reaps the connection's surfaces. The in-process teardown in `host.py` makes disappearance *prompt
and deterministic on the graceful/exception paths*; it is not the sole thing standing between a crash and a
clean screen.

### Promptness

On a clean exit the app orders out / closes the window first, so removal is immediate. On abnormal exit the
removal happens when the kernel finishes reaping the process and WindowServer notices the dropped connection
— effectively immediate at human timescale (sub-second), **not** deferred to a restart in the normal case.

### When it can visually orphan ("survives until reboot")

The "pixels remain until logout/reboot" symptom is real but is **not** the default connection-teardown path.
Reported/plausible causes, in rough likelihood order for an overlay like ours:

1. **A surviving process still owns the surface.** The most common real cause of a "stuck window until reboot"
   is that *some* process still holds the window's connection open — not necessarily the one you think died.
   If the overlay's server-side surface is ever adopted by, referenced from, or duplicated into another
   still-living connection (e.g. a helper/child that inherited it, or an accessibility/screen-capture client
   holding a reference), killing the original parent does **not** drop the surface. **This is the failure mode
   a watchdog is designed to close** (see §3): a *separate* live process is exactly what keeps a window alive
   past the crash of its creator.
2. **Compositor / Dock / Mission Control cache staleness.** WindowServer and the Dock keep a compositing/thumbnail
   cache. A crash at the wrong moment can leave a stale composited frame on screen even after the surface is gone;
   this is the class of bug people clear with `killall Dock` or `killall WindowServer` (the latter forcibly logs
   the session out). It is a compositor-cache artifact, not a live window — but it looks identical to the user.
3. **WindowServer itself wedged.** If WindowServer is hung/degraded, connection-drop notifications back up and
   *everything* orphans until it is restarted. Environmental, not something our app causes.

**Is orphaning tied to abnormal termination, CGS caching, or Spaces/`CanJoinAllSpaces`?**
- Abnormal termination *alone* does **not** orphan — connection teardown still fires (cause #1 is the exception,
  and it requires a *second* live owner).
- `NSStatusWindowLevel` (a high window level) makes an orphan *more visible and more annoying* (it floats over
  everything) but does not by itself prevent reclamation.
- `CanJoinAllSpaces` / `Stationary`: no primary source ties these to permanent orphaning. They make the window
  present on every Space, so a *cache-staleness* ghost (cause #2) would appear on every Space rather than one —
  amplifying the symptom, not creating it. Treat "all-Spaces caused the leak" as unproven; the amplification of a
  cache ghost is the defensible statement.

**Named cause for "survives until reboot," stated honestly:** when it is a *live* window (not a cache ghost),
the cause is **a surviving process still holding the window-server connection for that surface** — i.e. the
creator died but an owner did not. When it is *not* a live window, it is **WindowServer/Dock compositor cache
staleness**, cleared by `killall Dock` / `killall WindowServer` / logout / reboot. The pure "app crashed and its
surface leaked with no other owner" case is *not* well-supported by primary sources; connection reclamation
covers it.

> Sourcing note: Apple does not publish the WindowServer connection-teardown contract (the CGS/SkyLight API is
> private). The connection-ownership model above is the well-established behavior behind Quartz Compositor
> ([Wikipedia: Quartz Compositor](https://en.wikipedia.org/wiki/Quartz_Compositor)) and the private
> `CGSConnectionID` surface. The specific orphan causes are drawn from that model plus the widely-used
> `killall Dock` / `killall WindowServer` remedies; where a claim is inference rather than documented, it is
> flagged above.

---

## 2. In-process cleanup hooks and which exit paths each covers

### `atexit` — normal-exit only

`atexit` handlers run on **normal interpreter termination** and on **`sys.exit()`**. They are explicitly
**not** called when:

- the program is **killed by a signal not handled by Python** (SIGKILL, and SIGTERM/SIGINT unless a Python
  handler is installed),
- a **Python fatal internal error** is detected,
- **`os._exit()`** is called.

> "The functions registered via this module are not called when the program is killed by a signal not handled
> by Python, when a Python fatal internal error is detected, or when `os._exit()` is called."
> — [docs.python.org/3/library/atexit.html](https://docs.python.org/3/library/atexit.html)

**Native abort (SIGABRT / segfault): does NOT fire.** A native abort is precisely "a signal not handled by
Python" / a fatal error — atexit is skipped. **Gap confirmed.**

### `sys.excepthook` — uncaught *Python* exceptions only

Fires for an unhandled Python-level exception propagating out of the main thread (it is what prints the
traceback). It runs pure Python, so it *can* call `overlay.teardown()`. But it only sees Python exceptions —
a native SIGABRT/segfault never becomes a Python exception, so **`sys.excepthook` does NOT fire on a native
abort. Gap confirmed.** (Also: exceptions on non-main threads use `threading.excepthook`, not this one.)

### `faulthandler` — native faults, but no cleanup

`faulthandler.enable()` installs handlers for **SIGSEGV, SIGFPE, SIGABRT, SIGBUS, SIGILL** (plus a Windows
exception handler). On a fault it **dumps a Python traceback and then lets the process die** — it re-raises
into the default disposition. Crucially it runs only **signal-safe functions** ("it cannot allocate memory on
the heap… traceback dumping is minimal"), so it **cannot run arbitrary Python cleanup** — it cannot call
`panel.close()` or `overlay.teardown()`.
— [docs.python.org/3/library/faulthandler.html](https://docs.python.org/3/library/faulthandler.html)

**On a native abort: faulthandler gives you a *diagnostic* (the traceback), NOT teardown.** It closes the
"why did we crash" gap, not the "remove the HUD" gap. Useful to catch the pynput TIS SIGABRT in logs; it does
**not** reap the panel.

### Obj-C `applicationWillTerminate:` / `NSApplicationWillTerminateNotification`

Fires only on **graceful, intentional termination** — `[NSApp terminate:]` / the Quit command. It does **not**
fire on crash, SIGKILL/SIGABRT.

**And directly relevant to `host.py`:** it does **not** fire when the run loop is stopped via
**`[NSApp stop:]`** — `stop:` merely exits `run()` and returns control to the caller; it does **not** post
`NSApplicationWillTerminateNotification`. `terminate:` is what posts that notification.
— [applicationWillTerminate(_:)](https://developer.apple.com/documentation/appkit/nsapplicationdelegate/applicationwillterminate(_:)),
[NSApplication.stop(_:)](https://developer.apple.com/documentation/appkit/nsapplication/stop(_:))

**Consequence:** Seda's current shutdown uses `app.stop_(None)` (host.py `_pump`), so wiring cleanup into an
`applicationWillTerminate:` observer would **not run on Seda's normal graceful stop** — it would only fire if
shutdown were changed to `terminate:`. The existing `finally: overlay.teardown()` around `app.run()` is the
correct hook for the current `stop_`-based design, and does **not** fire on native abort either.

---

## 3. Out-of-process options for the un-catchable SIGKILL / abort case

None of the in-process hooks in §2 cover SIGKILL, and only a *diagnostic* covers native abort. The only way to
guarantee HUD removal across **every** exit path — including the cases where connection reclamation *doesn't*
save us (a second live owner, §1 cause #1) — is a **separate process that outlives the crash and reaps the
overlay**.

### Parent-death detection on macOS

macOS has **no `prctl(PR_SET_PDEATHSIG)`** (that is Linux-only). Options:

- **kqueue `EVFILT_PROC` + `NOTE_EXIT` on the parent pid** — event-driven, kernel-level, immediate, zero polling
  cost. Register with `EV_SET(&kev, parent_pid, EVFILT_PROC, EV_ADD, NOTE_EXIT, …)` then block in `kevent()`.
  Caveat: a race between capturing `getppid()` and registering — if the parent dies in that window you miss the
  event; guard with a `getppid()==1` re-check after registering.
- **Polling `getppid()==1`** — orphaned children reparent to `launchd` (pid 1) on macOS, so `getppid()==1` means
  the parent is gone. Simple, portable, but adds latency (poll interval) and a busy-ish loop; no registration
  race. Good *fallback* alongside kqueue.
- **XPC** — heavier; a full XPC service is more machinery (and packaging/entitlement surface) than a HUD reaper
  warrants.

Python note: `EVFILT_PROC`/`NOTE_EXIT` are reachable from Python via the `select.kqueue` API (`select.kevent`
with `filter=select.KQ_FILTER_PROC`, `fflags=select.KQ_NOTE_EXIT`), so a watchdog can be pure Python — no C.

### Cheapest viable design for a local-first single-binary app

A tiny **watchdog helper subprocess**, spawned by the host right after the panel is built:

1. Parent passes the watchdog its own pid (and, if the reaper needs it, the panel's window number / a token).
2. Watchdog registers kqueue `NOTE_EXIT` on the parent pid, with a `getppid()==1` fallback poll.
3. On parent death, the watchdog **reaps the overlay**. For our design the robust move is *not* to try to
   re-close a specific `CGSWindow` from outside (private API), but to ensure the surface has no surviving owner:
   since the watchdog is a child, it must make sure *it* is not what keeps the connection alive, and then let
   connection reclamation do its job — i.e. the watchdog's job is mostly to (a) detect death, (b) log/flag it,
   and (c) if a stale *cache* ghost is the concern, trigger the standard compositor refresh. A watchdog cannot
   portably force-close another process's window via public API.
4. Watchdog then exits.

**Honest trade-off assessment:**

- **Extra process:** one lightweight child, idle-blocked in `kevent()` (no CPU). Minor.
- **Packaging:** a single-binary app must ship the watchdog as either the same binary re-exec'd with a
  `--watchdog` flag, or a bundled helper. Re-exec of the same frozen binary is the cheapest and keeps "single
  binary" true. Adds startup/teardown wiring and a new failure mode (watchdog fails to spawn → fail open, no
  worse than today).
- **Permissions:** detecting parent death via kqueue needs **no special entitlement**. *Forcibly closing another
  app's window* would need private CGS APIs or Accessibility permission and is fragile — argues for the
  "don't fight the window server, just don't be the second owner + let reclamation run" posture above.
- **Reality check:** for the *specific* Seda risk (its own process aborts on the pynput TIS SIGABRT, with no
  second owner of the surface), **connection-based reclamation already removes the HUD** — a watchdog is belt-and-
  suspenders, not strictly required. A watchdog becomes *necessary* only if a real scenario is found where the
  surface has a surviving owner (a child/helper that inherited it) or a persistent compositor ghost. Recommend:
  **verify empirically** (SIGKILL and `kill -ABRT` the running app, watch the HUD) before paying for a watchdog.

---

## Recommended coverage matrix (exit path → mechanism that covers it)

| Exit path | `finally` + `stop_`/`app.run()` return (current) | `atexit` | `sys.excepthook` | `applicationWillTerminate:` | `faulthandler` | WindowServer connection reclamation | Out-of-proc watchdog |
|---|---|---|---|---|---|---|---|
| **Normal graceful stop** (`app.stop_` → run() returns) | ✅ teardown runs | ✅ | — (no exception) | ❌ (only on `terminate:`, not `stop:`) | — | ✅ (backstop) | ✅ |
| **Uncaught Python exception** out of `app.run()`/`start()` | ✅ (finally runs) | ✅ | ✅ (can call teardown) | ❌ | — | ✅ (on process exit) | ✅ |
| **`sys.exit()`** | ✅ if inside run scope | ✅ | — | ❌ | — | ✅ | ✅ |
| **Native abort — SIGABRT / segfault** (pynput TIS hazard) | ❌ | ❌ | ❌ | ❌ | ⚠️ diagnostic only, **no teardown** | ✅ **primary reclaimer** | ✅ (only guaranteed active reaper) |
| **`os._exit()`** | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| **SIGKILL** (uncatchable) | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ **primary reclaimer** | ✅ (only in-userland reaper) |

Legend: ✅ covers · ⚠️ partial (diagnostic, not cleanup) · ❌ does not fire · — not applicable.

**Reading of the matrix:** for the graceful and Python-exception paths, the existing `finally: overlay.teardown()`
is the right, sufficient hook. For **native abort and SIGKILL**, *no in-process Python hook removes the panel* —
the actual reclaimer is the **WindowServer connection teardown** that fires when the process dies. `faulthandler`
adds a crash traceback (worth enabling to catch the TIS abort) but does not reap the HUD.

## Bottom line

- The likely cause of a genuine "survives until reboot" HUD is **a surviving process still owning the
  window-server surface** (creator died, owner didn't), or a **Dock/WindowServer compositor cache ghost**
  (cleared by `killall Dock`/`killall WindowServer`) — *not* the bare "app aborted" case, which connection
  reclamation handles.
- `atexit`, `sys.excepthook`, and `applicationWillTerminate:` **all miss the native-abort/SIGKILL path**;
  `applicationWillTerminate:` additionally never fires under the current `app.stop_()` shutdown.
- `faulthandler` is worth enabling for the **diagnostic** on the pynput TIS SIGABRT, but it performs **no cleanup**.
- **Bulletproof (SIGKILL) active coverage realistically needs an out-of-process watchdog** (kqueue
  `NOTE_EXIT` on the parent, `getppid()==1` fallback) — but given connection-based reclamation already clears the
  common case, the watchdog is a belt-and-suspenders measure. Recommend an empirical `kill -9` / `kill -ABRT`
  test of the running HUD first; only build the watchdog if a real orphan is reproduced.
