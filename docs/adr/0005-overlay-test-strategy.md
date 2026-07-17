# ADR-0005 — Test strategy for the macOS-only GUI overlay

- **Status:** Accepted
- **Date:** 2026-07-17
- **Deciders:** grilling session (Hani Momeninia + agent)
- **Issue:** [#22 — Test strategy for the macOS-only GUI overlay](https://github.com/Hanimn/seda/issues/22)
- **Epic:** [#15 — live recording waveform overlay (macOS)](https://github.com/Hanimn/seda/issues/15)
- **Gathers test obligations from:** [ADR-0001](0001-gui-host-owns-main-thread.md),
  [ADR-0002](0002-recorder-latest-level-handoff.md),
  [ADR-0003](0003-notifier-seam-overlay-lifecycle.md),
  [ADR-0004](0004-overlay-config-and-gating.md)
- **Informed by:** [#17 research](https://github.com/Hanimn/seda/issues/17)

## Context

The overlay is macOS + AppKit code that CI (headless Linux) can't render. The repo already
has the pattern for testing native-dependent code without the native layer:
`TestDarwinIntercept` (`tests/unit/test_hotkeys.py:288`) monkeypatches **module-level native
functions** (`_darwin_event_identity`, `_darwin_chord_modifiers_held`) and, elsewhere, injects
a **fake native module** via `monkeypatch.setitem(sys.modules, "pynput", ...)`
(`test_hotkeys.py:343`). The `integration` marker exists (`pyproject.toml:127`), deselected in
CI with `-m 'not integration'`, and `@pytest.mark.skipif` layers on platform/dep gates.

This ADR gathers the test obligations that the four prior overlay ADRs each named, and fixes
the unit/integration/manual boundary.

## Decision

Test the overlay in **three tiers**: shim-level unit tests (the bulk, AppKit-free), a thin
integration smoke test (real `NSApplication`, macOS-only, deselected in CI), and a manual
on-device checklist for what automation can't judge.

### 1. Isolate every AppKit call behind a module-level shim

Every AppKit touch sits behind a small named module-level function — `_make_panel`,
`_show_panel`, `_hide_panel`, `_dispatch_main`, etc. — exactly as
`local_flow.input.hotkeys._darwin_event_identity` does. Unit tests
`monkeypatch.setattr("local_flow.<overlay module>._show_panel", fake)`; **CI never imports
AppKit.** This is the load-bearing testability decision — it makes tiers 2/3 the only place
real AppKit runs.

### 2. Unit tier (AppKit-free) — what it asserts, per ADR

The unit tests stub the shims and assert the *logic around* AppKit:

- **ADR-0002 (level hand-off):** `latest_level` is `0.0` before `start()` and after
  `cancel()`; reflects a synthetic block's RMS during recording; a monkeypatched `_rms` that
  raises never breaks `stop()` (compute-swallowed).
- **ADR-0003 (notifier seam):** `FanOutNotifier` forwards `notify()` to every notifier and
  **swallows** a raising one (a fake that raises doesn't stop the others); `OverlayNotifier`
  maps `RECORDING → _show_panel`, `{CANCELLED, SUCCESS, ERROR} → _hide_panel`, ignores
  `READY`/`BUSY`/`TRANSCRIBING`; **idempotent** double-show / double-hide are no-ops; show/hide
  are routed through a **fake `_dispatch_main`** (assert it was invoked — the listener/worker →
  main marshalling), so no real thread hop is needed.
- **ADR-0004 (config + gating):** `select_overlay_enabled` truth table — `--no-overlay` wins;
  explicit config `true`/`false` wins on every platform; `None` → macOS-on / others-off (with
  an **injected `platform`**, per the `select_push_to_talk` idiom); and a simulated AppKit
  `ImportError` yields a **console-only fan-out** with dictation intact.
- **ADR-0001 (inversion):** the signal handler calls `shutdown()` then stops the AppKit owner
  (assert ordering with fakes); the `cli.run()` `try/except` falls back to the blocking
  `run()` on a simulated host-build failure; **non-macOS never constructs a host**. The
  off-main execution of `run()` is exercised via the existing `in_main_thread` branch
  (`app.py:140`), which tests already hit.

**Fail-open tests follow the existing precedent** — `test_intercept_passes_event_when_identity_raises`
(`test_hotkeys.py:323`): monkeypatch a native shim to raise, assert the safe degradation
(dictation continues, console-only). Every overlay failure mode gets one such test.

### 3. Integration tier (real `NSApplication`, macOS-only, deselected in CI)

Any test that spins a **real** `NSApplication`/`NSPanel` is
`@pytest.mark.integration` + `@pytest.mark.skipif(sys.platform != "darwin", ...)`. CI runs
`-m 'not integration'` (existing convention), so these run only on a developer's Mac. Scope is
a **thin smoke test**: the panel constructs and `show()`/`hide()` run **without raising**
against real AppKit — proving the #17 recipe's selectors/constants are spelled correctly (one
of #17's `[uncertain]` items). It does *not* assert visual/behavioral properties (those are
tier 3).

### 4. Manual on-device tier (§39-style checklist)

The properties automation can't judge become a manual checklist (the repo's §39 verification
style), run on a Mac before the overlay ships:

- No focus theft — dictating into another app, the app keeps its cursor/key focus while the
  HUD is up (#17's decisive property).
- Floats over everything incl. a **full-screen** app; appears on all Spaces.
- Bars animate with voice; bottom-center placement; vanishes on release.
- The **#17 `[uncertain]` items** live here: window level actually covers full-screen
  (`NSStatusWindowLevel` + `fullScreenAuxiliary` vs `NSScreenSaverWindowLevel`);
  `CADisplayLink` availability vs `NSTimer` baseline; `Accessory` policy shows **no Dock-icon
  flash** under a plain `python -m local_flow` launch.

## Consequences

**Positive**

- The bulk of the overlay is unit-tested with **zero AppKit in CI** — reuses the exact
  `TestDarwinIntercept` shim pattern, so it fits the codebase without new infrastructure.
- Each prior ADR's named obligation has a concrete home; nothing is orphaned.
- The unit/integration/manual split matches the repo's existing markers and §39 style.

**Negative / costs accepted**

- The AppKit **construction recipe** (correct selectors/args: `initWithContentRect_...`,
  `setLevel_`, `orderFrontRegardless`) is *not* asserted in CI — only that the shims are
  called. It's verified by the tier-2 smoke test + tier-3 checklist on-device.
  - **Rejected — inject a fake AppKit module** (`monkeypatch.setitem(sys.modules, "AppKit",
    MagicMock())`) to run construction against a mock in CI. It would assert selectors/args,
    but couples the tests to a #17 API surface a `/prototype` may still change, and mocks can't
    validate that the real constants exist or that the panel behaves — the smoke test does that
    for real. Shim-level keeps CI honest about what it can actually prove.
  - **Rejected — manual-only for AppKit-real** (no integration tests at all): loses the cheap
    "constructs without raising" regression signal that catches a mistyped selector before a
    human ever opens the app.

**Follow-ups (implementation)**

- Name the overlay module and its shim functions so the `monkeypatch.setattr` targets are
  stable (implementation).
- Add the manual §39 overlay checklist to the release verification doc when the overlay lands.

## Open questions (for implementation)

- Whether a fake `_dispatch_main` in unit tests should run its callable **synchronously**
  (simplest — assert the effect) or record-only (assert it was scheduled). Leaning synchronous
  for effect-assertions, with one explicit test that scheduling happened. `[uncertain]`
- Whether the tier-2 smoke test can run **headless** on a CI Mac runner (no window server) or
  must be gated to an interactive session — a #17-adjacent unknown to settle when a Mac runner
  exists. `[uncertain]`
