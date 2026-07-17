# Overlay PyObjC / AppKit dependency declaration (issue #18)

Research resolving a wayfinder decision: how should the PyObjC/AppKit dependency
be declared for a macOS-only, fail-open floating-overlay feature?

Scope: findings only. No production code, config, or `pyproject.toml` was changed.
Each claim is tagged `[sourced]` (backed by a primary source below), `[inferred]`
(reasoned from sourced facts + the repo), or `[uncertain]`.

---

## Recommendation

**Use an environment-marker base dependency** (Option B): declare
`pyobjc-framework-Cocoa` in `[project] dependencies` with a `sys_platform == "darwin"`
marker, pinned to the same style as the rest of the file (`>=`, no upper cap beyond
what PyObjC itself needs). Keep the lazy-import-inside-function + `try/except`
fail-open pattern as the runtime safety net.

Rationale:
- **Non-macOS installs never pull it** — the PEP 508 marker `sys_platform == "darwin"`
  evaluates False on Linux/Windows, so the dependency is ignored there, including on
  headless Linux CI. `[sourced]`
- **AppKit is not guaranteed today** — the app relies on pynput transitively pulling
  `pyobjc-framework-Quartz` on darwin, and Quartz *does* pull Cocoa (hence AppKit).
  But that is an accident of pynput's dependency graph, not a contract; a base
  dependency makes AppKit's presence explicit and version-controlled. `[sourced]`
- **Overlay is a core, always-on macOS UI surface**, not an opt-in backend like
  `whisper`/`vad`/`cleanup`. An optional extra (Option C) would mean the overlay
  silently no-ops unless the user remembered `pip install local-flow[overlay]`,
  which is worse UX for a first-class feature. A base marker dep installs it
  automatically for every macOS user while staying invisible to non-macOS installs.
  `[inferred]`
- **Fail-open discipline is preserved** — the declaration only raises the odds the
  import succeeds; the lazy import + `try/except` in the overlay module (mirroring
  `hotkeys.py`) remains the actual runtime guarantee. `[inferred]`

### Exact `pyproject.toml` diff to apply

In `[project] dependencies` (currently ends at `"typer>=0.12,<1",`), add one line:

```toml
dependencies = [
  "numpy>=1.24,<3",
  "sounddevice>=0.4,<1",
  "pynput>=1.7,<2",
  "pyperclip>=1.8,<2",
  "pydantic>=2.6,<3",
  "platformdirs>=4,<5",
  "typer>=0.12,<1",
  # AppKit (NSPanel/NSWindow/NSApplication) for the floating overlay; macOS-only.
  # Pulled transitively by pynput today via Quartz, but declared explicitly so the
  # overlay's presence does not depend on another package's dependency graph.
  # Marker keeps it off Linux/Windows installs, including headless CI.
  "pyobjc-framework-Cocoa>=10.1; sys_platform == \"darwin\"",
]
```

