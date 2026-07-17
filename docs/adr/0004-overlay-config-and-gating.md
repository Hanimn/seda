# ADR-0004 — Overlay config, `--no-overlay`, and fail-open gating

- **Status:** Accepted
- **Date:** 2026-07-17
- **Deciders:** grilling session (Hani Momeninia + agent)
- **Issue:** [#21 — Overlay config, `--no-overlay` flag, and fail-open gating](https://github.com/Hanimn/seda/issues/21)
- **Epic:** [#15 — live recording waveform overlay (macOS)](https://github.com/Hanimn/seda/issues/15)
- **Depends on:** [ADR-0001](0001-gui-host-owns-main-thread.md) (#16 — `try/except` in `cli.run()`), [ADR-0003](0003-notifier-seam-overlay-lifecycle.md) (#20 — fan-out), [#18](https://github.com/Hanimn/seda/issues/18) (PyObjC dep)

## Context

The overlay needs a config + CLI surface and a precise fail-open gating rule. The repo has
established idioms the ticket points at directly:

- **`_Section` base** with `model_config = ConfigDict(extra="forbid")` (`config.py:48`) — every
  config section rejects unknown keys.
- **`Config` composes sections** via `Field(default_factory=...)` (`config.py:281-291`).
- **Platform selection is a function, not a field default** — `select_push_to_talk` +
  `_platform_key` (`config.py:377-397`), with `push_to_talk: str = ""` meaning "defer to the
  platform default" (issue #9).
- **CLI `--no-*` flags** on `run` (`cli.py:184-193`), with the `--no-cleanup` tri-state:
  `cleanup_enabled = None if not no_cleanup else False`.

Three sub-decisions are near-forced by these idioms; two (`enabled` semantics, how much
tuning) were grilled.

## Decision

### 1. `OverlayConfig(_Section)` — `enabled` only, tri-state

```python
class OverlayConfig(_Section):
    # None = defer to the platform default (macOS on, others off); an explicit
    # true/false pins the choice on every platform. Mirrors the hotkeys
    # push_to_talk="" defer-to-platform idiom (issue #9).
    enabled: bool | None = None
```

- **Tri-state `enabled: bool | None = None`.** `None` means "platform decides"; explicit
  `true`/`false` pins across platforms. This mirrors the hotkeys `push_to_talk = ""`
  empty-as-defer pattern rather than baking OS logic into the default.
- **No tuning fields yet.** #15 lists bar count / FPS / opacity / styling under *"Not yet
  specified — a `/prototype` may be warranted."* Committing values now would pin numbers the
  map says are unsettled. Tuning fields are added in the implementation/prototype flow.
- Added to `Config` as `overlay: OverlayConfig = Field(default_factory=OverlayConfig)`
  (`config.py:281-291` pattern).

**Rejected — `enabled: bool = True`** (plain): simpler field, but a non-macOS user's config
would show `enabled: true` that silently does nothing, and it doesn't match the established
defer-to-platform idiom.

### 2. `select_overlay_enabled()` — resolution as a function

Mirror `select_push_to_talk`: a module function resolves the *effective* value; OS logic never
lives in the field default.

```python
def select_overlay_enabled(
    config: OverlayConfig, *, no_overlay: bool = False, platform: str | None = None
) -> bool:
    if no_overlay:                       # 1. CLI flag: hard off for this run
        return False
    if config.enabled is not None:       # 2. explicit config wins on every platform
        return config.enabled
    plat = platform if platform is not None else sys.platform  # 3. platform decides
    return _platform_key(plat) == "macos"
```

`platform` is injectable (defaults to `sys.platform`) so tests stay deterministic regardless
of host OS — same discipline as `select_push_to_talk`.

### 3. `--no-overlay` CLI flag on `run`

Mirror `--no-paste`/`--no-cleanup` (`cli.py`). Pass its boolean into `select_overlay_enabled`
(or an equivalent tri-state into the controller); `--no-overlay` forces off for that run
regardless of config or platform.

### 4. Precedence — flag > explicit config > platform > fail-open

The effective decision resolves in this exact order:

1. **`--no-overlay`** → hard off (explicit per-run user intent).
2. else **explicit config `enabled` (`true`/`false`)** → wins on *every* platform.
3. else (`enabled is None`) → **platform decides**: macOS on, others off.
4. **then, independently, fail-open:** even if 1–3 resolve to "on", if the AppKit import or
   panel creation fails, the overlay silently no-ops.

**Consequence, confirmed and accepted:** a non-macOS user with `enabled: true` resolves to
"on" at step 2, then **fail-open (step 4) turns it off** because AppKit isn't importable there
— *not* an explicit platform rule. We keep this deliberately: "no AppKit" is the single source
of truth for "can't show," which is more honest than a platform policy that duplicates what the
missing framework already enforces. The overlay never appears on Linux/Windows either way.
(#15's out-of-scope "Windows/Linux overlay is a future effort" is thus enforced by the absence
of AppKit, not by a platform gate.)

**Rejected — platform hard-gate** (`select_overlay_enabled` returns `False` on non-darwin
regardless of config): more explicit about the scope boundary, but adds a platform check above
config and relies on policy where fail-open already suffices.

### 5. Fail-open behavior (the hard invariant)

Ties ADR-0001 + ADR-0003 together into the precise rule:

- The macOS GUI host / overlay is built behind a `try/except` in `cli.run()` (ADR-0001). Any
  failure — non-`darwin`, `ImportError` on AppKit (`pyobjc-framework-Cocoa`, #18), `NSApp` /
  panel creation failure — is caught.
- On failure, the fan-out (ADR-0003) is constructed **console-only**; `AppController` runs its
  fallback blocking `run()` on the main thread. **Dictation continues exactly as today.**
- The overlay is never a gate: a missing/broken AppKit degrades to today's terminal behavior,
  never blocks or alters dictation. `select_overlay_enabled()` returning `True` is a *request*
  to show the overlay; the `try/except` is what makes that request safe.

## Consequences

**Positive**

- Consistent with every existing config idiom (`_Section`, `default_factory`, function-based
  platform selection, `--no-*` flags).
- Minimal surface: one field now; tuning deferred to when the prototype fixes the numbers.
- Precedence is a single, testable function; fail-open is the single "can't show" authority.
- No new fail-open machinery — reuses ADR-0001's `try/except` and ADR-0003's fan-out.

**Negative / costs accepted**

- A non-macOS `enabled: true` "works" at the config layer and is only neutralized by
  fail-open — could momentarily read as "enabled" in `config show-effective` output.
  Mitigation: `select_overlay_enabled` is the effective value; a debug line can note "overlay
  requested but AppKit unavailable" if useful (implementation detail).

**Follow-ups (implementation / #22)**

- #22 (test strategy) must cover `select_overlay_enabled` across the truth table: `--no-overlay`
  wins; explicit `true`/`false` wins on every platform; `None` → macOS-on/others-off (with an
  injected `platform`); and that a simulated AppKit `ImportError` yields a console-only fan-out
  with dictation intact.
- `config init` / `show-effective` gain an `[overlay]` section (implementation).
- Tuning fields (bar count / fps / opacity) added when a `/prototype` settles the values
  (#15's "Not yet specified").

## Open questions (for implementation)

- Whether the controller takes an `overlay_enabled: bool | None` tri-state (like
  `cleanup_enabled`) or a resolved `bool` — leaning toward resolving in `cli.run()` via
  `select_overlay_enabled` and passing the host/notifier in, keeping the controller unaware of
  platform logic. `[uncertain]`
- Whether `config show-effective` should surface the *resolved* overlay state (post
  fail-open) or the *requested* one — leaning requested, with fail-open as a runtime note.
  `[uncertain]`
