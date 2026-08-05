# Windows overlay recipe — always-on-top, click-through, non-activating HUD (issue #40)

Research date: 2026-07-19. Sources are PRIMARY: Microsoft's Win32 / Windows App
developer documentation on `learn.microsoft.com` (Winuser.h window styles and
functions, the "Window Features" conceptual overview, the DXGI flip-model guidance),
the Tcl/Tk + CPython `tkinter` reference docs, and the Qt 6 `Qt::WindowType`
reference. Explicit statements from those sources are `[sourced]` (with the URL);
claims reasoned from sourced facts + the repo are `[inferred]`; things the #41
prototype must confirm on real hardware are `[uncertain]`.

This is the **Windows analogue** of the macOS recipe in
`docs/research/nspanel-nonactivating-float-recipe.md` (borderless non-activating
`NSPanel` shown via `orderFrontRegardless`, `NSStatusWindowLevel`, click-through via
`setIgnoresMouseEvents_`, redrawn by a main-thread `NSTimer` reading
`controller.latest_level`). The seam it must plug into is the platform-neutral
`Notifier`/`OverlayNotifier` contract (`src/seda/notifications/__init__.py`) and the
`Overlay` handle shape (`show`/`hide`/`teardown`/`dispatch_main`, level polled ~60 Hz)
established by the macOS host (`src/seda/gui/host.py`).

Scope: findings only. No production code or config was changed; this file is the
deliverable.

---

## Summary for our design (most relevant recipe decisions)

- **The decisive no-focus-steal property is a combination of extended window styles set
  at creation, NOT a single flag.** Create the HUD with
  `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST`
  (plus base style `WS_POPUP`). `WS_EX_NOACTIVATE` = "does not become the foreground
  window when the user clicks it"; `WS_EX_TRANSPARENT` (on a layered window) = "the
  mouse events will be passed to other windows underneath"; `WS_EX_TOOLWINDOW` = "does
  not appear in the taskbar or … ALT+TAB"; `WS_EX_TOPMOST` = "above all non-topmost
  windows … even when the window is deactivated". `[sourced]`
