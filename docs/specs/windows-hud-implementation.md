# Spec: Windows recording HUD — implementation hand-off

**Status:** ready to implement (one gate: transparency path awaits the on-hardware spike [#66](https://github.com/Hanimn/seda/issues/66); Option A is the documented fallback so the build is **not** blocked on it) · **Map:** [Windows recording HUD](https://github.com/Hanimn/seda/issues/39) · **Ticket:** [#69](https://github.com/Hanimn/seda/issues/69)
**Verification:** T1 unit (Linux CI, Win32-free) + T2 integration (`win32`-only, deselected) + T3 by-eye on real Windows hardware — mirrors [ADR-0005](../adr/0005-overlay-test-strategy.md).

This is the **single hand-off document** for the Windows recording-HUD implementation epic. It
synthesizes the five landed decisions of map #39 into a build sequence, a file manifest, and the
one open gate. It is an **index, not a restatement**: each section points at the ADR or spec that
owns the detail, and does not re-derive it. Read this to know *what to build, in what order, and
where the authority for each piece lives*; read the linked sources for the *why*.

Every decision is landed on `main`. Nothing here is open to re-litigation — the wayfinder map is
at its destination. What remains is a build, plus the #66 spike that only chooses between two
already-specified draw cores.

## The five grounding decisions (all closed, all on `main`)

| Concern | Decision | Authority |
|---|---|---|
| **Toolkit** | Raw Win32 via stdlib `ctypes` — zero new deps, mirroring the minimal PyObjC side | [#40 research](https://github.com/Hanimn/seda/issues/40) → `docs/research/windows-overlay-recipe.md`; validated on hardware [#41](https://github.com/Hanimn/seda/issues/41) |
| **Threading / pump / teardown** | Host owns the main thread; interruptible `PeekMessageW(PM_REMOVE)` pump; bare polled stop flag; `controller.shutdown()` → `finally:` window teardown | [ADR-0008](../adr/0008-windows-gui-host-threading-model.md) ([#59](https://github.com/Hanimn/seda/issues/59)) |
| **Host boundary** | Sibling `gui/host_win.py` (not a shared Protocol) over one shared `gui/_hostloop.run_hosted`; `OverlayNotifier(show, hide, set_mode, dispatch_main)` is the cross-host contract | [ADR-0009](../adr/0009-overlay-host-boundary.md) ([#62](https://github.com/Hanimn/seda/issues/62)) |
| **Visual + behavioral parity** | 1:1 port of macOS render truth + [ADR-0007](../adr/0007-persistent-hud-lifecycle.md) lifecycle; transparency = **Option B** (`UpdateLayeredWindow` + per-pixel ARGB via system `Gdiplus.dll`), spike-gated, Option A fallback | [`windows-hud-parity.md`](windows-hud-parity.md) + [ADR-0010](../adr/0010-windows-hud-transparency-path.md) ([#63](https://github.com/Hanimn/seda/issues/63)) |
| **Fail-open** | Exhaustive Win32/GDI+ failure-mode catalog; two-layer model; the propagate boundary is `controller.start()` | [`windows-hud-fail-open.md`](windows-hud-fail-open.md) ([#64](https://github.com/Hanimn/seda/issues/64)) |

Supporting chain: [ADR-0001](../adr/0001-gui-host-owns-main-thread.md) (macOS main-thread inversion, the shape ADR-0008 mirrors), [ADR-0003](../adr/0003-notifier-seam-overlay-lifecycle.md) (the `Notifier`/`OverlayNotifier` seam, platform-neutral and reused unchanged), [ADR-0005](../adr/0005-overlay-test-strategy.md) (the module-level-shim test strategy this port copies), [ADR-0006](../adr/0006-overlay-busy-mode-on-release.md) (BUSY timing).

## File manifest

| File | Change | Owner decision |
|---|---|---|
| `src/seda/gui/_hostloop.py` | **new** — shared gate → build-in-try → `False` → register → `start()` → run_loop → `finally: teardown` skeleton, `run_hosted(...) -> bool`. Written **once**, called by both hosts. | ADR-0009 §2 |
| `src/seda/gui/host_win.py` | **new** — the Windows sibling host: its own `Overlay` struct, `build_overlay` (layered window + GDI+ draw), and a `run_loop` (pump) calling `run_hosted`. Windows analogue of `host.py`. | ADR-0009 §1, ADR-0008, parity spec |
| `src/seda/gui/host.py` | **refactor, behavior-preserving** — `run_with_overlay` becomes a thin adapter over `run_hosted`; the AppKit body and `NSApplication` handle stay untouched inside its `run_loop`. Existing ADR-0005 tests must pass **unchanged**. | ADR-0009 §2 |
| `src/seda/cli.py` | `_HOST_MODULES = {"darwin": …, "win32": …}` platform map + `_select_host_module`; the `_register` wiring and terminal fallback stay **byte-for-byte**. | ADR-0009 §3, ADR-0008 §5 |
| `src/seda/notifications/__init__.py` | **unchanged** — `OverlayNotifier` is already platform-neutral; the Windows `Overlay` satisfies the same four-callable contract. | ADR-0003, ADR-0009 §1 |
| `tests/unit/test_gui_host_win.py` | **new** — T1 suite mirroring `tests/unit/test_gui_host.py` (AppKit fakes → Win32-shim fakes). | fail-open §5, ADR-0005 |
| `tests/unit/test_cli.py` (or equivalent) | extend — `_HOST_MODULES` selection incl. unknown-platform → terminal, host `ImportError` → terminal. | fail-open A1/A2/B1 |
| `tests/integration/test_gui_host_win.py` | **new** — T2, `@pytest.mark.integration` + `skipif(sys.platform != "win32")`; the two latent-corruption modes (G1/G2) no T1 can catch. | fail-open §4 |

**Untouched, on purpose:** the recorder, the state machine, the `NotificationEvent` enum, and
`controller.latest_level` (reused as-is — a locked scope decision of the map).

## Build order (each step is independently reviewable)

The ordering is chosen so every step lands behind green CI on Linux/macOS before any Windows-only
code exists, and so the safety-critical macOS refactor is proven by its own untouched test suite
first.

1. **`_hostloop.run_hosted` + the macOS refactor to a thin adapter** (ADR-0009 §2). Land these
   together: the helper is only correct if it preserves `run_with_overlay`'s public signature and
   fail-open behavior, and the macOS ADR-0005 tests are the regression net that proves it. **No
   Windows code yet.** Gate: the existing `tests/unit/test_gui_host.py` passes unchanged.
2. **`cli.py` `_HOST_MODULES` switch** (ADR-0009 §3, ADR-0008 §5). Platform selection + terminal
   fallback; `darwin` resolves to today's host, unknown platform → terminal. Gate: unknown-platform
   and host-`ImportError` tests (fail-open A1/A2/B1) green on Linux CI.
3. **`host_win.py` skeleton — lifecycle + threading, no real drawing** (ADR-0008). The `Overlay`
   struct, `build_overlay` behind module-level shims, the `PeekMessageW(PM_REMOVE)` pump, the
   `queue.Queue` `dispatch_main`, the stop flag, and the `finally:` teardown ordering. Draw calls
   are shimmed. Gate: the **whole T1 fail-open suite** (event→mode, dispatch drain order, timer
   interval selection, every C/D/E/F row, the conformance test) green on Linux CI — CI never loads
   `ctypes.windll`.
4. **The Option-B draw core** (parity spec Parts 1–3, ADR-0010) — **gated on spike #66**. The
   ~50-line `GdiplusStartup → CreateDIBSection → GdipCreateFromHDC → premultiplied ARGB →
   UpdateLayeredWindow → GdiplusShutdown` core plus the three modes' math ported verbatim from
   `WaveformView.drawRect_`. This is the **only** step the spike gates; everything before it is
   transparency-path-agnostic (ADR-0010 §3). If #66 fails, swap in the Option-A core (same seam,
   accepting the ADR-0010 §1 parity gaps) — a small, local change.
5. **T2 integration + T3 by-eye on hardware** (fail-open §4–5, parity Part 5). The `win32`-only
   prototype tests (G1 declared prototypes, G2 styles-applied) and the by-eye checklist
   (no-focus-steal, click-through, exclusive-fullscreen, persistent-across-dictations, sane idle
   CPU, gone after quit **and** Ctrl-C).

## The one open gate — spike #66

[Prototype: prove Option B on real Windows hardware](https://github.com/Hanimn/seda/issues/66)
runs on a Windows box and proves the Option-B stack renders without alpha halos, with antialiased
corners, and tears down clean (`IsWindow(after) == False`, no leaked GDI+ token). **PASS →**
build step 4 as specified. **FAIL →** fall back to Option A, localized to the draw+blit core,
accepting the flattened card/bar contrast + 1-bit corners (ADR-0010 §1). The spike gates **only**
step 4's draw core — it does not block steps 1–3, and this document already specifies both paths,
which is why the write-up did not wait on it.

## Cross-cutting invariants (do not regress)

- **Fail-open boundary is `controller.start()`, not `build_overlay`'s return** (fail-open §3).
  Everything through the last pre-`start()` step (build + `register_overlay` + `signal.signal` +
  the initial `SetTimer`) **fails open** to the terminal path; at/after `start()`, failures
  **propagate**. The `finally:` teardown runs on every exit and must never mask the original error.
- **`build_overlay` is transactional** (fail-open C0): on any failure it disposes what it has
  allocated **in reverse order** and re-raises — a failed build leaks zero resources (GDI+ token,
  class atom, HWND, DIB). This is the build-time twin of teardown's `finally`.
- **`dispatch_main` is a pump-drained `queue.Queue`, not `PostMessageW`** (parity Part 4,
  ADR-0008). `notify()` arrives on listener/worker threads; frames are enqueued fire-and-forget;
  the queue is **bounded** (`maxsize=256`, `put_nowait`, drop-newest-and-log on `Full`) so a dead
  pump can never block a live producer (fail-open E6).
- **Redraw cadence is a shared cross-platform policy** ([ADR-0007 §5](../adr/0007-persistent-hud-lifecycle.md)):
  ~60 Hz active (`LISTENING`/`BUSY`), ~10 Hz idle (`IDLE`) — the **same** rate pair as macOS,
  re-armed on every `set_mode`. Neither host re-tunes it.
- **The `OverlayNotifier` four-callable contract is the only cross-host promise** (ADR-0009 §1).
  Conformance is proven by a **callable-and-effectful** test (invoke each against a fake, observe
  the effect) — **not** `inspect.signature`, which the shipped `Overlay`'s no-op `set_mode`/
  `teardown` defaults make vacuous (ADR-0009 Consequences).
- **Constants are the macOS constants verbatim** (parity Parts 2–3): geometry (160×48 active /
  48×24 idle, 9 bars 6px/3px, 42% span, `y = workArea.bottom − PANEL_H − 80`), the three modes'
  math, `phase = frame/60.0`. Only the drawing primitive differs (GDI+ path vs `NSBezierPath`).
  Per-monitor-v2 DPI awareness so the HUD is the same **physical** size as the Retina panel.
- **Declared `restype`/`argtypes` on every `ctypes` prototype is mandatory** (ADR-0008 note,
  ADR-0010 note, fail-open G1). HWND/`Gp*` handles truncate on Win64 — a review gate + the T2
  test, **not** fail-open-able.

## Accepted gaps (carried, not introduced)

- **Contract-level, from macOS** (ADR-0007 §2): no distinct `ERROR` beat (settles to `IDLE`); no
  HUD during model load (appears at `READY`). These are deliberate deferrals, identical on both
  platforms.
- **Only if #66 fails** (ADR-0010 §1): flattened card-vs-bar contrast + 1-bit aliased corners
  under Option A. Not accepted unless the spike forces it.

## Out of scope (from the map)

Linux HUD (a separate, later map); changing the audio-level data path; redesigning the HUD
visuals. See [map #39](https://github.com/Hanimn/seda/issues/39) "Out of scope."
