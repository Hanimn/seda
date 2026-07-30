# ADR-0010 — The Windows HUD uses per-pixel alpha (UpdateLayeredWindow + GDI+), spike-gated

- **Status:** Accepted (implementation gated on the #66 hardware spike)
- **Date:** 2026-07-30
- **Deciders:** wayfinder grilling session (Hani Momeninia + agent)
- **Issue:** [#63 — Decision: Windows HUD visual + behavioral parity spec](https://github.com/Hanimn/seda/issues/63)
- **Map:** [#39 — Map: Windows recording HUD (cross-platform overlay)](https://github.com/Hanimn/seda/issues/39)
- **Spec:** [`docs/specs/windows-hud-parity.md`](../specs/windows-hud-parity.md)
- **Gates:** [#66 — Prototype: prove Option B on real Windows hardware](https://github.com/Hanimn/seda/issues/66)
- **Reverses:** the "**start with Option A**" recommendation in `docs/research/windows-overlay-recipe.md` §4 (branch `research/windows-overlay-recipe`) — hence this record.
- **Builds on:** [ADR-0008](0008-windows-gui-host-threading-model.md) (raw Win32 via `ctypes`, zero new deps), [ADR-0009](0009-overlay-host-boundary.md) (sibling host), [ADR-0007](0007-persistent-hud-lifecycle.md) (the render target's lifecycle).

## Context

The macOS recording HUD (`src/seda/gui/host.py`, `WaveformView.drawRect_`) is a **translucent
rounded card with alpha-blended, antialiased bars** drawn with two *independent* opacities: the
card at alpha **0.55**, the bars at **0.92** (listening) / **0.45→0.92** (busy sweep). Visually
this reads as "bright bars floating on a dim card" with smoothly antialiased `NSBezierPath`
corners. Parity with this look is the explicit goal of #63 — "**target visual/behavioral parity
with the macOS mirror-bars design unless the toolkit forces a change.**"

The toolkit is settled (ADR-0008: raw Win32 via stdlib `ctypes`). A Win32 layered window
(`WS_EX_LAYERED`) becomes visible only via **one of two mutually exclusive** APIs
(`docs/research/windows-overlay-recipe.md` §4, hardware-validated in #41):

- **Option A — `SetLayeredWindowAttributes` + GDI `WM_PAINT`.** A single **whole-window**
  `bAlpha`, and/or a **1-bit** color-key. Least code; matches a plain GDI draw model. #41
  validated Option-A transparency on real hardware (Q4 PASS).
- **Option B — `UpdateLayeredWindow` + a premultiplied 32-bit ARGB DIB.** True **per-pixel**
  alpha; re-blits the whole (small) window each frame.

The recipe recommended *starting with A*. This ADR revisits that against the actual render
target and reverses it. The pivotal question was whether reaching macOS parity on Windows
requires a **new dependency** (which would violate the zero-new-deps ethos of ADR-0008/0009).

## Decision

The Windows HUD uses **Option B — `UpdateLayeredWindow` with per-pixel premultiplied ARGB,
antialiased via GDI+** (`Gdiplus.dll`) through stdlib `ctypes`. **Option A is retained as a
documented fallback**, and **implementation is gated on the on-hardware spike #66** (the same
`/prototype`-on-real-Windows discipline #41 set).

### 1. Option A is a parity *failure*, not a toolkit-forced compromise

`SetLayeredWindowAttributes`/`LWA_ALPHA` applies **one** opacity to every pixel, so it cannot
render the card-0.55 / bars-0.92 depth separation that *is* the macOS look; and its rounded
corners (via `SetWindowRgn`/`RoundRect` + color-key) are **1-bit, non-antialiased**. Option A
would ship a recognizably flatter, harder-edged HUD. The ticket permits a change only when the
**toolkit forces** it — and, per §2, it does not.

### 2. Zero-new-deps survives per-pixel antialiasing — the crux, resolved

GDI+ ships as the **system DLL `Gdiplus.dll`** on every Windows since XP / 2000
(Microsoft's `GdiplusStartup` Requirements table: minimum client Windows XP, DLL `Gdiplus.dll`,
**no `req.redist`**). Calling its flat C API via `ctypes.windll.gdiplus` is the **same raw-Win32
move** ADR-0008 already blessed — no pip dependency, no redistributable, no SDK. So the
dependency-free path *does* reach exact parity: `UpdateLayeredWindow`'s per-pixel alpha restores
the independent card/bar opacities, and `GdipSetSmoothingMode(SmoothingModeAntiAlias)` restores
feathered edges, after which `WaveformView.drawRect_`'s math ports 1:1. Because a dependency-free
path reaches parity, the toolkit does **not** force Option A's compromise (§1).

### 3. The A-vs-B cost delta is small; correctness risk is the only real cost

All host wiring — the ADR-0008 pump, ADR-0009 lifecycle skeleton, the event→mode contract, the
redraw throttle — is **transparency-path-agnostic**. The only difference between A and B is the
~50-line draw+blit core. A 160×48 (and 48×24 idle) ARGB blit at ~60 Hz is sub-millisecond, and
`UpdateLayeredWindow` is *designed* for per-frame animated layered windows. So "A is less code"
buys almost nothing, while B meets the explicit parity goal. GDI+ requires an explicit
`GdiplusStartup`/`GdiplusShutdown` pair (the shutdown joins the ADR-0008 `finally:` teardown).

### 4. Gate on the #66 spike; Option A is the bounded fallback

#41 validated *Option-A* transparency, not Option B's specific stack
(`GdiplusStartup → CreateDIBSection → GdipCreateFromHDC → premultiplied ARGB →
UpdateLayeredWindow → GdiplusShutdown`). That stack is documented but **unproven on the fleet**,
so implementation is gated on **#66** proving it renders without alpha halos, with antialiased
corners, and tears down clean (`IsWindow(after) == False`, no leaked GDI+ token). The gate is
low-risk precisely because the **downside is capped**: a failed spike falls back to Option A —
the Q4-validated path — localized to the draw+blit core, accepting the §1 gaps. We fund the
spike rather than accept those gaps by default.

## Consequences

**Positive**

- Exact visual parity with the shipped macOS HUD (independent card/bar alpha, antialiased card + bar caps, all three modes' math verbatim) — the stated #63 goal.
- Zero new dependencies preserved (system `Gdiplus.dll` via `ctypes`), consistent with ADR-0008/0009.
- The choice is isolated: only the ~50-line draw+blit core differs from a hypothetical Option-A build; everything else is path-agnostic, so the fallback is a small, local swap.

**Negative / costs accepted**

- Implementation is **blocked on #66** (a hardware spike) — a real dependency in the plan, deliberately taken over shipping a flatter HUD by default.
- A per-frame premultiplied-ARGB blit is more intricate than a plain GDI paint (the premultiply-or-halo footgun); mitigated by a black-card/gray-white-bars palette that folds premultiplication into brush choice, kept in one guarded helper — no per-pixel Python loop.
- One extra init/teardown pair (`GdiplusStartup`/`GdiplusShutdown`) to manage in the lifecycle.

## Rejected alternatives

- **Option A (`SetLayeredWindowAttributes`, whole-window alpha + 1-bit color-key).** Shipped-and-#41-validated and least code, but a **parity failure** (§1): flattened card/bar contrast, aliased corners. Kept only as the **fallback if the #66 spike fails** — not the default.
- **A hybrid (Option A for the card, per-pixel for the bars).** A layered window admits exactly one of the two APIs; they are mutually exclusive per window (documented on both API pages). No coherent hybrid exists — rejected as impossible, not merely undesirable.

## Notes for implementation (non-binding)

- Declare `restype`/`argtypes` on **every** GDI+/Win32 `ctypes` prototype (ADR-0008's HWND-truncation lesson) — the GDI+ flat API takes `GpGraphics*`/`GpBrush*` handles that truncate identically on Win64.
- The #66 harness can extend the `proto/windows-overlay-focus` branch (#41) rather than start fresh — it already has the layered window + pump + teardown scaffolding.
