# ADR-0011 — macOS menu-bar UI is native AppKit, created inside the existing host run loop

- **Status:** Accepted
- **Date:** 2026-08-03
- **Deciders:** grilling session (Hani Momeninia + agent)
- **Issue:** [#84 — Decision/ADR: menu-bar UI toolkit — native AppKit vs alternatives](https://github.com/Hanimn/seda/issues/84)
- **Map:** [#83 — macOS menu-bar GUI (`seda gui`)](https://github.com/Hanimn/seda/issues/83)
- **Feeds:** the `seda gui` shell (#87), the settings window (#88), the menu actions (#90) — the toolkit every UI ticket in the map builds on.
- **Informed by:** [ADR-0001](0001-gui-host-owns-main-thread.md) (a macOS GUI host owns the main thread), [ADR-0009](0009-overlay-host-boundary.md) (sibling hosts over a shared lifecycle helper), [ADR-0003](0003-notifier-seam-overlay-lifecycle.md) (notifier seam).

## Context

The map #83 adds a macOS **menu-bar app** — a new `seda gui` entry point with an `NSStatusBar`
status item showing live dictation state (idle/listening/busy) and a dropdown (Settings, Open
Logs, Doctor, Quit), plus a settings window. This ADR locks the **UI toolkit** for that surface;
it is the load-bearing decision the shell (#87), settings window (#88), and menu actions (#90)
all build on.

Two facts from the existing codebase bound the choice:

1. **A GUI host already owns the main thread and the AppKit run loop** (ADR-0001). `_run_appkit_host`
   in `src/seda/gui/host.py` blocks the main thread in `NSApplication.run()`, services SIGINT/SIGTERM
   and shutdown via a ~10 Hz pump `NSTimer`, and does one-time main-thread warming of pynput's
   Carbon Text-Input-Source / `AXIsProcessTrusted` init to avoid a startup race. The app runs under
   `NSApplicationActivationPolicyAccessory` (`host.py:224` — no Dock icon; cannot grab activation).
2. **PyObjC/AppKit is already the sole macOS dependency.** `pyproject.toml` declares only
   `pyobjc-framework-Cocoa` (`sys_platform == "darwin"`); `host.py` already imports `NSApplication`,
   `NSObject`, `NSTimer`, `NSEvent`, and draws an `NSPanel`. `NSStatusBar` / `NSMenu` / `NSWindow`
   live in that same Cocoa framework — a menu-bar item needs **zero new dependencies**.

So the menu-bar UI is not greenfield: it is additive UI inside a run loop that already exists and
is already owned. The question is only which toolkit draws it.

Non-negotiable constraints carried from the map and prior ADRs:

- **The run loop already has one owner** (ADR-0001). Any toolkit must slot *into* that owned loop,
  not bring its own `NSApplication.run()` / signal handling that would contend with the host's
  pump-timer + stop-flag machinery.
- **Local-first, minimal-deps ethos** (README): no cloud, no telemetry; the sole macOS dep is PyObjC.
  A new runtime or heavy dependency for a background utility is a poor fit.
- **`seda run` (the CLI) is unchanged** — the menu-bar app is a sibling entry point (map decision 2),
  consistent with ADR-0009's sibling-host model.

## Decision

**The macOS menu-bar item and settings window are built with native AppKit via PyObjC**
(`NSStatusBar`, `NSMenu`/`NSMenuItem`, `NSWindow`/`NSView`), created **inside the run loop the host
already owns** (ADR-0001), under the existing `NSApplicationActivationPolicyAccessory`. No new
dependency is added; no second `NSApplication.run()` or signal owner is introduced.

The status item is an additive UI element attached to the running host: it reads the same
`HudMode`/notifier stream the HUD consumes (ADR-0003) to show live state, and its menu actions call
into the existing controller/`run_checks()`/config surfaces. Quit maps to the host's existing
`controller.shutdown()` + teardown path.

### Rejected alternative — `rumps`

`rumps` is a small library that wraps `NSStatusBar` for menu-bar apps. Rejected because:

- It expects to **own `NSApplication.run()`** (its `App.run()` starts and blocks the loop). That
  directly contends with the host's existing ownership (ADR-0001) — the pump `NSTimer`, the
  SIGINT/SIGTERM→flag→`stop_` shutdown, and the one-time Carbon/AX warming would have to be
  reconciled with rumps's own loop. Fighting a second loop owner is exactly the dual-loop teardown
  hazard ADR-0001 rejected.
- It is a **new dependency** for what `NSStatusBar` already provides directly in the Cocoa framework
  we already ship.
- It does **not** help with the settings window or the chord-capture widget — those are still raw
  AppKit — so it would buy a little menu boilerplate at the cost of a dependency and a loop-ownership
  conflict.

### Rejected alternative — web-shell (pywebview / Tauri-style)

Render settings as HTML/JS in an embedded webview. Rejected because it adds **heavy dependencies and
a second language/runtime** for a background dictation utility, cutting against the local-first /
minimal-deps ethos, and still needs native code for the status item and global-hotkey capture — so
it adds cost without removing the native surface.

## Consequences

**Positive**

- **Zero new dependencies** — `NSStatusBar`/`NSMenu`/`NSWindow` are in the `pyobjc-framework-Cocoa`
  already required on macOS. The whole app stays one coherent AppKit codebase.
- **One run-loop owner preserved** (ADR-0001). The status item is created on the main thread inside
  the host's existing loop; no competing `NSApplication.run()`, no duplicated signal/shutdown
  machinery. `NSApplicationActivationPolicyAccessory` already suits a status-item-only app (status
  items appear regardless of Dock presence).
- **Consistent with the sibling-host model** (ADR-0009): `seda gui` is a sibling entry point over the
  same host, not a fork of the CLI.
- **Reuses the notifier stream** (ADR-0003): live status is an observer on the existing `HudMode`
  signal — no duplicated mode logic.

**Negative / costs accepted**

- Raw AppKit is more boilerplate than a menu-bar helper lib for the menu itself (rumps would be
  terser for that one piece). Accepted: the boilerplate is small and one-time, and it avoids the
  loop-ownership conflict and the extra dependency.
- The team owns the AppKit UI code directly (status item, menu, window, later the chord-capture
  widget) rather than leaning on a library's abstractions. Accepted: it is the same PyObjC surface
  the HUD already uses, and it keeps full control of the main-thread interaction the host requires.

**Follow-ups (implementation flow, not this ADR)**

- The `seda gui` shell (#87) creates the `NSStatusBar` item inside the host loop and wires Quit to
  `controller.shutdown()`.
- `docs/ARCHITECTURE.md` gains a note that the macOS status item is native AppKit in the host loop,
  once the shell lands (do not edit now — this ADR records the decision; wording changes when code
  lands).

## Open questions (for the implementation tickets)

- Whether the status item is created by extending `_run_appkit_host` directly or by a small
  `gui`-mode wrapper that adds the item before `app.run()` — an #87 detail.
- Exact settings-window shape (modal vs non-modal, programmatic view vs `.xib`) — an #88 detail; this
  ADR only fixes that it is native AppKit, not its layout mechanism.
