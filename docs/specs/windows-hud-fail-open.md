# Spec: Windows overlay — fail-open story

**Status:** ready to implement (specs against the ADR-0009 seam, PR [#65](https://github.com/Hanimn/seda/pull/65)) · **Map:** [Windows recording HUD](https://github.com/Hanimn/seda/issues/39) · **Ticket:** [#64](https://github.com/Hanimn/seda/issues/64)
**Verification:** unit (Linux CI, Win32-free) + integration (`win32`-only, deselected) + by-eye exit-path runs, mirroring [ADR-0005](../adr/0005-overlay-test-strategy.md).
**Companion:** [`windows-hud-parity.md`](windows-hud-parity.md) (the visual/behavioral spec) — this doc is its fail-open half.

The exhaustive **fail-open catalog** for the Windows overlay: every Win32/GDI+ failure mode, the
layer that catches it, and the degradation it must produce — so that dictation is **never
blocked, delayed, or altered** by the overlay being unsupported, failing to build, or breaking at
runtime. The outcome is always *today's exact terminal behavior* (`controller.run()`), overlay
absent or silently degraded. This discharges the concern [ADR-0008 §5](../adr/0008-windows-gui-host-threading-model.md)
named ("a missing or broken Win32 overlay must degrade to today's exact terminal behavior").

> **Plan-dependency.** `gui/_hostloop.run_hosted` and [ADR-0009](../adr/0009-overlay-host-boundary.md)
> are not on `main` yet (PR #65). This spec is written against that proposed seam; the boundary
> rule in §3 is the acceptance contract for #65 on the Windows side. Today's macOS host
> (`src/seda/gui/host.py`) is the parity template throughout.

## 1. The invariant and the two-layer model

The overlay is a **strictly-additive companion**; every failure has a defined degradation and
none reach the recording/transcription path. Enforced at two layers:

- **Layer 1 — the build/gate boundary** (`gui/_hostloop.run_hosted` + `cli.run`'s `_HOST_MODULES` switch, ADR-0009). An unsupported platform, an un-importable host module, or a failed *acquire+build* returns `False`, and `cli.run` cleanly falls back to `controller.run()` — **because the controller has not started** (a clean retry).
- **Layer 2 — the runtime seam** (`OverlayNotifier` / `FanOutNotifier`, [ADR-0003](../adr/0003-notifier-seam-overlay-lifecycle.md), platform-neutral, unchanged). A broken overlay *after* the controller is running is swallowed-and-logged to "no HUD / degraded HUD, dictation intact."

The hard edge between them (§3): once `controller.start()` is called, a failure is the
**controller's own** and must **propagate** — re-running `start()` would only fail again.

## 2. The failure-mode catalog

Layer legend: **L1-build** = inside `run_hosted`'s fail-open try (`→ False → terminal`, clean
retry); **L1-setup** = post-build / pre-`start()` (still clean-retry, §3); **L1-post** = at/after
`controller.start()` (**PROPAGATE**); **L2** = runtime seam (swallow+log, dictation intact);
**REVIEW+T2** = not fail-open-able, caught by code review + a `win32` integration test (§4).

| # | Failure mode | Layer | Degradation | macOS analogue |
|---|---|---|---|---|
| **A — Gate** ||||
| A1 | `supports()` false on non-Windows | L1-build (gate) | `False` → terminal | `returns_false_on_non_macos` |
| A2 | `_HOST_MODULES` has no entry for `plat` | L1 (`cli.run`) | terminal; host never imported | Windows-new (ADR-0009 §3) |
| **B — Host-module import** ||||
| B1 | `import seda.gui.host_win` raises `ImportError` | L1 (`cli.run` `except ImportError`) | terminal; log "overlay host unavailable" | `returns_false_when_appkit_module_missing` + cli guard |
| B2 | `build_overlay` loads `windll.user32/gdi32/gdiplus`, a symbol missing | L1-build | `False` → terminal | `returns_false_when_build_raises_importerror` |
| **C — Win32 build steps** (all inside `build_overlay` → L1-build → `False`) ||||
| C0 | **Partial construction:** step *N* ok, *N+1* raises → any allocated GDI+ token / class atom / HWND / DIB unwound before the exception leaves `build_overlay` | L1-build + **build's own reverse-order dispose** (§3) | `False` → terminal **and zero leaked resources** | **Windows-new** (macOS build is one GC-backed alloc) |
| C1 | `SetProcessDpiAwarenessContext` **hard** raise | L1-build | `False` → terminal | `never_raises_into_the_caller` |
| C1b | `SetProcessDpiAwarenessContext` **benign** failure (already-set / down-level absent) | L1-build (swallowed, build continues) | swallow; HUD may be at wrong DPI (better than none) | `warm_accessibility_trust_swallows_failure` |
| C2 | `GdiplusStartup` non-`Ok` / raises | L1-build | `False` → terminal | `never_raises_into_the_caller` |
| C3 | `RegisterClassExW` returns 0 | L1-build | `False` → terminal | `never_raises_into_the_caller` |
| C4 | `CreateWindowExW` returns `NULL` | L1-build | `False` → terminal | `never_raises_into_the_caller` |
| C5 | `CreateDIBSection` returns `NULL` (incl. degenerate/zero dimensions) | L1-build | `False` → terminal | Windows-new (Option B DIB) |
| C6 | `GdipCreateFromHDC` fails on the DIB DC | L1-build | `False` → terminal | Windows-new |
| C7 | First `UpdateLayeredWindow` blit fails | L1-build | `False` → terminal | Windows-new (runtime twin → E1) |
| C8 | Any other unexpected exception building the host | L1-build (`except Exception`) | `False` → terminal, **never raises** | `never_raises_into_the_caller` |
| **D — Boundary + past it** ||||
| D0 | **Post-build / pre-`start()` setup** fails: `register_overlay`, `signal.signal`, the **initial** `SetTimer` arm | **L1-setup** (fail-open — §3) | `False` → terminal; nothing dispatched yet | Windows-new (macOS orders these in the run-loop) |
| D1 | `controller.start()` raises (backend load) | **L1-post** | **PROPAGATE**; pump not reached; **no fall-back** | `controller_start_failure_propagates_and_does_not_fall_back` |
| D2 | The pump loop raises after start | **L1-post** | **PROPAGATE**; teardown runs in `finally` (F3) | `teardown_runs_when_run_loop_raises` |
| **E — Runtime seam** (L2: swallow+log, dictation intact) ||||
| E1 | `WM_TIMER` → per-frame blit / GDI+ draw raises | L2 (guarded paint path) | swallow; pump survives; dictation intact | Windows-new |
| E2 | `SetTimer` fails when **re-arming** on a runtime `set_mode` | L2 | swallow; static/last-frame HUD; never propagate | Windows-new |
| E3 | A dequeued `dispatch_main` closure raises during pump drain | L2 (drain guards each closure; `OverlayNotifier._request` also wraps) | swallow; drain continues | ADR-0003 `dispatch_main` swallow |
| E4 | `SetWindowPos` panel-shrink fails inside `set_mode` | L2 | swallow; HUD stays old size | Windows-new (macOS has no resize) |
| E4b | The resize's `CreateDIBSection`+`GdipCreateFromHDC` rebuild fails (token valid, per-frame object fails) | L2 (guarded in `set_mode`) | swallow; **keep old backbuffer**; never propagate | Windows-new (C6's runtime twin) |
| E5 | `OverlayNotifier` `show`/`hide`/`set_mode` raises at runtime | L2 (`_request` + `FanOutNotifier` swallow) | no-HUD / degraded-HUD, dictation intact | ADR-0003 exception discipline |
| E6 | **Pump dead, producers live:** listener/worker keep `notify()`-ing onto a queue nobody drains | L2 (**bounded queue**) | `queue.Queue(maxsize=256)` + `put_nowait`; **drop-newest-and-log on `Full`, never block the producer** | **Windows-new** (macOS run loop can't die under live producers) |
| E7 | Runtime monitor-geometry query fails / degenerate (RDP, headless, hotplug) | L2 | swallow; keep last position/size | Windows-new |
| **F — Teardown** (in `run_loop`'s `finally`, ADR-0008/0009) ||||
| F1 | A single teardown step raises (`KillTimer`/`DestroyWindow`/`UnregisterClassW`/free-DIB/`GdiplusShutdown`) | Teardown guard (each step wrapped, ordered) | step swallowed+logged; teardown continues; exit proceeds | `teardown_runs_on_normal_exit` |
| F1b | **Ordering cascade:** `DestroyWindow` swallowed → `UnregisterClassW` then fails (window still exists) → class atom leaks | Teardown guard + **register-or-reuse on relaunch** | swallow both; leaked atom is harmless because `RegisterClassExW` tolerates already-registered on the next build (self-healing) | Windows-new |
| F2 | Teardown runs when `controller.start()` raised (D1) | Teardown in `finally` | teardown runs; original error still propagates | `teardown_runs_when_controller_start_raises` |
| F3 | Teardown runs when the pump raised (D2) | Teardown in `finally` | teardown runs; run-loop error propagates | `teardown_runs_when_run_loop_raises` |
| F4 | A raising teardown must not mask the real crash | Teardown guard | swallow the teardown error; **original** exception surfaces | `broken_teardown_does_not_mask_the_original_error` |
| F5 | Ctrl-C / SIGTERM sets `stop_requested`; pump breaks → `shutdown()` → `finally` teardown | L1-post stop path + teardown | clean stop, HUD gone (parity with #37/#38) | `signal_pump_shuts_down_and_stops_the_loop` |
| F6 | Pump is a no-op until the stop flag is set | L1-post | controller keeps running; no early teardown | `pump_ignores_when_no_stop_requested` |
| **G — Latent corruption** (NOT fail-open-able → REVIEW + T2, §4) ||||
| G1 | Missing `restype`/`argtypes` truncates a 64-bit HWND on Win64 (ADR-0008 lesson) — corrupted handle may succeed then mis-address / crash later | **REVIEW+T2** | mandatory declared prototypes (review gate) + a `win32` test building a real window + `show`/`set_mode`/`teardown` | Windows-new (no ctypes on macOS) |
| G2 | `WS_EX_LAYERED`/`WS_EX_TRANSPARENT` **silently not applied** — window renders opaque / eats clicks; raises nowhere | **REVIEW+T2** | T2 asserts `GetWindowLongPtrW(GWL_EXSTYLE)` carries both flags and the window is click-through | Windows-new |

## 3. The propagate-vs-fail-open boundary (crisp rule)

> **The boundary line is `controller.start()` — not `build_overlay`'s return.**
>
> - **Before `controller.start()`** — the whole of `build_overlay` (A1/B2/C0–C8) *and* the post-build setup `register_overlay` + `signal.signal` + the **initial** `SetTimer` (D0) — the controller has not started, so a failure **fails open** (`run_hosted` returns `False`; `cli.run` runs `controller.run()`). A *clean retry*, exactly as macOS keeps `NSApplication.sharedApplication()` + `start()` out of the acquire path. The fail-open try extends through the last step before `start()`. **[Confirmed — HITL, 2026-07-30.]**
> - **At and after `controller.start()`** (D1) and its pump/run-loop (D2) — the failure is the **controller's own**; it **PROPAGATES**. Falling back would re-invoke `start()` and fail identically. The `finally` teardown still runs (F2/F3); a raising teardown must not mask the real error (F4).

**Partial-construction cleanup rule (C0), mirroring teardown's `finally`.** The Windows build is
seven sequential imperative allocations (`GdiplusStartup` → `SetProcessDpiAwarenessContext`
best-effort → `RegisterClassExW` → `CreateWindowExW` → `CreateDIBSection` → `GdipCreateFromHDC` →
first `UpdateLayeredWindow`). `build_overlay` must be **transactional**: its own `try/except`, on
*any* failure, disposes what it has allocated so far **in reverse order** and then re-raises into
`run_hosted`. `run_hosted`'s `except` cannot do this — the partial overlay was never returned, so
the boundary holds no handles. A failed build leaks **nothing** (no GDI+ token, class atom, HWND,
or DIB) — the build-time twin of teardown's `finally` unwind, and why a failed launch retries
cleanly (dovetails with F1b's register-or-reuse).

## 4. The ctypes HWND-truncation risk — handled honestly (G1)

A truncated HWND is **not fail-open-able**, and this spec does not pretend otherwise. Declared
`restype`/`argtypes` on **every** `ctypes` prototype is a **code-review gate**, backstopped by the
T2 `win32` integration test — **not** a runtime `try/except`. The fail-open machinery can only
degrade *exceptions observed at a known seam*; a truncation is neither observed nor localized (the
corrupted handle may `CreateWindowExW`-succeed then mis-address later calls, crashing on the pump
thread or at a teardown deref). A T1 test asserting "a truncation degrades cleanly to `False`"
would encode a **false safety guarantee**. Disposition (straight from ADR-0008's implementation
note — declared prototypes **mandatory** on Win64): mandatory prototypes (review) + the T2 test
that constructs the real layered window and exercises `show`/`set_mode`/`teardown` without
raising. **We explicitly do not claim D2/F3 backstop a truncation** — teardown itself
dereferences the handle, so it can compound the corruption. G2 is the same class of latent,
seam-invisible corruption and shares this disposition.

## 5. Test-parity checklist (ADR-0005)

**Windows T1 mirrors `tests/unit/test_gui_host.py`** (AppKit fakes → Win32-shim fakes; the
fail-open *contract* is identical by construction because `run_hosted` is written once — ADR-0009):
`test_returns_false_on_non_windows` (A1), `…_when_build_raises_importerror` (B2),
`test_cli_host_win_import_error_runs_terminal` (B1), `test_never_raises_into_the_caller` (C8),
`test_success_runs_the_loop_starts_controller_and_registers_overlay`,
`…_start_failure_propagates_and_does_not_fall_back` (D1),
`test_teardown_runs_on_normal_exit` (F1), `…_when_controller_start_raises` (F2),
`…_when_run_loop_raises` / `test_pump_loop_failure_propagates_and_tears_down` (F3/D2),
`test_broken_teardown_does_not_mask_the_original_error` (F4),
`test_stop_flag_breaks_pump_shuts_down_and_tears_down` (F5),
`test_pump_ignores_when_no_stop_requested` (F6).

**Windows-new T1:** `test_cli_unknown_platform_runs_controller_directly` (A2);
`test_build_unwinds_*` ×3 (C0); `test_returns_false_when_dpi_awareness_raises` (C1) +
`test_dpi_awareness_benign_failure_still_builds` (C1b); per-call build-failure tests
`test_returns_false_when_{gdiplus_startup,register_class,create_window,create_dibsection,gdip_create_from_hdc,first_updatelayeredwindow}_fails`
(C2–C7); `test_post_build_pre_start_setup_failure_falls_back` (D0);
`test_wm_timer_draw_failure_is_swallowed_pump_survives` (E1);
`test_set_timer_failure_is_swallowed_dictation_intact` (E2);
`test_dispatch_queue_drain_swallows_a_raising_closure` (E3);
`test_set_window_pos_failure_is_swallowed` (E4);
`test_set_mode_backbuffer_rebuild_failure_keeps_old_hud` (E4b);
`test_dispatch_queue_drops_when_pump_not_draining_never_blocks_producer` (E6);
`test_runtime_monitor_geometry_failure_keeps_last_position` (E7);
`test_each_teardown_step_failure_is_isolated` + `test_teardown_destroywindow_failure_still_attempts_unregister`
+ `test_register_class_tolerates_already_registered` (F1/F1b). Plus the **conformance test**
(ADR-0009): the Windows `Overlay`'s four callables are **callable-and-effectful**, not
`inspect.signature`-shaped.

**T2 (Windows-only, `@pytest.mark.integration` + `skipif(sys.platform != "win32")`):**
`test_win_ctypes_prototypes_declared` (G1) and `test_win_layered_transparent_style_applied` (G2)
— the two latent-corruption modes no T1 can catch.

## 6. Settled sub-decisions

Ratified in the #64 HITL grilling (2026-07-30):

- **D0 boundary → fail open.** Post-build/pre-`start()` setup sits inside the clean-retry zone; #65's `run_hosted` must arm the teardown `finally` such that these steps fail open, not propagate.
- **E6 queue → bounded, drop-and-log.** `queue.Queue(maxsize=256)` + `put_nowait`; on `Full`, drop the frame and log, never block the producer. `256` is a tune-by-eye bound (ample for fire-and-forget mode changes), not a design point.
- **F1b class atom → register-or-reuse (self-healing).** `RegisterClassExW` tolerates an already-registered class on relaunch; a leaked atom from a swallowed-`DestroyWindow` cascade is harmless residue, and the HUD still comes up on a same-process retry.
- **C1b DPI benign allow-list (implementation default).** `E_ACCESSDENIED` ("already set") and an absent entry point (down-level Windows) count as benign → swallow-and-continue; **every other** result is a hard failure → `False` → terminal. The implement pass confirms the exact HRESULT list; the *shape* (a narrow allow-list, not a blanket swallow) is fixed here so the guard can't mask a genuine failure.
