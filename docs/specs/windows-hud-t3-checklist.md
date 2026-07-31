# Windows HUD — T3 by-eye verification checklist

**Tier:** T3 (by-eye, on real Windows hardware) · **Map:** [Windows recording HUD](https://github.com/Hanimn/seda/issues/39) · **Epic:** [#74](https://github.com/Hanimn/seda/issues/74) step 5
**Companions:** T1 unit (`tests/unit/test_gui_host_win.py`, Linux CI) · T2 win32-only integration (`tests/integration/test_gui_host_win_integration.py`, G1/G2)

The final tier of the ADR-0005 test strategy: the checks that **cannot** be a unit or
integration assertion because they are perceptual (does it look right?) or environmental
(does it behave right against a real focused app / a real fullscreen game?). Run this on a
real Windows box after the draw core (#74 step 4) and T2 land. Record pass/fail per item on
the step-5 issue.

Everything provable without eyes is already a T1/T2 test — this list is deliberately only
what a machine can't judge. It mirrors the by-eye boundary the macOS HUD accepts (ADR-0005).

## How to run

`seda` starts with the overlay enabled (default). Hold the push-to-talk hotkey to record;
the HUD shows bottom-centre. Have a text editor / browser focused as the "victim" app for
the focus + click-through checks. For the exclusive-fullscreen check, use a D3D sample or a
game set to **Exclusive Fullscreen** (not borderless).

## Checklist

### Focus + input (the decisive properties — #41 re-confirmed under the real draw)
- [ ] **No focus steal.** While the HUD is shown and animating, keep typing into the focused
      app — every keystroke lands in the app; the HUD never takes the caret or the title-bar
      active-look. (Contract: `WS_EX_NOACTIVATE` + `SW_SHOWNOACTIVATE`, never `SetForegroundWindow`.)
- [ ] **Click-through.** Click "on" the visibly-translucent card onto a control underneath —
      the underlying control fires; the HUD never receives the click. (`WS_EX_LAYERED |
      WS_EX_TRANSPARENT`; T2/G2 asserts the bits are set — this confirms the *behaviour*.)

### Always-on-top
- [ ] **Over normal windows.** The HUD floats above every normal + maximized window.
- [ ] **Over borderless/composited fullscreen** (browser F11, borderless game): bars visible +
      moving.
- [ ] **True exclusive fullscreen** (Alt+Enter D3D / "Exclusive Fullscreen" game): the HUD
      **vanishes** — the documented, expected DWM-bypass limitation (#41). If bars stay visible
      you're in borderless, not exclusive; re-enter true exclusive and confirm the vanish.
      Record the app + exact mode.

### Visual parity (the #66 draw, now in the shipping host)
- [ ] **Translucent card, smooth corners.** The rounded card (radius 8) is a *translucent*
      black — the desktop shows through it — with smooth antialiased corners and NO halo fringe
      over dark, mid-grey, and white backgrounds. (SourceCopy, not SourceOver — #66.)
- [ ] **Independent card-vs-bar alpha.** The bars read clearly brighter / more opaque than the
      dim card behind them — two transparencies in one window at once (the Option-B property
      Option A cannot do).
- [ ] **Three modes read as one widget changing state**, not a swap:
  - [ ] **IDLE** — compressed pill in the shrunk panel (once #56 lands; until then IDLE renders
        as the listening layout — note it, don't fail it).
  - [ ] **LISTENING** — mirror-EQ bars track your voice; quiet speech still moves them; a
        silent HUD is flat and still (no shimmering floor).
  - [ ] **BUSY** — a bright band sweeps L→R over a calm baseline; purely time-driven (no mic).
- [ ] **Smooth animation, no tearing/flicker** at the ~60 Hz active rate.

### Lifecycle
- [ ] **Persistent across dictations.** Record → release → record again: the HUD stays up and
      switches mode with no flash / no rebuild between cycles.
- [ ] **Sane idle CPU.** With the HUD shown but idle, CPU is low (the throttled redraw, not a
      busy-spin). Once #56 lands, IDLE should drop to ~10 Hz.

### Teardown (the #37/#38 property, under Option B)
- [ ] **Gone after normal quit.** Quit `seda` normally — the HUD disappears immediately, no
      lingering window. (fail-open F5)
- [ ] **Gone after Ctrl-C.** Kill with Ctrl-C — the HUD disappears, the process exits promptly
      (the pump is genuinely interruptible). (fail-open F5/F6)

## Result

Record on the step-5 issue: per-item pass/fail, the exclusive-fullscreen app + mode, and any
by-eye deltas from the macOS HUD. A full pass closes step 5 and, with it, epic #74.
