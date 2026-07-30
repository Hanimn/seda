# ADR-0009 — Overlay host boundary: sibling hosts over a shared lifecycle helper

- **Status:** Accepted
- **Date:** 2026-07-30
- **Deciders:** wayfinder grilling session (Hani Momeninia + agent)
- **Issue:** [#62 — Decision: shared-vs-duplicated overlay host boundary (macOS + Windows)](https://github.com/Hanimn/seda/issues/62)
- **Map:** [#39 — Map: Windows recording HUD (cross-platform overlay)](https://github.com/Hanimn/seda/issues/39)
- **Resolves:** the boundary [ADR-0008 §5](0008-windows-gui-host-threading-model.md) explicitly deferred ("Explicitly out of scope — the shared-vs-duplicated host boundary … is the **next** #39 ticket — the one this ADR unblocks").
- **Builds on:** [ADR-0001](0001-gui-host-owns-main-thread.md) (macOS main-thread inversion), [ADR-0008](0008-windows-gui-host-threading-model.md) (Windows threading model), [ADR-0003](0003-notifier-seam-overlay-lifecycle.md) (the `Notifier`/`OverlayNotifier` seam), [ADR-0005](0005-overlay-test-strategy.md) (fake-`Overlay` test injection).

## Context

The recording HUD ships on macOS (`src/seda/gui/host.py`, AppKit via PyObjC — ADR-0001) and is
being brought to Windows (`gui/host_win.py`, raw Win32 via stdlib `ctypes` — ADR-0008). The two
hosts share a *shape*:

> host owns the main thread → `register_overlay(overlay)` → `controller.start()` →
> block/pump the GUI loop → on stop, `controller.shutdown()` → `finally:` dispose the window.

But their loop **mechanics are genuinely incompatible**. macOS blocks opaquely in
`NSApplication.run()`, serviced by a separate ~10 Hz `NSTimer` signal pump (ADR-0001). Windows
*is* its own interruptible `PeekMessageW(..., PM_REMOVE)` + ~5 ms-sleep poll loop with a bare
stop flag — ADR-0008 §2 explicitly **rejected** blocking `GetMessage` because it swallows SIGINT.
There is no shared `run()` body to write: the pump *is* the servicing loop on Windows, and an
opaque kernel block on macOS.

Today's real seam already anticipates a boundary decision. `run_with_overlay` in `host.py` is:

```python
def run_with_overlay(
    controller,
    *,
    build: Callable[[Callable[[], float]], Overlay] | None = None,   # injectable (tests / per-platform)
    register_overlay: Callable[[Overlay], None] | None = None,       # cli.run wires the notifier fan-out
    platform: str | None = None,                                     # injectable (defaults to sys.platform)
) -> bool:                                                           # False == fail open → controller.run()
```

with a fail-open `try/except` around **acquire-AppKit + build-panel only** (a raise there returns
`False` for a clean retry, because the controller has not started yet), and `controller.start()`
deliberately *outside* the try. `cli.run()` already wires this platform-agnostically: it imports
`Overlay, run_with_overlay` behind a `try/except`, defines a `_register(overlay)` that builds an
`OverlayNotifier(show, hide, set_mode, dispatch_main)` and adds it to the `FanOutNotifier`, and
falls through to the blocking `controller.run()` on `False` or `ImportError`.

The question this ADR settles: is the host a **shared platform-neutral `OverlayHost`/`Overlay`
interface** (a `HostBackend` Protocol/ABC with an AppKit backend and a Win32 backend behind one
entry — "Candidate A"), or **independent sibling hosts** sharing only the `Notifier` seam
("Candidate B")?

**Constraints carried from the epic (all non-negotiable):**

1. **Fail-open** to today's exact terminal behavior must be preserved **identically** on both
   platforms (epic #15; the #37/#38 no-lingering-HUD fix).
2. **Zero new dependencies** — AppKit is a macOS-only optional import; Win32 is stdlib `ctypes`.
3. **Both platforms stay fakeable** via the `build=` / `platform=` module-level-shim injection
   ADR-0005 §1 fixed as the load-bearing testability decision.
4. **Linux is out of scope** for this effort — a separate, later map (#39 "Out of scope").

## Decision

Adopt **sibling hosts** (Candidate B) as the module layout — `gui/host.py` (macOS, unchanged)
and a new `gui/host_win.py` (Windows), each owning its own `Overlay` struct, `build_overlay`,
GUI loop, and teardown ordering. But extract the **fail-open + lifecycle skeleton** into one
shared `gui/_hostloop.py` helper both hosts call, so the invariant control flow is written
**once**, not copy-pasted. Loop *mechanics* stay per-platform.

This is B's small-diff, YAGNI-correct layout, plus the one concession that survived adversarial
review from both sides: the lifecycle skeleton (register-before-start, start-outside-the-try,
teardown-in-`finally`-on-every-exit) is a **real shared contract** and the home of the #37/#38
lingering-HUD bug — it must not be duplicated per platform.

Four sub-decisions.

### 1. Sibling hosts, not a shared `HostBackend` interface

Each platform host is a self-contained sibling module. They coordinate through **two named
seams** and nothing else:

- **Seam #1 — the capability contract (already exists, unchanged):**
  `OverlayNotifier(show, hide, set_mode, dispatch_main)` in `notifications/__init__.py`. These
  four callables are the canonical host→notifier contract. Changing this signature is an
  ADR-level change touching every host.
- **Seam #2 — the lifecycle helper (NEW, small):** `gui/_hostloop.run_hosted(...)` owning the
  gate → build-in-try → `False` → register → start → loop → shutdown → `finally: teardown` flow
  (§2).

**Decisive reason — a shared `run()` would unify only the word.** The AppKit block and the Win32
poll share no body (ADR-0001 vs ADR-0008 §2). A `HostBackend.run()` over them is a shallow module:
a wide interface wrapping ~450 lines of AppKit and a comparable Win32 body that stay fully
separate. B lets each host stay idiomatic to its OS.

### 2. The shared lifecycle helper `gui/_hostloop.run_hosted`

```python
def run_hosted(
    controller,
    *,
    supports: Callable[[str], bool],                    # platform gate (per-host)
    build: Callable[[Callable[[], float]], Overlay],    # toolkit construction (per-host)
    run_loop: Callable[[AppController, Overlay, Callable[[Overlay], None] | None], None],
    register_overlay: Callable[[Overlay], None] | None = None,
    platform: str | None = None,
) -> bool:
    plat = platform if platform is not None else sys.platform
    if not supports(plat):
        return False                                    # fail open: unsupported platform
    try:
        overlay = build(lambda: controller.latest_level)
    except (ImportError, OSError):
        logger.info("overlay unavailable, falling back to terminal mode")
        return False                                    # fail open: acquire/build failed (clean retry)
    except Exception:                                   # noqa: BLE001
        logger.warning("overlay setup failed, falling back to terminal mode", exc_info=True)
        return False
    # Past the boundary the host OWNS the run — a raise here PROPAGATES (matches today's
    # run_with_overlay: a controller.start() failure must not fall back and re-run start()).
    run_loop(controller, overlay, register_overlay)
    return True
```

`run_loop` — the per-platform body — receives the built overlay and **must**:
`register_overlay(overlay)` → `controller.start()` → block/pump its GUI loop → on stop
`controller.shutdown()` → **`finally:` `overlay.teardown()`**. The helper guarantees the
fail-open boundary and the `-> bool` contract; each host guarantees its own loop and teardown
ordering.

`run_with_overlay` (macOS) becomes a thin adapter that calls `run_hosted` with `supports=` (is
`darwin` + AppKit importable), the existing `build_overlay`, and a `run_loop` wrapping today's
`_run_appkit_host`. Its public signature and fail-open behavior are **preserved**, so `cli.run`
and every existing ADR-0005 test are unchanged. `host.py`'s AppKit body is **not touched** —
critically, the `NSApplication.sharedApplication()` handle stays inside the macOS `run_loop`,
so the safety-critical, hardware-validated `_run_appkit_host` never has its signature disturbed.

### 3. The platform switch and fail-open seam live in `cli.run()`

`cli.run()` selects the host module by platform and keeps its `_register` wiring and terminal
fallback byte-for-byte:

```python
_HOST_MODULES = {"darwin": "seda.gui.host", "win32": "seda.gui.host_win"}

def _select_host_module(plat: str) -> str | None:
    return _HOST_MODULES.get(plat)
```

Unknown platform → `None` → terminal path. An optional-host `ImportError` (e.g. a broken PyObjC)
→ terminal path, exactly as today. The `-> bool` return from `run_hosted` is the single
fail-open signal `cli.run()` keys on — **identical on both platforms because it is produced by
one shared function.** A second, runtime fail-open layer already exists and is untouched:
`OverlayNotifier` swallows every `show`/`hide`/`set_mode`/`dispatch_main` failure so a broken
overlay degrades to "no HUD, dictation intact" (ADR-0003).

### 4. The `Overlay` struct is duplicated per host, on purpose

Each host defines its own `Overlay` — the private fields genuinely differ
(`_panel`/`_view`/`_timer_holder` on macOS; `_hwnd`/`_timer_id`/`_wndproc_ref` on Windows, the
last a GC-keepalive hazard the Win32 `ctypes` recipe requires). Two honest structs beat one
`Any`-polluted shared dataclass. What is *not* duplicated is the **shape** the `OverlayNotifier`
constructor pins (§1, seam #1) — that four-callable contract is the only cross-host promise the
`Overlay` must satisfy.

## Consequences

**Positive**

- Minimal diff from shipped code: one new host file (`gui/host_win.py`), one small helper
  (`gui/_hostloop.py`), one dict in `cli.run()`. `host.py`'s AppKit body is untouched, and its
  public `run_with_overlay` keeps its signature (thin adapter over `run_hosted`).
- The fail-open boundary and the teardown-in-`finally` ordering are written **once** in
  `_hostloop` and exercised by every host's tests — directly defending the #37/#38 lingering-HUD
  regression rather than trusting per-platform discipline.
- ADR-0005 testing is unchanged: `build=` / `platform=` injection works per host, the macOS test
  file is the template the Windows suite copies, and `OverlayNotifier` tests stay 100%
  platform-free. **No new fake is introduced** (Candidate A would have needed a `FakeBackend`
  on top of the still-needed fake `Overlay`).
- Fail-open preserved identically: any unsupported platform / broken acquire → `False` → the
  process is literally today's terminal code path.

**Negative / costs accepted**

- **No compiler-enforced capability conformance across hosts** — nothing type-errors a Windows
  host that forgets to implement `set_mode`. Mitigated by: a **parametrized conformance test**
  over both host modules, a structural `inspect.signature` check against the `OverlayNotifier`
  constructor, and the rule that the constructor signature *is* the contract (§1).
- `_register` in `cli.run()` grows if the `OverlayNotifier` capability set grows — accepted
  while the set is small and stable.
- One new concept (`gui/_hostloop.py`) over pure-B. Accepted deliberately: the invariant it owns
  is exactly where the lingering-HUD bug lives, and the Notifier seam does not encode it.

**Revisit trigger (binding).** Promote to a shared `HostBackend` / `select_backend()` interface
(Candidate A, kept as the documented escape hatch) if **either**:

1. a **third host is *designed*** (not merely landed) — notably Linux, whose no-single-toolkit
   reality (Wayland/X11 × GTK/Qt/raw layer-shell) may force per-platform backend *sub-selection*
   a flat `_HOST_MODULES` string map cannot express; **or**
2. the `OverlayNotifier` capability set changes **more than once** (e.g. adding `set_progress`).

Firing on *design* rather than *landing* is deliberate: by the time a third host has landed as a
copy, the throwaway cost is already paid.

## Rejected alternatives

**Candidate A — shared `HostBackend` Protocol + `backends/` package.** Fairly stated, A has the
best answer to drift (a Protocol type-errors a missing capability at the Win32 backend) and
centralizes fail-open in one `try/except`. It lost on three concrete facts, not on principle:

- The shared body it buys is thin — ~30 lines of fail-open guard — wrapping two fully separate
  loop bodies (a shallow module).
- Its `acquire(level_source) -> Overlay` split has **nowhere to carry macOS's live
  `NSApplication` handle**, forcing a signature change to `_run_appkit_host` — the exact
  safety-critical, hardware-validated (#41/#37/#38) code A promised to move "verbatim." That
  "verbatim move" is false.
- A Protocol types method **presence**, not lifecycle **ordering** — and ordering
  (`finally: teardown`) is where the real regression risk lives. A pays interface + package +
  registry + fake-maintenance cost without defending the bug that matters.

Its one durable benefit (conformance under platform/capability growth) is a benefit this effort
has ruled out (Linux out of scope). Kept as the condition-gated escape hatch above.

**Pure Candidate B — the `Notifier` seam as the *only* shared contract.** Rejected because both
adversarial reviews independently showed the hosts share an **unwritten** lifecycle contract
(register-before-start, start-outside-the-try, teardown-in-`finally`) that the Notifier seam does
not encode. Leaving it implicit invites per-platform re-introduction of the #37/#38 lingering-HUD
bug. Hence the `_hostloop` helper (§2).

## Notes for implementation (non-binding)

- The macOS `run_with_overlay` refactor to a `run_hosted` adapter must be **behavior-preserving**
  — its existing ADR-0005 unit tests are the regression net; they should pass unchanged.
- The parametrized conformance test should assert, for each entry in `_HOST_MODULES`, that the
  module exposes the `run_with_overlay`-shaped entry and that its built `Overlay` satisfies the
  `OverlayNotifier` four-callable contract — the structural stand-in for A's compiler check.
- `gui/host_win.py` lifts the pump/signal/teardown structure ADR-0008 already fixed (and the #41
  prototype validated) into its `run_loop`; `_hostloop` owns only the gate + fail-open + `-> bool`.