- **Show WITHOUT activating** via `ShowWindow(hWnd, SW_SHOWNOACTIVATE)` ("the window is
  not activated") or `SW_SHOWNA` ("not activated"), and keep it placed with
  `SetWindowPos(hWnd, HWND_TOPMOST, …, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)`
  ("Does not activate the window"). **Never call `SetForegroundWindow` / `SetActiveWindow`**
  on the HUD. The `SetForegroundWindow` doc's own example demonstrates exactly this
  "make it topmost without the foreground focus" pattern
  (`SWP_SHOWWINDOW | SWP_NOACTIVATE`). `[sourced]`
- **The decisive click-through fact is documented on the *layered-window hit-testing*
  page, not the extended-styles table.** The extended-styles table's `WS_EX_TRANSPARENT`
  entry only talks about paint order; the "Window Features" overview states that a
  layered window with `WS_EX_TRANSPARENT` has its shape ignored and "the mouse events
  will be passed to other windows underneath". So click-through requires **both**
  `WS_EX_LAYERED` and `WS_EX_TRANSPARENT`. `[sourced]`
- **Always-on-top** via `HWND_TOPMOST` ("maintains its topmost position even when it is
  deactivated"). This floats over normal and other non-topmost windows. Floating over a
  **true exclusive-fullscreen / Independent-Flip** app is NOT guaranteed — those bypass
  the DWM compositor — so fullscreen coverage is a prototype question (see Open
  questions). `[sourced/uncertain]`
- **Transparency / translucent rounded HUD:** two documented layered-window paths.
  Simple: `SetLayeredWindowAttributes` with `LWA_ALPHA` (whole-window opacity) and/or
  `LWA_COLORKEY` (a chroma-key color drawn where you want holes) — draw normally via
  `WM_PAINT`. Per-pixel: `UpdateLayeredWindow` with a premultiplied 32-bit ARGB bitmap +
  `BLENDFUNCTION`/`AC_SRC_ALPHA` for true per-pixel antialiased rounded corners. The two
  are mutually exclusive on the same window. `[sourced]`
- **Toolkit verdict:** **Raw Win32 (ctypes / pywin32)** is the only option that is
  *documented* to achieve every property, including click-through + no-activate, at
  essentially zero new dependency weight. **Qt (PySide6/PyQt6)** documents the equivalent
  flags (`Qt.WindowTransparentForInput`, `Qt.WindowDoesNotAcceptFocus`,
  `Qt.WindowStaysOnTopHint`, `Qt.Tool`, `Qt.FramelessWindowHint`,
  `WA_TranslucentBackground`) but is a heavyweight dependency. **Tkinter** ships with
  CPython and documents `-alpha`/`-topmost`/`-transparentcolor` + `overrideredirect`,
  but its docs do **not** cover click-through or no-activate — those would need raw Win32
  poked onto the Tk `HWND`, which is the crux the prototype must settle. `[sourced/uncertain]`
- **Redraw driver** (the Windows analogue of the macOS main-thread `NSTimer` polling
  `controller.latest_level`): Win32 `SetTimer` → `WM_TIMER` dispatched by the window's
  own message loop (raw Win32), `QTimer` (Qt), or `widget.after(ms, …)` (Tkinter). The
  hard rule is the same as macOS: **all GUI/window calls must happen on the thread that
  owns the window and runs its message loop** — the audio/hotkey threads only update a
  shared level float; the UI thread's timer reads it and repaints. `[sourced/inferred]`

---

## 1. Extended window styles — the no-focus-steal + no-taskbar posture

Set these `dwExStyle` bits at `CreateWindowEx` time (base `dwStyle = WS_POPUP` for a
chrome-free popup). Documented behavior of each, verbatim from the Extended Window Styles
table: `[sourced]`
<https://learn.microsoft.com/en-us/windows/win32/winmsg/extended-window-styles>

- **`WS_EX_NOACTIVATE` (0x08000000):** "A top-level window created with this style **does
  not become the foreground window when the user clicks it**. The system does not bring
  this window to the foreground when the user minimizes or closes the foreground window.
  The window should not be activated through programmatic access or via keyboard
  navigation by accessible technology, such as Narrator. … **The window does not appear on
  the taskbar by default.**" — This is the core no-focus-theft flag. `[sourced]`
- **`WS_EX_TRANSPARENT` (0x00000020):** the table entry describes only *paint order*
  ("should not be painted until siblings beneath the window … have been painted"). The
  *click-through* behavior is documented separately under layered-window hit testing (see
  §3). `WS_EX_TRANSPARENT` is necessary but only delivers click-through **in combination
  with `WS_EX_LAYERED`**. `[sourced]`
- **`WS_EX_LAYERED` (0x00080000):** "The window is a layered window." Required for both
  the transparency APIs (§4) and the documented click-through hit-testing rule (§3).
  Supported for top-level windows on all versions; for child windows since Windows 8.
  `[sourced]`
- **`WS_EX_TOOLWINDOW` (0x00000080):** "The window is intended to be used as a floating
  toolbar. … **A tool window does not appear in the taskbar or in the dialog that appears
  when the user presses ALT+TAB.**" — Keeps the HUD out of the taskbar and the ALT+TAB
  switcher (the Windows analogue of macOS accessory / `LSUIElement`). `[sourced]`
- **`WS_EX_TOPMOST` (0x00000008):** "The window should be placed above all non-topmost
  windows and should stay above them, **even when the window is deactivated**. To add or
  remove this style, use the `SetWindowPos` function." — Always-on-top (see §2).
  `[sourced]`

**Belt-and-braces (analogue of the macOS `canBecomeKeyWindow` override):** even with
`WS_EX_NOACTIVATE`, a robust HUD should also refuse activation defensively by handling
`WM_MOUSEACTIVATE` → return `MA_NOACTIVATE` in the window procedure, so a click on the
HUD can never activate it. With `WS_EX_TRANSPARENT` the HUD receives no mouse messages at
all, so this is redundant in the click-through configuration but cheap insurance if
click-through is ever disabled. `[inferred]`

## 2. Always-on-top — over normal windows, and the fullscreen caveat

- **`HWND_TOPMOST` (via `SetWindowPos`):** "Places the window above all non-topmost
  windows. **The window maintains its topmost position even when it is deactivated.**"
  Set it with `SetWindowPos(hWnd, HWND_TOPMOST, 0,0,0,0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)`.
  `[sourced]`
  <https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowpos>
- **`SWP_NOACTIVATE` (0x0010):** "Does not activate the window. If this flag is not set,
  the window is activated and moved to the top …" — this is what keeps the z-order /
  topmost change from stealing focus. `[sourced]`
- **Z-order semantics:** "The topmost window receives the highest rank and is the first
  window in the Z order." A window becomes topmost "by setting the `hWndInsertAfter`
  parameter to `HWND_TOPMOST` and ensuring that the `SWP_NOZORDER` flag is not set."
  `[sourced]`
- **Fullscreen caveat (the key uncertainty).** `HWND_TOPMOST` orders the HUD above other
  *desktop-composited* windows, but the DXGI guidance documents that **exclusive
  fullscreen and "Independent Flip"** send an app's frames straight to the display and let
  the DWM compositor "go to sleep": "it is possible to bypass desktop composition entirely
  and directly send application frames to the screen, in the same way that exclusive
  fullscreen does. … the DWM can go to sleep … Your application frames are sent directly to
  the screen, independently." When composition is bypassed, a topmost overlay is *not*
  guaranteed to be composited over the fullscreen app (though the doc also notes DWM can
  "seamlessly transition back to composed mode … if other desktop contents come on top").
  **Borderless-windowed-fullscreen** apps stay DWM-composited, so a topmost HUD should draw
  over them; **true exclusive fullscreen** is the risk case. This is macOS's
  `fullScreenAuxiliary` question in Windows clothing and must be settled by the prototype.
  `[sourced/uncertain]`
  <https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/for-best-performance--use-dxgi-flip-model>

## 3. Click-through — the decisive hit-testing rule

The click-through property is **documented on the "Window Features" → Layered Windows
overview**, in the hit-testing paragraph — not in the extended-styles table: `[sourced]`
<https://learn.microsoft.com/en-us/windows/win32/winmsg/window-features>

> "Hit testing of a layered window is based on the shape and transparency of the window.
> This means that the areas of the window that are color-keyed or whose alpha value is
> zero will let the mouse messages through. However, **if the layered window has the
> `WS_EX_TRANSPARENT` extended window style, the shape of the layered window will be
> ignored and the mouse events will be passed to other windows underneath the layered
> window.**"

- **Consequence:** `WS_EX_LAYERED | WS_EX_TRANSPARENT` = the entire HUD is click-through
  regardless of its opaque/translucent pixels. This is the direct analogue of macOS
  `setIgnoresMouseEvents_(True)`, and it also removes the last click-to-activate path
  (a click that never reaches the HUD can never activate it). `[sourced/inferred]`
- Because the HUD is a pure feedback surface (never interactive), full click-through is
  exactly what we want — no need for the finer alpha/color-key hit-testing. `[inferred]`

## 4. Transparency / per-pixel alpha — drawing a translucent rounded HUD

A layered window (`WS_EX_LAYERED`) becomes visible only after one of two mutually
exclusive APIs is called: `[sourced]` (Window Features overview + the two function docs)

**Option A — `SetLayeredWindowAttributes` (simple, GDI `WM_PAINT` drawing):** `[sourced]`
<https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setlayeredwindowattributes>
- Signature `SetLayeredWindowAttributes(hwnd, crKey, bAlpha, dwFlags)`.
- **`bAlpha`** — "opacity of the layered window … When `bAlpha` is 0, the window is
  completely transparent. When `bAlpha` is 255, the window is opaque." (whole-window
  constant alpha; use with `LWA_ALPHA`). `[sourced]`
- **`crKey` + `LWA_COLORKEY`** — "All pixels painted by the window in this color will be
  transparent." So you can punch fully-transparent regions by painting them in a chosen
  chroma-key color. `[sourced]`
- You still draw the HUD normally in `WM_PAINT`. Limitation: color-key transparency is
  1-bit (a pixel is either the key color or not) — **no smooth per-pixel antialiased
  rounded corners**; combined constant alpha applies to the whole window. Good enough for a
  simple translucent rounded rect if you accept color-key edges or a rectangular
  translucent card. `[sourced/inferred]`

**Option B — `UpdateLayeredWindow` (per-pixel alpha, true antialiased shape):** `[sourced]`
<https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-updatelayeredwindow>
- "Updates the position, size, shape, content, and translucency of a layered window."
- Use `ULW_ALPHA` with a `BLENDFUNCTION` whose `AlphaFormat = AC_SRC_ALPHA`, supplying a
  32-bit **premultiplied ARGB** DIB as `hdcSrc`. This gives true per-pixel alpha —
  smoothly antialiased rounded corners and a soft translucent fill, exactly the macOS
  `NSBezierPath` rounded-rect look. `[sourced/inferred]`
- "`UpdateLayeredWindow` always updates the entire window. To update part of a window,
  use the traditional `WM_PAINT` and set the blend value using
  `SetLayeredWindowAttributes`." So the per-pixel path re-blits the whole HUD each frame —
  fine for a small ~160×48 HUD. `[sourced]`
- **Mutual exclusion (documented on both pages):** "after `SetLayeredWindowAttributes`
  has been called, subsequent `UpdateLayeredWindow` calls will fail until the layering
  style bit is cleared and set again." Pick one path per window. `[sourced]`

**Recommendation:** start with **Option A** (`SetLayeredWindowAttributes` + `LWA_ALPHA`,
GDI/`WM_PAINT` drawing) for a translucent rounded card — least code, matches the level-
meter drawing model. Move to **Option B** only if the prototype shows the color-key /
whole-window-alpha limitation produces visibly hard edges we can't live with. `[inferred]`

## 5. Show / hide without activating — the decisive primitives

- **`ShowWindow(hWnd, SW_SHOWNOACTIVATE)`** — "Displays a window in its most recent size
  and position. This value is similar to `SW_SHOWNORMAL`, **except that the window is not
  activated**." (For an already-sized visible window, `SW_SHOWNA` — "similar to `SW_SHOW`,
  except that the window is not activated" — is the equivalent.) These are the analogue of
  macOS `orderFrontRegardless()`. `[sourced]`
  <https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-showwindow>
- **Hide:** `ShowWindow(hWnd, SW_HIDE)` — "Hides the window and activates another window."
  (For the HUD, the "activates another window" clause is harmless because the HUD was
  never the active window.) `[sourced]`
- **Never call `SetForegroundWindow`/`SetActiveWindow` on the HUD.** `SetForegroundWindow`
  "Brings the thread that created the specified window into the foreground and activates
  the window. Keyboard input is directed to the window …" — the exact opposite of what we
  want. `[sourced]`
  <https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow>
- **Documented proof of the show-without-stealing-focus pattern** — the `SetForegroundWindow`
  page's own example makes a window topmost *without* foreground focus and only calls
  `SetForegroundWindow` when explicitly requested: `[sourced]`
  ```cpp
  // If the window is invisible we will show it and make it topmost without the
  // foreground focus. …
  SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
               SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW |
               (bVisible ? SWP_NOACTIVATE : 0));
  ```
  With `WS_EX_NOACTIVATE` set at creation and `SW_SHOWNOACTIVATE` / `SWP_NOACTIVATE` on
  every show, the currently-focused app keeps keyboard focus when the overlay appears.
  `[sourced/inferred]`

## 6. Redraw driver — the Windows analogue of the main-thread NSTimer

The repo produces one audio level float per block (`controller.latest_level`, backed by
`audio/recorder.py`'s `peak_level`/`_rms`); the HUD just polls it ~60 Hz and repaints a
few bars. `[sourced — repo: src/seda/gui/host.py, src/seda/audio/recorder.py]`

**The hard rule (identical in spirit to macOS's all-AppKit-on-main-thread):** Win32
windows and their message loops are **thread-affine** — a window is serviced by the thread
that created it and pumps its messages; another thread must not call window APIs on it
directly. So, mirroring the macOS design: the audio/hotkey threads only update a shared
level float; the **UI thread's** timer reads it and repaints. `[inferred]` (grounded in the
`SetTimer` ownership rule below)

Timer options, per toolkit:

- **Raw Win32 — `SetTimer` → `WM_TIMER`:** `SetTimer(hWnd, id, uElapse_ms, NULL)` posts a
  `WM_TIMER` message to the thread's queue, dispatched by that thread's `GetMessage`/
  `DispatchMessage` loop. The associated "window must be owned by the calling thread." Note
  `uElapse` is clamped to `USER_TIMER_MINIMUM` = 10 ms (so ~100 Hz max; 16 ms ≈ 60 Hz is
  fine). `WM_TIMER` is a low-priority message (delivered only when the queue is otherwise
  empty), which is acceptable for a feedback HUD. On each tick: read the level float, then
  invalidate/repaint (`InvalidateRect` + `WM_PAINT`, or re-`UpdateLayeredWindow`).
  `[sourced]`
  <https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-settimer>
- **Qt — `QTimer`** at ~16 ms in the GUI thread; its `timeout` runs in the thread with the
  event loop. `[inferred — Qt event-loop model]`
- **Tkinter — `widget.after(ms, callback)`**, self-rescheduling, runs in the Tk mainloop
  thread. `[inferred — Tk event model]`

All three require the timer to live on, and repaint from, the thread that owns the window
and pumps its loop — the same constraint the macOS host encodes with `dispatch_main` /
`performSelectorOnMainThread_`. `[inferred]`

---

## 7. Toolkit landscape (Python) — decisive property, dependency cost, threading

Assessed against (i) does it *documentedly* achieve the decisive no-focus-steal +
click-through property, (ii) dependency cost, (iii) threading / event-loop implications.

### 7a. Raw Win32 via `pywin32` or `ctypes` — RECOMMENDED

- **(i) Decisive property:** **Yes, fully documented.** Every property above maps directly
  to a Win32 style/flag/call sourced in §§1–6:
  `CreateWindowEx(WS_EX_LAYERED|WS_EX_TRANSPARENT|WS_EX_NOACTIVATE|WS_EX_TOOLWINDOW|WS_EX_TOPMOST, …, WS_POPUP, …)`,
  `SetLayeredWindowAttributes`/`UpdateLayeredWindow`, `ShowWindow(SW_SHOWNOACTIVATE)`,
  `SetWindowPos(HWND_TOPMOST, … SWP_NOACTIVATE)`, `SetTimer`. `[sourced]`
- **(ii) Dependency cost:** **Lowest.** `ctypes` is in the stdlib → **zero new
  dependencies** (call `user32`/`gdi32` directly). `pywin32` is a heavier optional
  convenience (`win32gui`/`win32con` wrappers, C-extension), only if raw `ctypes` structs
  (`BLENDFUNCTION`, `POINT`, `SIZE`, `WNDCLASS`) prove too fiddly. Prefer `ctypes` to keep
  parity with the low-dependency macOS side. `[inferred]`
- **(iii) Threading / event loop:** You own the message pump (`GetMessage`/
  `TranslateMessage`/`DispatchMessage`) on the UI thread, and a `WNDPROC` callback. This is
  the closest structural analogue of the macOS host owning `NSApplication.run()` — it likely
  reopens the same main-thread-ownership question the macOS side resolved in ADR-0001 (who
  owns the blocking loop; hotkey/audio threads marshal onto it). Marshalling onto the UI
  thread uses `PostMessage`(a custom `WM_APP` message) — the Win32 analogue of
  `performSelectorOnMainThread_`. `[inferred]`
- **Verdict:** the recipe-faithful, lowest-dependency choice; the one option whose every
  property is backed by a primary-source citation.

### 7b. Tkinter (ships with CPython) — the crux for #41

- **(i) Decisive property:** **Partly documented; the crux is UNKNOWN from docs.** Tk
  documents the *cosmetic* pieces — `attributes('-topmost', True)` ("displayed above all
  other windows"), `attributes('-alpha', f)` ("opacity, from 0.0 … to 1.0"),
  `attributes('-transparentcolor', color)` (Windows-only, "the color that is made fully
  transparent"), and `overrideredirect(True)` (borderless, no WM decorations). **But the Tk
  / tkinter docs do NOT document click-through or non-activating / no-focus-steal windows
  at all.** Achieving those on Tk almost certainly requires reaching the underlying `HWND`
  (`root.winfo_id()` → the toplevel's window handle) and applying the raw Win32 ex-styles
  (`WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE`) via `GetWindowLong`/
  `SetWindowLong` from `ctypes` — which is undocumented territory for Tk and may fight Tk's
  own window management. **This is the single biggest open question the prototype must
  settle.** `[sourced for the documented attrs; uncertain for click-through+no-activate]`
  <https://docs.python.org/3/library/tkinter.html>
  <https://www.tcl.tk/man/tcl9.0/TkCmd/wm.html>
- **(ii) Dependency cost:** **Zero** (stdlib), same weight class as `ctypes` — *if* it can
  be made to work. But if it needs `ctypes` `SetWindowLong` poking anyway, raw Win32 (7a)
  is cleaner and fully documented, undercutting Tk's only advantage.
- **(iii) Threading / event loop:** Tk is single-threaded — all Tk calls must be on the
  `mainloop` thread; use `after()` for the redraw and `after_idle`/an event-queue trick to
  marshal from other threads. Reconciling Tk's `mainloop()` with the existing
  hotkey/audio-thread architecture is the same ownership question as everywhere. `[inferred]`
- **Verdict:** attractive for its zero dependency *only if* the click-through + no-activate
  crux holds on real Tk `HWND`s; otherwise it collapses into "raw Win32 with extra Tk
  overhead." Prototype must decide.

### 7c. Qt — PyQt6 / PySide6 — heavyweight

- **(i) Decisive property:** **Yes, documented.** Qt exposes first-class flags for every
  property: `Qt.WindowTransparentForInput` ("Makes the window transparent for input
  events" → click-through), `Qt.WindowDoesNotAcceptFocus` ("The window does not want
  focus" → no focus steal), `Qt.WindowStaysOnTopHint` ("should stay on top of other
  windows"), `Qt.Tool` ("A tool window"), `Qt.FramelessWindowHint` ("Produces a borderless
  window"), plus `WA_TranslucentBackground` for per-pixel alpha. `[sourced]`
  <https://doc.qt.io/qt-6/qt.html>
- **(ii) Dependency cost:** **Highest** — PySide6/PyQt6 pulls tens of MB of Qt binaries.
  This directly contradicts seda's local-first, low-footprint posture (cf. the deliberately
  minimal PyObjC dependency on macOS in `overlay-pyobjc-dependency.md`). `[inferred]`
- **(iii) Threading / event loop:** Qt requires a `QApplication` event loop owning the GUI
  thread; `QTimer` for redraw; `QMetaObject.invokeMethod(..., Qt.QueuedConnection)` /
  signals-slots to marshal from worker threads. Same main-thread-ownership shape. `[inferred]`
- **Verdict:** the most *ergonomic* API (named flags for each property) but the wrong
  dependency weight for this project. Keep as a fallback only if raw Win32 proves
  impractical from Python.

**Bottom line:** raw Win32 (`ctypes`, optionally `pywin32`) is the recipe-faithful,
lowest-cost, fully-documented choice and the recommended target for the #41 prototype;
Tkinter is the zero-dependency wildcard whose viability hinges entirely on the
click-through + no-activate crux; Qt works but is too heavy for seda.

---

## Concrete Win32-via-ctypes sketch (illustrative — verify in #41)

```python
# windows_overlay.py — Windows-only; wrap creation in try/except to fail open,
# exactly like the macOS host's fail-open boundary in src/seda/gui/host.py.
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

WS_POPUP          = 0x80000000
WS_EX_LAYERED     = 0x00080000
WS_EX_TRANSPARENT = 0x00000020   # click-through (with WS_EX_LAYERED) — §3
WS_EX_NOACTIVATE  = 0x08000000   # no focus theft — §1
WS_EX_TOOLWINDOW  = 0x00000080   # no taskbar / no ALT+TAB — §1
WS_EX_TOPMOST     = 0x00000008   # always on top — §1/§2

SW_SHOWNOACTIVATE = 4            # show without activating — §5
SW_HIDE           = 0
HWND_TOPMOST      = wintypes.HWND(-1)
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
LWA_ALPHA, LWA_COLORKEY = 0x00000002, 0x00000001

def create_overlay(wndproc, w=160, h=48):
    ex = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
          | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
    # ... register a WNDCLASS with `wndproc`, then:
    hwnd = user32.CreateWindowExW(ex, class_atom, "seda-hud", WS_POPUP,
                                  x, y, w, h, None, None, hinst, None)
    # Option A transparency (§4): whole-window 55% alpha, GDI drawing in WM_PAINT.
    user32.SetLayeredWindowAttributes(hwnd, 0, 140, LWA_ALPHA)
    return hwnd

def show(hwnd):
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)                    # §5 — no activate
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)  # §2 — topmost, no activate

def hide(hwnd):
    user32.ShowWindow(hwnd, SW_HIDE)

# Redraw: SetTimer(hwnd, TID, 16, None) posts WM_TIMER (§6); in the WNDPROC,
# on WM_TIMER read the latest level float and InvalidateRect(hwnd,...); on
# WM_PAINT draw the translucent rounded rect + level-meter bars.
# On WM_MOUSEACTIVATE return MA_NOACTIVATE (belt-and-braces, §1).
```

(Constant values are from the sourced Winuser.h tables above. Exact `ctypes` struct
layouts — `WNDCLASSW`, `PAINTSTRUCT`, `BLENDFUNCTION` — and the `WNDPROC` `CFUNCTYPE`
signature must be verified against a real build in #41.)

---

## Open questions (what the #41 prototype must settle on real hardware)

- **[uncertain] The Tkinter click-through + no-activate crux.** Tk documents `-topmost`,
  `-alpha`, `-transparentcolor`, and `overrideredirect`, but **not** click-through or
  no-activate. Can we reliably apply `WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE`
  to a Tk toplevel's `HWND` (`winfo_id()`) via `ctypes` `SetWindowLong` **without** Tk
  fighting it (Tk may reset styles, or `-transparentcolor` may conflict with the layered
  attributes)? If this fails or is flaky, Tkinter is out and raw Win32 (7a) is the pick.
  **This is the decisive experiment.** `[uncertain]`
- **[uncertain] Fullscreen coverage.** Does an `HWND_TOPMOST` layered overlay actually
  appear over (a) borderless-windowed-fullscreen apps and (b) true exclusive-fullscreen /
  Independent-Flip apps? The DXGI docs say exclusive/independent-flip bypass the DWM
  compositor, so (b) may not be covered — the Windows analogue of the macOS
  `fullScreenAuxiliary` question. Test across a browser, a video player, and a fullscreen
  game. `[uncertain]`
- **[uncertain] Transparency path choice — `SetLayeredWindowAttributes` vs
  `UpdateLayeredWindow`.** Does Option A's color-key / whole-window alpha give acceptable
  rounded-corner quality, or do we need Option B's per-pixel `UpdateLayeredWindow` +
  premultiplied ARGB for the macOS-parity soft rounded look? Settle by eye on a real
  display. `[uncertain]`
- **[inferred, architecture] Message-loop ownership.** Raw Win32 (and Qt and Tk) each need
  a GUI thread that owns the window and pumps its loop, with hotkey/audio threads marshalling
  onto it — the Windows re-run of the macOS ADR-0001 main-thread-inversion decision. Decide
  whether the Windows host owns a `GetMessage` loop the way `gui/host.py` owns
  `NSApplication.run()`, and how `dispatch_main` maps (`PostMessage` of a custom `WM_APP`).
  `[inferred]`
- **[uncertain] `pywin32` vs pure `ctypes`.** Confirm whether pure `ctypes` (zero deps) is
  ergonomic enough for the `WNDCLASS`/`WNDPROC`/`BLENDFUNCTION` plumbing, or whether the
  `pywin32` convenience is worth the extra dependency. Prefer `ctypes` for dependency parity
  with the macOS side unless the prototype shows it's unmanageable. `[uncertain]`

---

## Sources

Microsoft Win32 / Windows developer documentation (primary):
- Extended Window Styles (WS_EX_LAYERED / TRANSPARENT / NOACTIVATE / TOOLWINDOW / TOPMOST): <https://learn.microsoft.com/en-us/windows/win32/winmsg/extended-window-styles>
- Window Features (Layered Windows overview + layered-window hit-testing / click-through rule): <https://learn.microsoft.com/en-us/windows/win32/winmsg/window-features>
- ShowWindow (SW_SHOWNOACTIVATE / SW_SHOWNA / SW_HIDE): <https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-showwindow>
- SetWindowPos (HWND_TOPMOST, SWP_NOACTIVATE, z-order semantics): <https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowpos>
- SetLayeredWindowAttributes (LWA_ALPHA / LWA_COLORKEY; mutual exclusion note): <https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setlayeredwindowattributes>
- UpdateLayeredWindow (per-pixel alpha, ULW_ALPHA, BLENDFUNCTION; whole-window update): <https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-updatelayeredwindow>
- SetForegroundWindow (restrictions + "topmost without foreground focus" example): <https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow>
- SetTimer (WM_TIMER posted to owning thread's queue; USER_TIMER_MINIMUM): <https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-settimer>
- DXGI flip model — best performance (exclusive fullscreen / Independent Flip bypass DWM composition): <https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/for-best-performance--use-dxgi-flip-model>

Toolkit references (primary):
- Python `tkinter` reference: <https://docs.python.org/3/library/tkinter.html>
- Tcl/Tk `wm` command (`-topmost`, `-alpha`, `-transparentcolor`, `overrideredirect`): <https://www.tcl.tk/man/tcl9.0/TkCmd/wm.html>
- Qt 6 `Qt::WindowType` (WindowTransparentForInput, WindowDoesNotAcceptFocus, WindowStaysOnTopHint, Tool, FramelessWindowHint): <https://doc.qt.io/qt-6/qt.html>

Repo files grounding the seam / redraw-driver findings:
- `/Users/I748258/Projects/seda/src/seda/gui/host.py` (macOS host: show/hide/teardown/dispatch_main contract, main-thread NSTimer polling `controller.latest_level`)
- `/Users/I748258/Projects/seda/src/seda/notifications/__init__.py` (Notifier Protocol, OverlayNotifier with injected show/hide/dispatch_main)
- `/Users/I748258/Projects/seda/docs/research/nspanel-nonactivating-float-recipe.md` (the macOS recipe being mirrored)
```
