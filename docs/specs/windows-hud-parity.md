# Spec: Windows recording HUD — visual + behavioral parity

**Status:** ready to implement (transparency path gated on spike [#66](https://github.com/Hanimn/seda/issues/66)) · **Map:** [Windows recording HUD](https://github.com/Hanimn/seda/issues/39) · **Ticket:** [#63](https://github.com/Hanimn/seda/issues/63)
**Verification:** manual, by eye + exit-path runs, on real Windows hardware (mirrors the macOS by-eye boundary of ADR-0005).

Bring the macOS recording HUD to **Windows at visual + behavioral parity**: the same persistent
companion — compressed **idle** pill, **listening** wave, **busy** pulse — in the same
bottom-centre translucent rounded card, driven by the same event→mode contract, removed only on
close. Drawn with raw Win32 + GDI+ via stdlib `ctypes` (zero new deps), behind the Windows
sibling host of [ADR-0009](../adr/0009-overlay-host-boundary.md).

This spec is a **1:1 port of the macOS render truth** in `src/seda/gui/host.py`
(`build_overlay` / `WaveformView.drawRect_`) and the lifecycle contract in
[ADR-0007](../adr/0007-persistent-hud-lifecycle.md). Where a value is a tune-by-eye knob on
macOS, the *form* is fixed here and the constant is settled by eye on Windows — **using the same
constant as macOS**, not a re-tuned one.

## Grounding decisions (settled)

- **Toolkit:** raw Win32 via `ctypes` — [ADR-0008](../adr/0008-windows-gui-host-threading-model.md) (#40/#41).
- **Threading / pump / teardown:** host owns main, interruptible `PeekMessage(PM_REMOVE)` pump, `finally:` teardown — ADR-0008.
- **Host boundary:** sibling `gui/host_win.py` over the shared `gui/_hostloop.py`; the `Overlay` struct is duplicated per platform; `OverlayNotifier(show, hide, set_mode, dispatch_main)` is the cross-host contract — ADR-0009.
- **Redraw cadence:** the shared ~60 Hz-active / ~10 Hz-idle policy — [ADR-0007 §5](../adr/0007-persistent-hud-lifecycle.md) (both hosts use the *same* rate pair).
- **Transparency path:** **Option B** — `UpdateLayeredWindow` + per-pixel premultiplied ARGB, antialiased via system `Gdiplus.dll` (see [ADR-0010](../adr/0010-windows-hud-transparency-path.md)). **Gated on the on-hardware spike [#66](https://github.com/Hanimn/seda/issues/66);** Option A is the documented fallback.

## Files (new / touched, at implementation)

| File | Change |
|---|---|
| `src/seda/gui/host_win.py` | **new** — the Windows sibling host: `Overlay` struct + `build_overlay` (layered window + GDI+ draw) + a `run_loop` calling the shared `run_hosted`. Windows analogue of `host.py`. |
| `src/seda/gui/_hostloop.py` | **new** (ADR-0009) — shared gate/fail-open/lifecycle skeleton both hosts call. |
| `src/seda/cli.py` | `_HOST_MODULES` platform map (ADR-0009); `_register` wiring unchanged. |
| `src/seda/notifications/__init__.py` | **unchanged** — `OverlayNotifier` is already platform-neutral; the Windows overlay satisfies the same four-callable contract. |

No changes to the recorder, the state machine, the notification-event enum, or `controller.latest_level` (reused as-is).

---

## Part 1 — Rendering / transparency (Option B, spike-gated)

A layered top-level window, click-through, never-activating:

- **Styles:** ex-style `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`; style `WS_POPUP` (no caption/border). `WS_EX_LAYERED | WS_EX_TRANSPARENT` = click-through (ADR-0008 recipe §3). Never `SetForegroundWindow`.
- **Init once:** `GdiplusStartup` (token held for teardown). `Gdiplus.dll` is a system DLL since XP (no redist) — `ctypes.windll.gdiplus`.
- **Per frame:** `CreateDIBSection` (top-down 32-bit ARGB) → `GdipCreateFromHDC` on its DC → `GdipSetSmoothingMode(SmoothingModeAntiAlias)`, `GdipSetCompositingMode(SourceOver)` → clear to 0 → draw card + bars (Parts 2–3) → premultiply → `UpdateLayeredWindow` with `BLENDFUNCTION{ AC_SRC_ALPHA, 255 }` (`ULW_ALPHA`).
- **Premultiply footgun:** ARGB must be premultiplied or edges halo. The palette is a black card + gray/white bars, so premultiplication folds into the brush-color/alpha choice — **no per-pixel Python loop**; keep it in one guarded helper.
- **Show without activating:** `ShowWindow(SW_SHOWNOACTIVATE)` (first show) / `SW_SHOWNA` thereafter.

**Fidelity claim:** Option B reproduces the macOS look **exactly** — the independent card alpha
(0.55) vs bar alpha (0.92 / 0.45→0.92), and antialiased rounded corners + bar caps — which
Option A (whole-window `LWA_ALPHA`, 1-bit color-key) structurally cannot. See ADR-0010.

---

## Part 2 — Geometry (1:1 with macOS `build_overlay`)

- **Active panel:** 160 × 48 px. **Idle panel:** 48 × 24 px (per #56 panel-shrink).
- **9 bars**, 6 px wide, 3 px gap, cluster centred horizontally; vertical centre `cy`.
- **Span** (half-height at full amplitude) = 42% of panel height. Bars never fully vanish: min half-height 2 px. Bar caps rounded, radius = width/2.
- **Position:** bottom-centre of the primary monitor. Win32 origin is top-left, so
  `y = workArea.bottom − PANEL_H − 80` (the macOS `y=80`-from-bottom, flipped).
- **DPI:** declare per-monitor-v2 awareness (`SetProcessDpiAwarenessContext`) and scale all px by the monitor's effective DPI, so the HUD is the same physical size the macOS Retina panel is.

---

## Part 3 — Three modes (1:1 port of `WaveformView.drawRect_`)

Same bar geometry in all three modes, so a mode switch reads as one widget changing state.
`_state` (level + frame counter + mode) is mutated by `set_mode`/the redraw tick and read in the
draw; single-threaded on the pump thread (Part 4), so no lock.

- **Card (all modes):** rounded rect, radius 8, black at **alpha 0.55**, no shadow.
- **LISTENING** (mic-driven mirror EQ): `_GATE = 0.006`, `level = clamp(sqrt(max(0, rms − _GATE)) * _GAIN, 0, 1)`, `_GAIN = 2.6`; raw/instant (no smoothing). Triangular per-bar weight 1.0 at centre → 0.35 at edges. Level-scaled jitter `1 + 0.3·level·sin(phase·9 + i)` (vanishes at silence). Bars at **alpha 0.92**.
- **BUSY** (time-driven sweep, no mic): `head = (phase·3.2) % (9+3)`; per bar `bump = exp(−d²/2.2)` with `d = i − head`; half-height `= span·(0.18 + 0.62·bump)` (0.18 baseline so bars never vanish); **alpha = 0.45 + 0.47·bump**.
- **IDLE** (per [#56](https://github.com/Hanimn/seda/issues/56)): a **compressed pill** — short horizontal capsule — in the shrunk 48×24 panel, with a faint slow shimmer as an **alpha pulse** (`≈ 0.4 + 0.1·sin(slow phase)`). Under Option B this is a real per-pixel alpha pulse (no gray-value substitution). Waking widens the pill back into the 9 bars.

`phase = frame / 60.0` as on macOS. Constants are the macOS constants verbatim; only the
drawing primitive (GDI+ path vs `NSBezierPath`) differs.

---

## Part 4 — Lifecycle + threading

**Event→mode — identical to [ADR-0007 §2](../adr/0007-persistent-hud-lifecycle.md), ports 1:1** (the `OverlayNotifier` is platform-neutral; nothing platform-specific leaks in):

| Event | Contract |
|---|---|
| `READY` | show + `set_mode(IDLE)` |
| `RECORDING` | show (idempotent) + `set_mode(LISTENING)` |
| `BUSY` | show (idempotent) + `set_mode(BUSY)` |
| `TRANSCRIBING` | ignored |
| `SUCCESS` / `CANCELLED` / `ERROR` | `set_mode(IDLE)` (stays shown) |
| *app close* | `Overlay.teardown` — the **only** removal path |

`_visible` is a one-shot latch; nothing in the event stream hides; after the first show every
cycle is pure flicker-free `set_mode` on the same window. **Accepted parity gaps carried from
macOS** (not new): no distinct `ERROR` beat (settles to IDLE); no HUD during model load (appears
at `READY`).

**Threading (ADR-0008):**

- The `PeekMessage(PM_REMOVE)` pump on the host's main thread is the single UI thread; all draw + `set_mode` + resize run there.
- **`dispatch_main` = a thread-safe `queue.Queue`, drained each pump iteration.** The `notify()` calls arrive on the pynput-listener / worker threads (never the pump thread), so `set_mode`/`show` are enqueued fire-and-forget (matching macOS `performSelectorOnMainThread_…waitUntilDone:False`). **Not `PostMessageW`** — a Python closure can't ride `wParam`/`lParam` without a side-table that reintroduces a GC-keepalive hazard, and the pump already polls, so the queue is strictly simpler.
- **Redraw:** a `SetTimer` whose interval follows [ADR-0007 §5](../adr/0007-persistent-hud-lifecycle.md) — active (~60 Hz) in `LISTENING`/`BUSY`, idle (~10 Hz) in `IDLE` — re-armed on every `set_mode`. `WM_TIMER` reads `latest_level` and `InvalidateRect`s; the paint path re-blits via `UpdateLayeredWindow`.
- **Panel-shrink:** `SetWindowPos(SWP_NOACTIVATE | SWP_NOZORDER)` on the IDLE↔active transition inside `set_mode` — no focus steal, topmost band untouched. Because Option B supplies the bitmap, the resize and the next `UpdateLayeredWindow` must be **dimension-matched in the same turn** or the shrink tears for one frame; do the re-blit at the new size immediately after the `SetWindowPos`.

**Teardown (ADR-0008, in the shared `run_hosted` `finally`, after `controller.shutdown()`):**
`KillTimer → DestroyWindow → UnregisterClassW`, then free the DIB section and `GdiplusShutdown`.
Idempotent + fail-open (each step guarded; a failure never blocks shutdown). Hold the WNDPROC
`ctypes` reference alive **past** `DestroyWindow` (its `WM_DESTROY` dispatches to the WNDPROC
during the call). A dead process's windows are OS-reclaimed regardless (#41 Q6) — this is the
clean path, not the only net.

---

## Part 5 — Test strategy (ADR-0005 parity)

Three tiers, mirroring the macOS strategy:

- **T1 — unit (Linux CI, Win32-free):** every GDI+/Win32 touch sits behind a module-level shim (`_make_window`, `_blit`, `_show`, `_set_timer`, `_dispatch`, …) monkeypatched in tests — CI never loads `ctypes.windll`. Assert: the event→mode mapping (Part 4 table); `set_mode(IDLE)` selects the idle timer interval and `LISTENING`/`BUSY` the active one (ADR-0007 §5 mechanism, fake timer); `dispatch_main` enqueues and the pump drains in order; the fail-open fallback to the terminal path on a simulated build failure. **Conformance test** (ADR-0009): assert the Windows `Overlay`'s four callables are **callable and effectful** — *not* `inspect.signature`-shaped (the shipped macOS `Overlay` defaults `set_mode`/`teardown` to no-ops, so a signature check is vacuous).
- **T2 — integration (Windows-only, deselected in CI):** `@pytest.mark.integration` + `skipif(sys.platform != "win32")`. Constructs the real layered window and runs `show`/`set_mode`/`teardown` without raising — proves the `ctypes` prototypes (`restype`/`argtypes`) are spelled right.
- **T3 — by-eye on hardware (checklist):** never steals focus (type into another app while it shows/animates); draws over exclusive + borderless fullscreen; idle pill / listening bars / busy sweep read as one widget changing state; persistent across dictations (no flash between); idle CPU is sane at the throttled rate; gone after normal quit **and** Ctrl-C.

---

## Accepted parity gaps

- **Carried from macOS** (contract-level, not Windows-specific): no distinct `ERROR` beat; no HUD during model load. (ADR-0007 §2.)
- **Only if the spike [#66](https://github.com/Hanimn/seda/issues/66) fails and we fall back to Option A:** flattened card-vs-bar contrast (one whole-window alpha) and 1-bit aliased corners. Documented in ADR-0010; not accepted unless the spike forces it.