(`>=10.1` is a floor that comfortably covers the `>=3.11` Python requirement; see
Q4. An unpinned `pyobjc-framework-Cocoa; sys_platform == "darwin"` is also
defensible for a lean local-first tool — pick the floor if you want reproducibility,
drop it if you prefer to always track PyObjC's latest.)

### Exact mypy diff to apply

In the single `[[tool.mypy.overrides]]` `module = [...]` list, add the new PyObjC
modules alongside the existing `"Quartz.*"` entry:

```toml
module = [
  "sounddevice.*",
  "pynput.*",
  "Quartz.*",
  "AppKit.*",
  "Cocoa.*",
  "objc.*",
  "pyperclip.*",
  "faster_whisper.*",
  "ctranslate2.*",
  "httpx.*",
]
```

Add `"AppKit.*"` for the imported module; `"Cocoa.*"` and `"objc.*"` only if the
overlay code imports those names directly (see Q5). `[inferred]`

---

## Q1 — Which PyObjC distribution provides AppKit, and what it pulls

- **AppKit lives in `pyobjc-framework-Cocoa`.** The Cocoa wrapper is an umbrella that
  bundles the **AppKit, Foundation, and CoreFoundation** module wrappers; after
  installing it you `import AppKit` (and `Foundation`, `Cocoa`). So
  `NSPanel`/`NSWindow`/`NSApplication` come from `pyobjc-framework-Cocoa`. `[sourced]`
- **`pyobjc-framework-Cocoa` depends only on `pyobjc-core`** (Requires-Dist:
  `['pyobjc-core>=12.2.1']` in the current 12.2.1 release). `[sourced]`
- **Depending on Cocoa does NOT pull Quartz.** The dependency arrow points the other
  way: `pyobjc-framework-Quartz` Requires-Dist is
  `['pyobjc-core>=12.2.1', 'pyobjc-framework-Cocoa>=12.2.1']` — i.e. **Quartz depends
  on Cocoa**, not vice versa. `[sourced]`
- Consequence for this repo: the app's current Quartz usage in `hotkeys.py` is
  actually *already* pulling Cocoa/AppKit transitively **wherever Quartz is present**.
  Today Quartz arrives via pynput (see Q3), so on a normal macOS install AppKit is
  usually importable. But because neither Quartz nor Cocoa is *declared* by this
  project, AppKit's presence is not guaranteed by the project's own metadata. `[inferred]`

## Q2 — PEP 508 environment markers for macOS-only deps

- **Exact syntax:** `pyobjc-framework-Cocoa; sys_platform == "darwin"` (optionally with
  a PEP 440 version specifier before the `;`, e.g.
  `pyobjc-framework-Cocoa>=10.1; sys_platform == "darwin"`). `[sourced]`
- When a marker evaluates False the dependency **is ignored**, so this installs only
  on macOS and is excluded from Linux and Windows — including headless Linux CI, where
  `sys.platform == "linux"`. `[sourced]`
- `sys_platform` maps to Python's `sys.platform`; documented values include `"linux"`,
  `"win32"`, `"darwin"`. It is the recommended field for platform-specific deps (more
  well-defined than `platform_system`, which maps to `platform.system()` →
  `"Linux"`/`"Windows"`/`"Darwin"`). Prefer `sys_platform == "darwin"`. `[sourced]`
- This mirrors how pynput itself declares its macOS deps (see Q3), so the style is
  consistent with a package already in the tree. `[sourced]`

## Q3 — Three options with concrete snippets (this project is hatchling)

Build backend is **hatchling** (`[build-system] requires = ["hatchling"]`,
`build-backend = "hatchling.build"`). Hatchling reads standard `[project] dependencies`
and `[project.optional-dependencies]` as PEP 508 strings and **supports environment
markers in both** regular and optional-dependency groups. `[sourced]`

**Option A — Optional extra**
```toml
[project.optional-dependencies]
overlay = ["pyobjc-framework-Cocoa>=10.1; sys_platform == \"darwin\""]
```
Installed only with `pip install local-flow[overlay]`. Off by default everywhere.
Downside: the overlay (a core UI surface) silently no-ops for users who don't opt in,
even on macOS. Best only if the overlay is genuinely optional. `[inferred]`

**Option B — Environment-marker base dependency (RECOMMENDED)**
```toml
[project]
dependencies = [
  # ...existing...
  "pyobjc-framework-Cocoa>=10.1; sys_platform == \"darwin\"",
]
```
Auto-installed on every macOS install; never pulled on Linux/Windows/CI. Explicit,
version-controlled, invisible to non-macOS. `[inferred]`

**Option C — Rely on transitive presence + fail-open (status quo)**
Declare nothing; rely on pynput pulling Quartz → Cocoa → AppKit on darwin, and on the
`try/except` guard if the import fails.
- **pynput 1.8.2** (allowed by the project's `pynput>=1.7,<2`) declares
  `pyobjc-framework-Quartz>=8.0; sys_platform == "darwin"`, and Quartz pulls Cocoa
  (Q1), so AppKit is usually present. `[sourced]`
- Fragile: it hinges on pynput keeping that transitive dep and on it never being
  overridden; the project's metadata makes no promise about AppKit. This is exactly
  the current Quartz situation, which #18 is trying to move away from. `[inferred]`

**Choice:** Option B. It satisfies (a) non-macOS installs never pull it (marker),
(b) fail-open discipline stays as the runtime net (unchanged), and (c) it keeps the
tool lean — one marker-gated line, no new extra to document or teach users. `[inferred]`

## Q4 — Version pinning

- The rest of `pyproject.toml` uses lower-bound-plus-cap ranges (`numpy>=1.24,<3`,
  `pynput>=1.7,<2`, etc.). `[sourced]`
- PyObjC (all `pyobjc-*` 12.x releases: core, Cocoa, Quartz) currently declares
  **`Requires-Python: >=3.10`**, which comfortably covers this project's
  `requires-python = ">=3.11"`. `[sourced]`
- Recommended style: a **lower `>=` floor with no tight upper cap** —
  `pyobjc-framework-Cocoa>=10.1`. Reasons:
  - A floor documents the minimum API level while letting macOS users get current
    PyObjC (important because PyObjC tracks OS/SDK changes). `[inferred]`
  - Unlike numpy/pynput, an upper cap on PyObjC risks blocking the exact new release
    a future macOS needs; PyObjC is closely tied to the OS, so pinning `<N` is more
    likely to hurt than help for a local-first desktop tool. `[inferred]`
  - **Unpinned** (`pyobjc-framework-Cocoa; sys_platform == "darwin"`) is an acceptable
    lean alternative; use the `>=10.1` floor only if you want a documented minimum.
    `[inferred]`
  - Do **not** pin to `12.2.1`-style exact versions — over-constrains a fast-moving
    OS-bridge package. `[inferred]`

## Q5 — mypy overrides

- The project has one `[[tool.mypy.overrides]]` block with
  `ignore_missing_imports = true` and a `module` list that already contains
  `"Quartz.*"`. `[sourced]`
- Mirror that for the new imports: add **`"AppKit.*"`** (the module the overlay
  imports for `NSPanel`/`NSWindow`/`NSApplication`). Add **`"Cocoa.*"`** and/or
  **`"objc.*"`** only if the overlay code imports those names directly (e.g.
  `import objc` for `objc.super`/protocols, or `from Cocoa import ...`). PyObjC ships
  no bundled type stubs, so these are needed to keep `strict` type-checking green on
  the non-macOS CI env where the modules aren't installed. `[inferred]`
- This is the same reasoning the file already documents for its native/optional
  entries (`Quartz.*`, `httpx.*`). `[sourced]`

## Q6 — Fail-open consequence

- Declaring the dependency raises the probability the import succeeds; it is **not** a
  guarantee. On a broken/partial PyObjC install, a headless/CI-like macOS runner
  without a window server, or a stripped environment, `import AppKit` (or an
  `NSApplication` call) can still fail (`ImportError`, `OSError`, or ObjC runtime
  errors). `[inferred]`
- Therefore the recommendation keeps the **runtime safety net independent of the
  dependency declaration**: the overlay must **lazily import AppKit inside the
  function that uses it** and wrap it in `try/except`, exactly as `hotkeys.py` does —
  `hotkeys.py` imports `Quartz`/`pynput` inside functions, guards with
  `sys.platform == "darwin"`, and catches `(ImportError, OSError)` /
  broad `except` to fail open. The overlay should catch import + ObjC errors and
  degrade gracefully (dictation keeps working without the overlay). `[sourced` for the
  `hotkeys.py` pattern; `inferred]` for applying it to the overlay.
- Net: **dependency declaration = "usually installed and version-controlled"; lazy
  import + try/except = "never crashes if it isn't."** Both are needed; neither
  replaces the other. `[inferred]`

---

## Sources

- PyObjC docs (readthedocs, latest): <https://pyobjc.readthedocs.io/en/latest/> —
  Python 3.10+ support; framework-wrapper overview.
- PyObjC framework wrappers page: <https://pyobjc.readthedocs.io/en/latest/notes/framework-wrappers.html>
- PyObjC Cocoa API notes (Cocoa = umbrella over AppKit + Foundation + CoreFoundation):
  <https://github.com/ronaldoussoren/pyobjc/blob/main/docs/apinotes/Cocoa.md>
- PyObjC Cocoa README (Cocoa wraps CoreFoundation, Foundation, AppKit):
  <https://github.com/ronaldoussoren/pyobjc/blob/main/pyobjc-framework-Cocoa/README.txt>
- PyObjC intro (AppKit importable as `AppKit`; `objc`/`PyObjCTools`):
  <https://github.com/ronaldoussoren/pyobjc/blob/main/docs/core/intro.md>
- PyPI metadata `pyobjc-framework-Cocoa` (Requires-Dist `pyobjc-core>=12.2.1`;
  Requires-Python `>=3.10`): <https://pypi.org/pypi/pyobjc-framework-Cocoa/json>
- PyPI metadata `pyobjc-framework-Quartz` (Requires-Dist `pyobjc-core`,
  `pyobjc-framework-Cocoa`; Requires-Python `>=3.10`):
  <https://pypi.org/pypi/pyobjc-framework-Quartz/json>
- PyPI metadata `pyobjc-core` (no Requires-Dist; Requires-Python `>=3.10`):
  <https://pypi.org/pypi/pyobjc-core/json>
- PyPI metadata `pynput` 1.8.2 (darwin deps include
  `pyobjc-framework-Quartz>=8.0; sys_platform == "darwin"`):
  <https://pypi.org/pypi/pynput/json>
- Python Packaging — Dependency specifiers / PEP 508 environment markers
  (`sys_platform == "darwin"`; markers ignored when False; `sys_platform` values):
  <https://packaging.python.org/en/latest/specifications/dependency-specifiers/>
- Hatch dependency config (PEP 508 strings + environment markers in dependencies and
  optional-dependencies): <https://hatch.pypa.io/latest/config/dependency/>
- Repo files grounding the snippets:
  `/Users/I748258/Projects/seda/pyproject.toml`,
  `/Users/I748258/Projects/seda/src/local_flow/input/hotkeys.py`
