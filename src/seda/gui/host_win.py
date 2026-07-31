"""Windows GUI host that owns the main thread (ADR-0008, ADR-0009).

The Windows sibling of :mod:`seda.gui.host` (macOS/AppKit). Same *shape* — a GUI
host owns the main thread, registers the overlay, starts the controller, pumps a
GUI loop, and tears the window down in a ``finally`` on every exit — but
Win32-specific *mechanics*: an interruptible ``PeekMessageW(PM_REMOVE)`` pump
with a bare polled stop flag (ADR-0008 §2/§3), and raw Win32 + GDI+ via stdlib
``ctypes`` (zero new deps).

**Draw core (Step 4, epic #74).** Every Win32/GDI+ touch sits behind a
module-level shim (ADR-0005) so unit tests monkeypatch fakes and **CI never
loads ``ctypes.windll``**. The Option-B render path (:func:`_paint` +
:func:`_blit`) is the hardware-validated stack from spike #66: GDI+ draws the
card + bars into a top-down 32-bit ARGB DIB with ``CompositingModeSourceCopy``,
the bytes are premultiplied, then ``UpdateLayeredWindow(ULW_ALPHA)`` blits them.
The three modes' math ports 1:1 from ``WaveformView.drawRect_`` (parity spec
`docs/specs/windows-hud-parity.md`); ``HudMode.IDLE`` renders the compressed pill
(the #56 look) in a shrunk 48×24 panel at the shared ~10 Hz idle cadence — the
DIB stays 160×48 and IDLE draws + blits only its top-left sub-rect (#79).

**CI-cleanliness invariant.** ``import ctypes`` at module top is fine (stdlib,
present on Linux), but ``ctypes.WINFUNCTYPE`` does not exist on non-Windows
CPython and ``ctypes.WinDLL`` cannot load Windows DLLs there. So the WNDPROC
functype, the ``ctypes.Structure`` layouts, the DLL handles, and the
``restype``/``argtypes`` declarations are all built **lazily inside**
:func:`_load_libs`, first called from :func:`build_overlay`. Importing this
module on Linux runs zero native code.

**Fail-open is the hard invariant** (epic #15, `docs/specs/windows-hud-fail-open.md`):
this host never lets a missing or broken Win32/GDI+ layer affect dictation. The
fail-open boundary lives in the shared :func:`seda.gui._hostloop.run_hosted`
(build fails → ``False`` → terminal path); this module supplies the ``supports``
gate, the transactional :func:`build_overlay`, and the :func:`_run_win32_loop`
body (past the boundary — a failure there propagates, exactly as macOS).
"""

from __future__ import annotations

import ctypes
import logging
import queue
import signal
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from seda.notifications import (
    HUD_ACTIVE_HZ,
    HUD_ACTIVE_PANEL_H,
    HUD_ACTIVE_PANEL_W,
    HUD_IDLE_HZ,
    HUD_IDLE_PANEL_H,
    HUD_IDLE_PANEL_W,
    HUD_IDLE_PILL_H,
    HUD_IDLE_PILL_W,
    HudMode,
    hud_idle_shimmer,
)

if TYPE_CHECKING:
    from seda.app import AppController

logger = logging.getLogger(__name__)

# --- Win32 constants (plain ints — safe to define at import) ----------------
# Extended styles for the overlay window (validated on hardware, #41): layered +
# transparent = click-through; no-activate + tool-window + topmost = never steals
# focus, no taskbar/ALT+TAB, always on top. Never SetForegroundWindow.
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
_OVERLAY_EX_STYLE = (
    WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
)
WS_POPUP = 0x80000000

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOWNA = 8

# Window messages / pump / cursor (plain ints — import-safe).
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_MOUSEACTIVATE = 0x0021
WM_QUIT = 0x0012
MA_NOACTIVATE = 3
PM_REMOVE = 0x0001
IDC_ARROW = 32512

# Z-order / SetWindowPos flags.
HWND_TOPMOST = -1  # wrapped as ctypes.c_void_p(-1) at the call site
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004

# CreateDIBSection / BITMAPINFOHEADER.
BI_RGB = 0
DIB_RGB_COLORS = 0

# UpdateLayeredWindow / BLENDFUNCTION.
AC_SRC_OVER = 0x00  # BlendOp
AC_SRC_ALPHA = 0x01  # AlphaFormat -> source is premultiplied ARGB
ULW_ALPHA = 0x00000002

# GDI+ enums / status.
SMOOTHING_MODE_ANTIALIAS = 4
# CompositingModeSourceCopy (1), NOT SourceOver (0) — see the discrepancy note in
# _gdip_create_from_hdc and memory:windows-hud-gdiplus-sourcecopy (#66 hardware finding).
COMPOSITING_MODE_SOURCE_COPY = 1
FLUSH_INTENTION_SYNC = 1
GDIP_OK = 0

# Win32 error / system-parameter codes.
ERROR_CLASS_ALREADY_EXISTS = 1410
SPI_GETWORKAREA = 0x0030
# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (HANDLE)-4.
_DPI_PER_MONITOR_V2 = -4

_CLASS_NAME = "SedaHudOverlay"

# Palette (straight-alpha ARGB, 0xAARRGGBB as GDI+ expects). Verbatim from the
# macOS render truth via the #66 spike: card black alpha 0.55 -> 140; bars white
# alpha 0.92 -> 235. Exact integer literals (no int() on a fractional alpha).
CARD_ALPHA = 140
BAR_ALPHA = 235
CARD_ARGB = (CARD_ALPHA << 24) | 0x000000  # 0x8C000000
BAR_ARGB = (BAR_ALPHA << 24) | 0xFFFFFF  # 0xEBFFFFFF

# ctypes width aliases (plain type objects — import-safe). GDI+ handles are
# pointer-width (Gp* opaque pointers); the GDI+ token is ULONG_PTR.
GpHandle = ctypes.c_void_p
ULONG_PTR = ctypes.c_size_t

# Panel geometry (1:1 with macOS build_overlay; parity spec Part 2).
_PANEL_W, _PANEL_H = 160, 48

# Redraw cadence (ADR-0007 §5) — shared cross-platform policy. ~60 Hz active for
# LISTENING/BUSY, throttled to ~10 Hz in IDLE. The rates come from the shared
# notifications knobs (HUD_ACTIVE_HZ / HUD_IDLE_HZ) so macOS and Windows cannot
# diverge; here they are turned into SetTimer millisecond intervals.
_ACTIVE_INTERVAL_MS = round(1000 / HUD_ACTIVE_HZ)  # ~16 ms
_IDLE_INTERVAL_MS = round(1000 / HUD_IDLE_HZ)  # ~100 ms
_DISPATCH_QUEUE_MAX = 256  # bounded dispatch_main queue (fail-open E6)

# Pump cadence (ADR-0008 §2): drain messages, service the stop flag, sleep ~5 ms.
_PUMP_SLEEP_SECONDS = 0.005


def _interval_ms(mode: HudMode) -> int:
    """Redraw interval (ms) for *mode* (ADR-0007 §5): idle throttled, else active.

    LISTENING and BUSY are both motion-carrying (~60 Hz); IDLE drops to ~10 Hz for
    its slow shimmer, cutting idle wakeups ~6×. Re-armed on every ``set_mode``.
    """
    return _IDLE_INTERVAL_MS if mode is HudMode.IDLE else _ACTIVE_INTERVAL_MS


def _panel_size(mode: HudMode) -> tuple[int, int]:
    """The on-screen panel size ``(w, h)`` for *mode* (parity spec Part 2).

    IDLE shrinks to 48×24, LISTENING/BUSY use the active 160×48. The backing DIB
    stays a fixed 160×48; :func:`_paint` draws the mode's content into the top-left
    ``w×h`` sub-region and :func:`_blit` copies exactly that sub-rect via
    ``UpdateLayeredWindow`` (``psize=(w,h)``, ``ptSrc=(0,0)``), sizing the window to
    match — so the shrink is dimension-matched in the same blit and never tears.
    """
    if mode is HudMode.IDLE:
        return HUD_IDLE_PANEL_W, HUD_IDLE_PANEL_H
    return HUD_ACTIVE_PANEL_W, HUD_ACTIVE_PANEL_H


# ---------------------------------------------------------------------------
# Lazy native cache (ADR-0005 CI-cleanliness). Everything that would touch
# ``ctypes.WINFUNCTYPE`` or ``ctypes.windll`` — the WNDPROC functype, the
# ``Structure`` layouts, the DLL handles — is built ONCE inside :func:`_load_libs`
# and stashed here. At import ``_NATIVE`` is ``None`` and nothing native is
# evaluated, so importing this module on Linux runs zero native code (the
# ``test_module_imports_without_touching_windll`` poison test). ``ctypes.Structure``
# subclasses with ``wintypes`` fields are technically import-safe (the aliases
# resolve to plain ``ctypes.c_*``), but the WNDPROC functype is NOT — so we keep
# ALL native surface in one lazy place rather than splitting it fragilely.
# ---------------------------------------------------------------------------


@dataclass
class _Native:
    """The lazily-built Win32/GDI+ surface: DLL handles, the WNDPROC functype, the
    ctypes ``Structure`` layouts, the process HINSTANCE, and the window class name."""

    user32: Any
    gdi32: Any
    gdiplus: Any
    kernel32: Any
    wt: Any  # the ctypes.wintypes module (shims reach RECT/POINT/MSG/BOOL/HANDLE via it)
    WNDPROCTYPE: Any
    WNDCLASSEXW: type
    BITMAPINFO: type
    BITMAPINFOHEADER: type
    BLENDFUNCTION: type
    GdiplusStartupInput: type
    SIZE: type
    hinst: Any
    class_name: str


_NATIVE: _Native | None = None  # populated once by _load_libs; None at import


def _n() -> _Native:
    """Return the populated native cache (asserts :func:`_load_libs` has run)."""
    assert _NATIVE is not None, "_load_libs() must run before any native shim"
    return _NATIVE


def _win_error() -> BaseException:
    """Build an ``OSError`` from ``GetLastError`` (Windows-only ctypes surface).

    ``ctypes.WinError``/``ctypes.get_last_error`` exist ONLY on Windows: mypy on
    non-Windows flags them ``[attr-defined]``, mypy on Windows would flag the
    ignore as unused — so the dual-code ``# type: ignore`` lives here, in ONE
    place, and every shim raises ``raise _win_error()`` instead of repeating it.
    Returns (does not raise) so call sites read ``raise _win_error()``.
    """
    get_last_error = ctypes.get_last_error  # type: ignore[attr-defined, unused-ignore]
    win_error = ctypes.WinError  # type: ignore[attr-defined, unused-ignore]
    return win_error(get_last_error())  # type: ignore[no-any-return, unused-ignore]


def _last_error() -> int:
    """Return ``GetLastError`` (Windows-only ctypes surface; dual-code ignore).

    Used where the error *code* is inspected (e.g. the F1b already-registered
    allow-list) rather than raised — otherwise call :func:`_win_error`.
    """
    get_last_error = ctypes.get_last_error  # type: ignore[attr-defined, unused-ignore]
    return get_last_error()  # type: ignore[no-any-return, unused-ignore]


# ---------------------------------------------------------------------------
# GDI+ draw helpers (pure-Python signatures; take the gdiplus handle explicitly
# so they are import-safe and unit-reachable). Ported from the #66 spike.
# ---------------------------------------------------------------------------


def _add_round_rect_i(gp: Any, path: Any, x: int, y: int, w: int, h: int, r: int) -> None:
    """Build a rounded rectangle into *path* via 4 integer corner arcs + close.

    Card only (its geometry ``0,0,w,h,8`` is integral, so ``...ArcI`` truncates
    nothing). Angles: TL 180, TR 270, BR 0, BL 90 — each sweeping +90 clockwise
    (harness ``_add_round_rect`` verbatim).
    """
    d = 2 * r
    gp.GdipAddPathArcI(path, x, y, d, d, 180.0, 90.0)  # TL
    gp.GdipAddPathArcI(path, x + w - d, y, d, d, 270.0, 90.0)  # TR
    gp.GdipAddPathArcI(path, x + w - d, y + h - d, d, d, 0.0, 90.0)  # BR
    gp.GdipAddPathArcI(path, x, y + h - d, d, d, 90.0, 90.0)  # BL
    gp.GdipClosePathFigure(path)


def _add_round_rect_f(gp: Any, path: Any, x: float, y: float, w: float, h: float, r: float) -> None:
    """Rounded rectangle via 4 FLOAT corner arcs (``GdipAddPathArc``).

    Used for the pill bars: integer arcs would truncate the bar's top and height
    independently and shift it off-centre by up to 1 px, breaking the vertical
    symmetry about ``cy``. Float arcs keep sub-pixel symmetry.
    """
    d = 2.0 * r
    gp.GdipAddPathArc(path, x, y, d, d, 180.0, 90.0)
    gp.GdipAddPathArc(path, x + w - d, y, d, d, 270.0, 90.0)
    gp.GdipAddPathArc(path, x + w - d, y + h - d, d, d, 0.0, 90.0)
    gp.GdipAddPathArc(path, x, y + h - d, d, d, 90.0, 90.0)
    gp.GdipClosePathFigure(path)


def _fill_bar(gp: Any, g: Any, brush: Any, x: float, cy: float, half: float, bw: int) -> None:
    """Fill one pill bar: rect ``(x, cy-half, bw, 2*half)``, cap radius ``bw/2``.

    PARITY: macOS ``drawRect_`` fills bars with roundedRect xRadius=width/2 (pill
    caps). The #66 spike used plain ``FillRectangleI`` (square) — enough for the
    halo/AA verdict but NOT pixel-parity. We port pill caps via a per-bar FLOAT
    rounded path (r=bw/2, ``GdipAddPathArc`` not ``...ArcI``) so top/height keep
    sub-pixel symmetry about ``cy``. #66 proved AA edges composite clean, so pill
    bars composite clean too. The brush carries the bar's alpha.
    """
    path = GpHandle()
    gp.GdipCreatePath(0, ctypes.byref(path))
    try:
        _add_round_rect_f(gp, path, x, cy - half, float(bw), 2.0 * half, bw / 2.0)
        gp.GdipFillPath(g, brush, path)
    finally:
        gp.GdipDeletePath(path)


def _fill_pill(gp: Any, g: Any, brush: Any, cx: float, cy: float, w: float, h: float) -> None:
    """Fill the IDLE compressed pill: a horizontal capsule centered at ``(cx, cy)``.

    ``w×h`` capsule (cap radius = h/2), the #56 idle look. Same FLOAT rounded-path
    primitive as :func:`_fill_bar`; the brush carries the shimmer alpha.
    """
    path = GpHandle()
    gp.GdipCreatePath(0, ctypes.byref(path))
    try:
        _add_round_rect_f(gp, path, cx - w / 2.0, cy - h / 2.0, w, h, h / 2.0)
        gp.GdipFillPath(g, brush, path)
    finally:
        gp.GdipDeletePath(path)


def _premultiply(backbuffer: dict[str, Any]) -> None:
    """Premultiply the DIB's straight-alpha ARGB in place (called from :func:`_blit`).

    PARITY-SPEC DISCREPANCY: parity spec Part 1 says "no per-pixel Python loop".
    #66 PROVED the walk is REQUIRED: ``UpdateLayeredWindow(AC_SRC_ALPHA)`` needs
    premultiplied BGRA, and GDI+ ``SmoothingModeAntiAlias`` produces fractional-
    coverage edge alphas unknown ahead of time, so pre-premultiplying known brush
    colors cannot cover the AA edges. O(w*h)=7680 px/frame is trivially fine.
    Follows the validated spike, not the stale spec line.

    A top-down BI_RGB 32bpp DIB is little-endian 0xAARRGGBB in memory, i.e. bytes
    ``[B, G, R, A]``. Premultiply B,G,R by A/255; leave A. Fully-transparent pixels
    (a==0) are forced to 0 so no stray colored fringe survives; a==255 is already
    premultiplied (x*255//255 == x) and skipped.
    """
    bits = backbuffer["bits"]
    w = backbuffer["w"]
    h = backbuffer["h"]
    npx = w * h
    buf = (ctypes.c_ubyte * (npx * 4)).from_address(bits.value)  # view over the DIB pixels
    for p in range(0, npx * 4, 4):
        a = buf[p + 3]
        if a == 0:
            buf[p] = 0
            buf[p + 1] = 0
            buf[p + 2] = 0
        elif a != 255:
            buf[p] = (buf[p] * a) // 255  # B
            buf[p + 1] = (buf[p + 1] * a) // 255  # G
            buf[p + 2] = (buf[p + 2] * a) // 255  # R


# ---------------------------------------------------------------------------
# Module-level shims (ADR-0005). The ONLY place raw ctypes/windll lives; unit
# tests monkeypatch ``seda.gui.host_win._<name>``. Native ctors/DLLs are lazy
# (first-touch from :func:`_load_libs`), so importing this module on Linux runs
# zero native code — the lifecycle/threading logic is proven Win32-free on CI.
# ---------------------------------------------------------------------------


def _load_libs() -> tuple[Any, Any, Any]:
    """Lazily load user32/gdi32/gdiplus, build ctypes layouts, declare prototypes.

    Called first from :func:`build_overlay`. Everything Windows-only lives here
    (never at import): ``ctypes.WinDLL``/``ctypes.windll`` (raises
    ``OSError``/``AttributeError`` on non-Windows — the natural fail-open trigger,
    catalog B2), the ``WINFUNCTYPE`` WNDPROC, the ``Structure`` layouts, and every
    ``restype``/``argtypes`` (mandatory on Win64 — HWND truncation, catalog G1).
    The built surface is cached on the module global :data:`_NATIVE`.
    """
    global _NATIVE
    import ctypes.wintypes as wt  # lazy; import-safe, kept here so native surface is one place

    # ctypes.windll exists ONLY on Windows: mypy on non-Windows flags it
    # [attr-defined], mypy on Windows would otherwise flag the ignore as unused —
    # so silence both codes to keep the strict check green on every CI platform.
    windll = ctypes.windll  # type: ignore[attr-defined, unused-ignore]
    user32 = windll.user32
    gdi32 = windll.gdi32
    gdiplus = windll.gdiplus
    kernel32 = windll.kernel32

    lresult = ctypes.c_ssize_t  # LRESULT is LONG_PTR (pointer-width), never c_long
    wndproctype = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined, unused-ignore]
        lresult, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM
    )

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.UINT),
            ("style", wt.UINT),
            ("lpfnWndProc", wndproctype),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wt.HINSTANCE),
            ("hIcon", wt.HICON),
            ("hCursor", wt.HANDLE),
            ("hbrBackground", wt.HBRUSH),
            ("lpszMenuName", wt.LPCWSTR),
            ("lpszClassName", wt.LPCWSTR),
            ("hIconSm", wt.HICON),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD),
            ("biWidth", wt.LONG),
            ("biHeight", wt.LONG),  # NEGATIVE => top-down DIB
            ("biPlanes", wt.WORD),
            ("biBitCount", wt.WORD),
            ("biCompression", wt.DWORD),
            ("biSizeImage", wt.DWORD),
            ("biXPelsPerMeter", wt.LONG),
            ("biYPelsPerMeter", wt.LONG),
            ("biClrUsed", wt.DWORD),
            ("biClrImportant", wt.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [
            ("bmiHeader", BITMAPINFOHEADER),
            ("bmiColors", wt.DWORD * 3),  # padding; unused for 32bpp BI_RGB
        ]

    class BLENDFUNCTION(ctypes.Structure):
        # Field ORDER and TYPES matter: all four are BYTE.
        _fields_ = [
            ("BlendOp", ctypes.c_ubyte),
            ("BlendFlags", ctypes.c_ubyte),
            ("SourceConstantAlpha", ctypes.c_ubyte),
            ("AlphaFormat", ctypes.c_ubyte),
        ]

    class GdiplusStartupInput(ctypes.Structure):
        _fields_ = [
            ("GdiplusVersion", wt.UINT),
            ("DebugEventCallback", ctypes.c_void_p),
            ("SuppressBackgroundThread", wt.BOOL),
            ("SuppressExternalCodecs", wt.BOOL),
        ]

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", wt.LONG), ("cy", wt.LONG)]

    # Assign the cache FIRST (with hinst placeholder), so _declare_prototypes can
    # read the struct types via _n(); fill hinst just below.
    _NATIVE = _Native(
        user32=user32,
        gdi32=gdi32,
        gdiplus=gdiplus,
        kernel32=kernel32,
        wt=wt,
        WNDPROCTYPE=wndproctype,
        WNDCLASSEXW=WNDCLASSEXW,
        BITMAPINFO=BITMAPINFO,
        BITMAPINFOHEADER=BITMAPINFOHEADER,
        BLENDFUNCTION=BLENDFUNCTION,
        GdiplusStartupInput=GdiplusStartupInput,
        SIZE=SIZE,
        hinst=None,
        class_name=_CLASS_NAME,
    )
    _declare_prototypes(user32, gdi32, gdiplus)

    # kernel32.GetModuleHandleW inline (the _declare_prototypes arity is frozen to
    # three libs; only GetModuleHandleW is needed here). restype MUST be declared
    # or the HMODULE truncates on Win64 (catalog G1).
    kernel32.GetModuleHandleW.restype = wt.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    _NATIVE.hinst = kernel32.GetModuleHandleW(None)  # the process module handle (§HINSTANCE)
    return user32, gdi32, gdiplus


def _declare_prototypes(user32: Any, gdi32: Any, gdiplus: Any) -> None:
    """Set ``restype``/``argtypes`` on every windll call (Win64 truncation guard).

    Mandatory — an undeclared ``CreateWindowExW`` truncates its 64-bit HWND to a
    32-bit ``c_int`` (catalog G1). Ported from the validated #66 spike. Reads the
    ctypes ``Structure`` types from the cache (:func:`_n`), which is already
    populated by :func:`_load_libs` before this call. Exercised by the T2
    win32-only integration test (G1), not T1.
    """
    n = _n()
    wt = n.wt
    c_int = ctypes.c_int

    # --- user32: class / window lifecycle ---
    user32.LoadCursorW.restype = wt.HANDLE
    user32.LoadCursorW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]

    user32.RegisterClassExW.restype = wt.ATOM
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(n.WNDCLASSEXW)]

    user32.UnregisterClassW.restype = wt.BOOL
    user32.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]

    user32.CreateWindowExW.restype = wt.HWND
    user32.CreateWindowExW.argtypes = [
        wt.DWORD,
        wt.LPCWSTR,
        wt.LPCWSTR,
        wt.DWORD,
        c_int,
        c_int,
        c_int,
        c_int,
        wt.HWND,
        wt.HMENU,
        wt.HINSTANCE,
        wt.LPVOID,
    ]

    user32.DestroyWindow.restype = wt.BOOL
    user32.DestroyWindow.argtypes = [wt.HWND]

    user32.DefWindowProcW.restype = ctypes.c_ssize_t  # LRESULT
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]

    user32.IsWindow.restype = wt.BOOL
    user32.IsWindow.argtypes = [wt.HWND]

    # --- show / z-order ---
    user32.ShowWindow.restype = wt.BOOL
    user32.ShowWindow.argtypes = [wt.HWND, c_int]

    user32.SetWindowPos.restype = wt.BOOL
    user32.SetWindowPos.argtypes = [
        wt.HWND,
        wt.HWND,
        c_int,
        c_int,
        c_int,
        c_int,
        wt.UINT,
    ]

    # --- work area (bottom-centre placement, catalog E7) ---
    user32.SystemParametersInfoW.restype = wt.BOOL
    user32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, wt.LPVOID, wt.UINT]

    # --- timers ---
    user32.SetTimer.restype = ctypes.c_void_p  # UINT_PTR
    user32.SetTimer.argtypes = [wt.HWND, ctypes.c_void_p, wt.UINT, wt.LPVOID]
    user32.KillTimer.restype = wt.BOOL
    user32.KillTimer.argtypes = [wt.HWND, ctypes.c_void_p]

    # --- message pump ---
    user32.PeekMessageW.restype = wt.BOOL
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wt.MSG),
        wt.HWND,
        wt.UINT,
        wt.UINT,
        wt.UINT,
    ]
    user32.TranslateMessage.restype = wt.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t  # LRESULT
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.PostQuitMessage.restype = None
    user32.PostQuitMessage.argtypes = [c_int]

    # --- DCs (for the DIB blit) ---
    user32.GetDC.restype = wt.HDC
    user32.GetDC.argtypes = [wt.HWND]
    user32.ReleaseDC.restype = c_int
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]

    # --- UpdateLayeredWindow: the blit that IS the paint (no WM_PAINT) ---
    user32.UpdateLayeredWindow.restype = wt.BOOL
    user32.UpdateLayeredWindow.argtypes = [
        wt.HWND,
        wt.HDC,  # hdcDst (NULL)
        ctypes.POINTER(wt.POINT),  # pptDst
        ctypes.POINTER(n.SIZE),  # psize
        wt.HDC,  # hdcSrc (memory DC with DIB)
        ctypes.POINTER(wt.POINT),  # pptSrc
        wt.COLORREF,  # crKey
        ctypes.POINTER(n.BLENDFUNCTION),  # pblend
        wt.DWORD,  # dwFlags
    ]

    # --- gdi32: memory DC + DIB section ---
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.DeleteDC.restype = wt.BOOL
    gdi32.DeleteDC.argtypes = [wt.HDC]
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi32.DeleteObject.restype = wt.BOOL
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi32.CreateDIBSection.restype = wt.HBITMAP
    gdi32.CreateDIBSection.argtypes = [
        wt.HDC,
        ctypes.POINTER(n.BITMAPINFO),
        wt.UINT,
        ctypes.POINTER(ctypes.c_void_p),  # ppvBits (OUT)
        wt.HANDLE,
        wt.DWORD,
    ]

    # --- GDI+ flat API. All handles pointer-width; status is GpStatus (int). ---
    gdiplus.GdiplusStartup.restype = c_int
    gdiplus.GdiplusStartup.argtypes = [
        ctypes.POINTER(ULONG_PTR),  # &token (OUT, pointer-width)
        ctypes.POINTER(n.GdiplusStartupInput),
        ctypes.c_void_p,  # &output (NULL)
    ]
    gdiplus.GdiplusShutdown.restype = None
    gdiplus.GdiplusShutdown.argtypes = [ULONG_PTR]

    gdiplus.GdipCreateFromHDC.restype = c_int
    gdiplus.GdipCreateFromHDC.argtypes = [wt.HDC, ctypes.POINTER(GpHandle)]
    gdiplus.GdipDeleteGraphics.restype = c_int
    gdiplus.GdipDeleteGraphics.argtypes = [GpHandle]

    gdiplus.GdipSetSmoothingMode.restype = c_int
    gdiplus.GdipSetSmoothingMode.argtypes = [GpHandle, c_int]
    gdiplus.GdipSetCompositingMode.restype = c_int
    gdiplus.GdipSetCompositingMode.argtypes = [GpHandle, c_int]

    gdiplus.GdipGraphicsClear.restype = c_int
    gdiplus.GdipGraphicsClear.argtypes = [GpHandle, wt.DWORD]  # ARGB

    gdiplus.GdipCreateSolidFill.restype = c_int
    gdiplus.GdipCreateSolidFill.argtypes = [wt.DWORD, ctypes.POINTER(GpHandle)]  # ARGB, &brush
    gdiplus.GdipDeleteBrush.restype = c_int
    gdiplus.GdipDeleteBrush.argtypes = [GpHandle]

    gdiplus.GdipCreatePath.restype = c_int
    gdiplus.GdipCreatePath.argtypes = [c_int, ctypes.POINTER(GpHandle)]  # FillMode, &path
    gdiplus.GdipDeletePath.restype = c_int
    gdiplus.GdipDeletePath.argtypes = [GpHandle]
    gdiplus.GdipAddPathArcI.restype = c_int
    gdiplus.GdipAddPathArcI.argtypes = [
        GpHandle,
        c_int,
        c_int,
        c_int,
        c_int,
        ctypes.c_float,
        ctypes.c_float,  # startAngle, sweepAngle
    ]
    gdiplus.GdipAddPathArc.restype = c_int  # float variant (pill bars)
    gdiplus.GdipAddPathArc.argtypes = [
        GpHandle,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_float,
    ]
    gdiplus.GdipClosePathFigure.restype = c_int
    gdiplus.GdipClosePathFigure.argtypes = [GpHandle]
    gdiplus.GdipFillPath.restype = c_int
    gdiplus.GdipFillPath.argtypes = [GpHandle, GpHandle, GpHandle]  # graphics, brush, path

    # GdipFlush(graphics, FlushIntention): GDI+ batches drawing; flush Sync so the
    # premultiply walk reads committed bytes, not a torn/partial frame.
    gdiplus.GdipFlush.restype = c_int
    gdiplus.GdipFlush.argtypes = [GpHandle, c_int]


def _make_wndproc(state: dict[str, Any]) -> Any:
    """Build the ``WINFUNCTYPE`` WNDPROC callback (GC-keepalive hazard).

    The returned object MUST outlive :func:`_destroy_window` — ``WM_DESTROY``
    dispatches into it synchronously during the destroy call. It is held on the
    :class:`Overlay` (``_wndproc_ref``) until after teardown destroys the window.

    ``WM_TIMER`` reaches the redraw via ``state["tick"]`` (mirrored onto *state*
    by :func:`build_overlay`). The tick is UNGUARDED here by contract: the pump's
    ``DispatchMessageW`` path is where a raising tick surfaces, and
    :func:`_run_win32_loop` lets it propagate (a genuine draw crash is not
    silently swallowed at the WNDPROC).
    """
    n = _n()

    def _proc(hwnd: Any, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE  # belt-and-braces: never activate on a click
        if msg == WM_TIMER:
            tick = state.get("tick")
            if tick is not None:
                tick()  # re-blit for animation (NO WM_PAINT under ULW)
            return 0
        if msg == WM_CLOSE:
            n.user32.DestroyWindow(hwnd)  # traverse WM_DESTROY for real teardown
            return 0
        if msg == WM_DESTROY:
            n.user32.PostQuitMessage(0)
            return 0
        result: int = n.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        return result

    # The WNDPROCTYPE-wrapped callable is the GC-keepalive object held on Overlay.
    wndproc: Any = n.WNDPROCTYPE(_proc)
    return wndproc


def _gdiplus_startup() -> Any:
    """``GdiplusStartup``; returns the token held for teardown (catalog C2)."""
    n = _n()
    token = ULONG_PTR(0)
    inp = n.GdiplusStartupInput(1, None, False, False)
    st = n.gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(inp), None)
    if st != GDIP_OK:
        raise OSError(f"GdiplusStartup status={st}")  # C2
    return token  # ULONG_PTR held for teardown


def _gdiplus_shutdown(token: Any) -> None:
    """``GdiplusShutdown`` (teardown, catalog F1)."""
    if token is not None:
        _n().gdiplus.GdiplusShutdown(token)


def _set_dpi_awareness() -> None:
    """``SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)`` (catalog C1/C1b).

    Decisive for render quality: without it the DWM bitmap-stretches the layered
    surface on any non-100%-DPI machine, softening the antialiased corners.
    Benign failures (``E_ACCESSDENIED`` "already set" / an absent entry point on
    down-level Windows) are swallowed *inside this shim* and the build continues
    (C1b allow-list); the older ``SetProcessDPIAware`` is the down-level fallback.
    """
    n = _n()
    try:
        n.user32.SetProcessDpiAwarenessContext.restype = n.wt.BOOL
        n.user32.SetProcessDpiAwarenessContext.argtypes = [n.wt.HANDLE]
        if n.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(_DPI_PER_MONITOR_V2)):
            return
    except (AttributeError, OSError):
        pass  # C1b benign: down-level entry point / already-set
    try:
        n.user32.SetProcessDPIAware.restype = n.wt.BOOL
        n.user32.SetProcessDPIAware.argtypes = []
        n.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass  # C1b benign fallback; a low-DPI box needs neither


def _register_class(wndproc_ref: Any) -> Any:
    """``RegisterClassExW``; returns the class atom (catalog C3).

    Register-or-reuse: an already-registered class is a success, not a failure
    (self-healing on relaunch, catalog F1b) — the class *name* works as
    ``lpClassName`` for ``CreateWindowExW`` regardless of the atom.

    The ``hinst`` threaded through :func:`build_overlay` is the frozen ``None``;
    the real module handle is ``_NATIVE.hinst`` (``GetModuleHandleW(None)``,
    restype=HMODULE, captured in :func:`_load_libs`). Signatures stay frozen.
    """
    n = _n()
    cls = n.WNDCLASSEXW()  # ctypes zero-inits all fields
    cls.cbSize = ctypes.sizeof(n.WNDCLASSEXW)
    cls.lpfnWndProc = wndproc_ref  # the WNDPROCTYPE object from _make_wndproc
    cls.hInstance = n.hinst
    cls.hCursor = n.user32.LoadCursorW(None, ctypes.c_wchar_p(IDC_ARROW))
    cls.hbrBackground = None  # layered ULW window paints itself; no bg brush
    cls.lpszClassName = n.class_name
    atom = n.user32.RegisterClassExW(ctypes.byref(cls))
    if not atom:
        if _last_error() == ERROR_CLASS_ALREADY_EXISTS:
            return n.class_name  # F1b register-or-reuse
        raise _win_error()  # C3
    return atom


def _unregister_class(atom: Any, hinst: Any) -> None:
    """``UnregisterClassW`` (teardown, catalog F1/F1b).

    ``hinst`` param is the frozen ``None`` from :func:`build_overlay`; use the
    cached ``_NATIVE.hinst`` (same handle as register — a mismatch fails the
    unregister). Unregistering by class name is correct for a name-registered class.
    """
    n = _n()
    n.user32.UnregisterClassW(n.class_name, n.hinst)


def _create_window(atom: Any) -> Any:
    """``CreateWindowExW`` with the overlay ex-style set; NULL raises (catalog C4).

    ``atom`` is accepted for signature-compatibility but unused — the class *name*
    is the ``lpClassName`` (works for both a fresh atom and the F1b reuse path).
    The initial rect is the bottom-centre placement; ``UpdateLayeredWindow``
    repositions the window on every blit thereafter.
    """
    n = _n()
    x, y, w, h = _placement(_PANEL_W, _PANEL_H)
    hwnd = n.user32.CreateWindowExW(
        _OVERLAY_EX_STYLE,
        n.class_name,
        "seda-hud",
        WS_POPUP,
        x,
        y,
        w,
        h,
        None,
        None,
        n.hinst,
        None,
    )
    if not hwnd:
        raise _win_error()  # C4
    return hwnd


def _destroy_window(hwnd: Any) -> None:
    """``DestroyWindow`` (teardown, catalog F1). Dispatches ``WM_DESTROY`` into
    the WNDPROC synchronously — the ``_wndproc_ref`` must still be alive here."""
    n = _n()
    if hwnd is not None and n.user32.IsWindow(hwnd):
        n.user32.DestroyWindow(hwnd)


def _create_dib(w: int, h: int) -> tuple[Any, Any, Any]:
    """``CreateDIBSection`` (top-down 32-bit ARGB) + a memory DC; NULL raises (C5).

    Returns ``(dib, hdc, bits)`` — ``bits`` is the ``c_void_p`` OUT pixel pointer
    (its ``.value`` is the pixel address the premultiply walk views). ``biHeight``
    is NEGATIVE so row 0 is the top row (screen orientation).

    ATOMICITY (catalog C0): the outer :func:`build_overlay` undo is not armed
    until this returns, so this shim disposes its OWN partial allocations (DC and
    DIB) on a mid-build failure before re-raising.
    """
    n = _n()
    bmi = n.BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(n.BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # NEGATIVE = top-down (CRITICAL)
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    bits = ctypes.c_void_p(0)
    screen_dc = n.user32.GetDC(None)
    dib = n.gdi32.CreateDIBSection(
        screen_dc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
    )
    n.user32.ReleaseDC(None, screen_dc)
    if not dib or not bits.value:
        raise _win_error()  # C5 — nothing allocated to leak
    hdc = n.gdi32.CreateCompatibleDC(None)
    if not hdc:
        n.gdi32.DeleteObject(dib)  # atomicity: undo the DIB just made
        raise _win_error()
    if not n.gdi32.SelectObject(hdc, dib):  # NULL/HGDI_ERROR -> select failed
        n.gdi32.DeleteDC(hdc)
        n.gdi32.DeleteObject(dib)  # atomicity: undo DC + DIB
        raise _win_error()
    return dib, hdc, bits


def _gdip_create_from_hdc(hdc: Any) -> Any:
    """``GdipCreateFromHDC`` + smoothing/compositing mode; fail raises (catalog C6)."""
    n = _n()
    g = GpHandle()
    st = n.gdiplus.GdipCreateFromHDC(hdc, ctypes.byref(g))
    if st != GDIP_OK:
        raise OSError(f"GdipCreateFromHDC status={st}")  # C6
    n.gdiplus.GdipSetSmoothingMode(g, SMOOTHING_MODE_ANTIALIAS)
    # PARITY-SPEC DISCREPANCY: docs/specs/windows-hud-parity.md Part 1 says SourceOver.
    # That is STALE. #66 hardware finding + memory:windows-hud-gdiplus-sourcecopy: SourceOver
    # composites the translucent card against a DIB treated as opaque -> the card interior
    # renders opaque black. SourceCopy (1) writes brush ARGB verbatim so card a=0.55 lands;
    # AA edges still get fractional-coverage alpha copied in, so rounded corners stay smooth.
    n.gdiplus.GdipSetCompositingMode(g, COMPOSITING_MODE_SOURCE_COPY)
    return g


def _paint(backbuffer: dict[str, Any], state: dict[str, Any]) -> None:
    """Render the card + bars into the backbuffer (catalog E1).

    A 1:1 port of ``WaveformView.drawRect_`` (parity spec Parts 2-3): rounded card
    (radius 8, black alpha 0.55) then the mode's content. IDLE draws the compressed
    pill (the #56 look); LISTENING draws the 9 mirror-EQ bars; BUSY the time-driven
    sweep — all constants verbatim from macOS / the shared knobs. Draws in straight
    alpha (SourceCopy writes it verbatim); :func:`_blit` premultiplies before
    ``UpdateLayeredWindow``.

    IDLE renders into a 48×24 top-left sub-region of the DIB (the panel-shrink);
    LISTENING/BUSY fill the active 160×48. :func:`_blit` copies only the mode's
    ``_panel_size`` sub-rect via ``UpdateLayeredWindow`` (``psize`` from the geom
    tuple, ``ptSrc=(0,0)``), sizing the window to match — so the shrink/grow is
    dimension-matched in one blit and never tears. The card + content are laid out
    against the *panel* size, not the fixed DIB size.

    Brushes/paths are created per-frame and freed in ``finally`` so a raising
    fill never leaks a GDI+ handle (60 Hz create/delete is negligible).
    """
    import math

    n = _n()
    gp = n.gdiplus
    g = backbuffer["graphics"]
    mode = state["mode"]
    # Lay out against the mode's on-screen panel size (the shrunk 48×24 in IDLE),
    # a top-left sub-region of the fixed 160×48 DIB. _blit copies just this rect.
    w, h = _panel_size(mode)

    # 1) Clear the FULL DIB to fully transparent (SourceCopy writes 0x00000000
    #    verbatim). Clearing everything — not just the sub-rect — keeps stale
    #    pixels from a prior larger frame out of any future blit. premultiply of
    #    a==0 leaves 0,0,0,0.
    gp.GdipGraphicsClear(g, 0x00000000)

    card = GpHandle()
    bar = GpHandle()
    pill = GpHandle()
    path = GpHandle()
    try:
        # 2) CARD: rounded rect over the panel sub-region. Integer arcs (integral).
        gp.GdipCreatePath(0, ctypes.byref(path))  # FillMode Alternate
        _add_round_rect_i(gp, path, 0, 0, w, h, 8)
        gp.GdipCreateSolidFill(CARD_ARGB, ctypes.byref(card))
        gp.GdipFillPath(g, card, path)
        gp.GdipDeletePath(path)
        path = GpHandle()

        # 3) BARS: shared geometry (drawRect_) so LISTENING<->BUSY reads as one widget.
        # PARITY DELTA (accepted, #66): under SourceCopy each bar fill REPLACES the card
        # pixels it covers rather than compositing OVER them as macOS SourceOver does, so
        # bars are pure 0xEBFFFFFF (no faint card tint). The #66 spike shipped exactly this
        # bars-on-card SourceCopy path and passed the by-eye verdict — accepted, not a bug.
        n_bars, bw, gap = 9, 6, 3
        cluster = n_bars * bw + (n_bars - 1) * gap
        x0 = (w - cluster) / 2.0
        cy = h / 2.0
        span = h * 0.42  # half-height at full amplitude
        frame = state["frame"]
        phase = frame / 60.0  # seconds-ish, as on macOS

        if mode == HudMode.IDLE:
            # Idle: a single compressed pill with a faint slow alpha breath (#56).
            # Shimmer is a shared knob (ADR-0007 §5) so macOS + Windows breathe
            # identically. Centered in the shrunk 48×24 panel sub-region (w,h here
            # are the IDLE panel size, a top-left sub-rect of the 160×48 DIB).
            alpha = hud_idle_shimmer(frame, HudMode.IDLE)
            gp.GdipCreateSolidFill((round(alpha * 255) << 24) | 0x00FFFFFF, ctypes.byref(pill))
            _fill_pill(gp, g, pill, w / 2.0, cy, float(HUD_IDLE_PILL_W), float(HUD_IDLE_PILL_H))
        elif mode == HudMode.BUSY:
            # Busy: a bright band sweeps L->R over a calm baseline (time-driven; no mic).
            speed = 3.2  # bars/sec the head travels
            head = (phase * speed) % (n_bars + 3)  # +3 = gap between sweeps
            for i in range(n_bars):
                d = i - head
                bump = math.exp(-(d * d) / 2.2)
                hh = 0.18 + 0.62 * bump  # baseline + travelling swell
                half = max(2.0, span * hh)
                alpha = 0.45 + 0.47 * bump
                busy_bar = GpHandle()
                gp.GdipCreateSolidFill(
                    (round(alpha * 255) << 24) | 0x00FFFFFF, ctypes.byref(busy_bar)
                )
                try:
                    _fill_bar(gp, g, busy_bar, x0 + i * (bw + gap), cy, half, bw)
                finally:
                    gp.GdipDeleteBrush(busy_bar)
        else:
            # Listening: symmetric mirror EQ driven by the mic level. GATE/GAIN + sqrt
            # expand so quiet speech visibly moves the bars; raw/instant (no smoothing).
            gate, gain = 0.006, 2.6
            rms = float(state["level"])
            level = max(0.0, min(1.0, math.sqrt(max(0.0, rms - gate)) * gain))
            gp.GdipCreateSolidFill(BAR_ARGB, ctypes.byref(bar))  # constant alpha 0.92
            for i in range(n_bars):
                # Triangular weight: 1.0 at centre, ~0.35 at edges.
                weight = 0.35 + 0.65 * (1.0 - abs(i - (n_bars - 1) / 2.0) / (n_bars / 2.0))
                # Jitter scales with level so it vanishes at silence.
                jitter = 1.0 + 0.3 * level * math.sin(phase * 9.0 + i)
                half = max(2.0, span * level * weight * jitter)
                _fill_bar(gp, g, bar, x0 + i * (bw + gap), cy, half, bw)

        # 4) FLUSH so GDI+ commits all fills to the DIB before _blit premultiplies it.
        gp.GdipFlush(g, FLUSH_INTENTION_SYNC)
    finally:
        if path:
            gp.GdipDeletePath(path)
        if card:
            gp.GdipDeleteBrush(card)
        if bar:
            gp.GdipDeleteBrush(bar)
        if pill:
            gp.GdipDeleteBrush(pill)


def _blit(hwnd: Any, backbuffer: dict[str, Any], geom: tuple[int, int, int, int]) -> None:
    """``UpdateLayeredWindow`` (``ULW_ALPHA``, premultiplied) — first blit C7 / runtime E1.

    Premultiply the freshly-drawn DIB, then blit it at the placement origin. With
    ``UpdateLayeredWindow`` the blit IS the paint (no ``WM_PAINT``).
    """
    n = _n()
    _premultiply(backbuffer)  # walk the DIB bits before the ULW (kills halos)
    x, y, w, h = geom
    size = n.SIZE(w, h)
    src = n.wt.POINT(0, 0)
    dst = n.wt.POINT(x, y)
    blend = n.BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
    ok = n.user32.UpdateLayeredWindow(
        hwnd,
        None,  # hdcDst (NULL)
        ctypes.byref(dst),
        ctypes.byref(size),
        backbuffer["hdc"],  # hdcSrc (memory DC with the DIB)
        ctypes.byref(src),
        0,  # crKey
        ctypes.byref(blend),
        ULW_ALPHA,
    )
    if not ok:
        raise _win_error()  # C7 at build; runtime tick escape guarded by pump


def _show_window(hwnd: Any, *, first: bool) -> None:
    """``ShowWindow`` without activating: ``SW_SHOWNOACTIVATE`` first, ``SW_SHOWNA`` after.

    On the first show, force topmost via ``SetWindowPos`` (never
    ``SetForegroundWindow``) so the click-through overlay floats over everything.
    """
    n = _n()
    n.user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE if first else SW_SHOWNA)
    if first:
        n.user32.SetWindowPos(
            hwnd,
            ctypes.c_void_p(HWND_TOPMOST),
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )


def _hide_window(hwnd: Any) -> None:
    """``ShowWindow(SW_HIDE)``."""
    _n().user32.ShowWindow(hwnd, SW_HIDE)


def _set_window_pos(hwnd: Any, geom: tuple[int, int, int, int]) -> None:
    """``SetWindowPos(SWP_NOACTIVATE | SWP_NOZORDER)`` for the panel-shrink (catalog E4)."""
    n = _n()
    x, y, w, h = geom
    n.user32.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOACTIVATE | SWP_NOZORDER)


def _set_timer(hwnd: Any, timer_id: int, interval_ms: int) -> int:
    """``SetTimer``; returns the timer id (initial arm C-series / re-arm E2)."""
    n = _n()
    tid = timer_id or 1
    n.user32.SetTimer(hwnd, ctypes.c_void_p(tid), interval_ms, None)
    return tid


def _kill_timer(hwnd: Any, timer_id: int) -> None:
    """``KillTimer`` (teardown, catalog F1).

    Runs first in teardown, so it purges pending ``WM_TIMER``s for this timer from
    the queue — no late ``WM_TIMER`` dispatches into a freed DIB/HWND, which is
    what makes the unguarded ``_tick`` safe at teardown.
    """
    n = _n()
    if hwnd is not None:
        n.user32.KillTimer(hwnd, ctypes.c_void_p(timer_id or 1))


def _free_dib(backbuffer: dict[str, Any]) -> None:
    """Delete the GDI+ graphics object + DIB section + memory DC (teardown, catalog F1).

    Order matters: the graphics wraps the DC, so delete it FIRST, then the DIB
    (freed explicitly by ``DeleteObject``), then the memory DC. Each field is
    nulled after deletion so a double teardown is a harmless no-op (idempotency).
    The stock 1×1 bitmap the DC still has selected is system-owned; ``DeleteDC``
    releases the association without deleting our DIB.
    """
    n = _n()
    g = backbuffer.get("graphics")
    if g:
        n.gdiplus.GdipDeleteGraphics(g)
    backbuffer["graphics"] = None
    dib = backbuffer.get("dib")
    if dib:
        n.gdi32.DeleteObject(dib)
    backbuffer["dib"] = None
    hdc = backbuffer.get("hdc")
    if hdc:
        n.gdi32.DeleteDC(hdc)
    backbuffer["hdc"] = None


def _monitor_geometry() -> tuple[int, int, int, int]:
    """Primary-monitor work area ``(left, top, right, bottom)`` (catalog E7).

    ``SystemParametersInfoW(SPI_GETWORKAREA)`` excludes the taskbar, so the HUD
    sits above it. A failure raises → :func:`_placement` swallows and keeps the
    last-good area.
    """
    n = _n()
    r = n.wt.RECT()
    if not n.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
        raise _win_error()  # E7
    return r.left, r.top, r.right, r.bottom


# Last-good monitor work area, so a runtime geometry query failure (E7: RDP,
# headless, hotplug) keeps the last position instead of dropping the HUD.
_last_work_area: tuple[int, int, int, int] = (0, 0, 1920, 1080)


def _pump_once() -> None:
    """One pump drain: ``PeekMessageW(PM_REMOVE)`` + translate + dispatch (ADR-0008 §2).

    Drains all currently-queued messages (each ``WM_TIMER`` runs the redraw tick
    through the WNDPROC); returns when the queue is empty so the caller can service
    the stop flag + dispatch queue and sleep.
    """
    n = _n()
    msg = n.wt.MSG()
    while n.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
        n.user32.TranslateMessage(ctypes.byref(msg))
        n.user32.DispatchMessageW(ctypes.byref(msg))


def _sleep(seconds: float) -> None:
    """The pump's ~5 ms sleep (ADR-0008 §2). A shim so tests can neutralize it."""
    import time

    time.sleep(seconds)


def _placement(w: int, h: int) -> tuple[int, int, int, int]:
    """Bottom-centre placement rect on the primary monitor (parity spec Part 2).

    Win32 origin is top-left, so ``y = workArea.bottom - PANEL_H - 80`` (the
    macOS ``y=80``-from-bottom, flipped). A runtime geometry-query failure (E7)
    is swallowed and the **last-good** work area is reused — the HUD keeps its
    last position rather than vanishing.
    """
    global _last_work_area
    try:
        _last_work_area = _monitor_geometry()
    except Exception:  # noqa: BLE001
        logger.debug("monitor geometry query failed; keeping last position", exc_info=True)
    left, _top, right, bottom = _last_work_area
    x = left + ((right - left) - w) // 2
    y = bottom - h - 80
    return x, y, w, h


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------


class Overlay:
    """Handle to a live Windows overlay: the four-callable ``OverlayNotifier``
    contract (ADR-0009 §1) plus Windows-private fields.

    Duplicated per host (ADR-0009 §4): the private fields genuinely differ from
    macOS. ``set_mode``/``teardown`` default to no-ops for hand-built test
    overlays — which is exactly why the conformance test must be
    *callable-and-effectful*, not an ``inspect.signature`` check.
    """

    def __init__(
        self,
        *,
        show: Callable[[], None],
        hide: Callable[[], None],
        dispatch_main: Callable[[Callable[[], None]], None],
        set_mode: Callable[[HudMode], None] | None = None,
        teardown: Callable[[], None] | None = None,
        _hwnd: Any = None,
        _atom: Any = None,
        _hinst: Any = None,
        _timer_id: int = 0,
        _wndproc_ref: Any = None,
        _gdiplus_token: Any = None,
        _backbuffer: dict[str, Any] | None = None,
        _queue: queue.Queue[Callable[[], None]] | None = None,
        _state: dict[str, Any] | None = None,
        _first_shown: dict[str, bool] | None = None,
    ) -> None:
        self.show = show
        self.hide = hide
        self.dispatch_main = dispatch_main
        self.set_mode = set_mode if set_mode is not None else (lambda _mode: None)
        self.teardown = teardown if teardown is not None else (lambda: None)
        self._hwnd = _hwnd
        self._atom = _atom
        self._hinst = _hinst
        self._timer_id = _timer_id
        # GC-keepalive: the WNDPROC ctypes object must stay reachable until AFTER
        # the window is destroyed (WM_DESTROY dispatches into it during the call).
        self._wndproc_ref = _wndproc_ref
        self._gdiplus_token = _gdiplus_token
        self._backbuffer = _backbuffer
        self._queue = _queue
        self._state = _state
        self._first_shown = _first_shown if _first_shown is not None else {"flag": False}


def build_overlay(level_source: Callable[[], float]) -> Overlay:
    """Build the Windows overlay (transactional; fail-open catalog §3 C0).

    The **only** call inside :func:`run_hosted`'s fail-open try. Native loading is
    lazy (:func:`_load_libs`), so on a non-Windows host it raises
    ``OSError``/``AttributeError`` (catalog B2) and the caller fails open. The
    seven native build steps (fail-open §3) run in order; on **any** partial
    failure the already-allocated resources are disposed **in reverse order** and
    the exception re-raised — a failed build leaks nothing (GDI+ token, class
    atom, HWND, DIB). This is the build-time twin of teardown's ``finally``.

    *level_source* is polled on the pump thread to drive the meter (it is
    ``AppController.latest_level``).
    """
    _load_libs()  # lazy; declares prototypes. Non-Windows -> raises -> fail open (B2).

    state: dict[str, Any] = {"level": 0.0, "frame": 0, "mode": HudMode.IDLE}
    first_shown = {"flag": False}
    w, h = _PANEL_W, _PANEL_H  # the DIB is always the max (active) size; IDLE draws a sub-rect

    # LIFO of guarded dispose thunks for the transactional unwind (C0). Each
    # allocation appends its undo immediately after it succeeds, so a failure at
    # step N unwinds exactly steps 1..N-1 in reverse.
    undo: list[Callable[[], None]] = []
    # wndproc_ref stays a local (referenced) through the whole build; on failure
    # it must still be alive when _destroy_window runs its WM_DESTROY dispatch.
    wndproc_ref = _make_wndproc(state)
    hinst: Any = None  # the real HINSTANCE is cached in _load_libs; shims read _NATIVE.hinst
    try:
        token = _gdiplus_startup()  # 1  (C2)
        undo.append(lambda: _gdiplus_shutdown(token))

        _set_dpi_awareness()  # 2  (C1/C1b — benign swallowed inside the shim; allocates nothing)

        atom = _register_class(wndproc_ref)  # 3  (C3 / F1b)
        undo.append(lambda: _unregister_class(atom, hinst))

        hwnd = _create_window(atom)  # 4  (C4)
        undo.append(lambda: _destroy_window(hwnd))

        dib, hdc, bits = _create_dib(w, h)  # 5  (C5)
        backbuffer: dict[str, Any] = {
            "dib": dib,
            "hdc": hdc,
            "bits": bits,
            "graphics": None,
            "w": w,
            "h": h,
        }
        undo.append(lambda: _free_dib(backbuffer))

        backbuffer["graphics"] = _gdip_create_from_hdc(hdc)  # 6  (C6)

        _paint(backbuffer, state)  # first frame drawn into the DIB (IDLE at startup)
        # First blit at the startup mode's panel size (IDLE → 48×24 sub-rect).
        _blit(hwnd, backbuffer, _placement(*_panel_size(state["mode"])))  # 7  (C7)

        # D0: the initial timer arm sits pre-boundary, INSIDE build, so a failure
        # here is a plain build failure that run_hosted fails open (the frozen
        # run_hosted always returns True after run_loop, so D0 cannot fail open
        # any other way — HITL-confirmed).
        timer_id = _set_timer(hwnd, 0, _interval_ms(state["mode"]))
    except BaseException:
        # Reverse-order dispose of everything allocated so far, then re-raise.
        # wndproc_ref is still a live local, so _destroy_window's WM_DESTROY
        # dispatch is safe. Each dispose is guarded so unwind never masks the
        # original error.
        for dispose in reversed(undo):
            try:
                dispose()
            except Exception:  # noqa: BLE001
                logger.debug("overlay build unwind step failed", exc_info=True)
        raise

    q: queue.Queue[Callable[[], None]] = queue.Queue(maxsize=_DISPATCH_QUEUE_MAX)

    def _dispatch(fn: Callable[[], None]) -> None:
        # Producer side: runs on listener/worker threads. Fire-and-forget onto a
        # bounded queue (parity Part 4, fail-open E6). Never block a live producer
        # if the pump is dead — drop the newest frame and log.
        try:
            q.put_nowait(fn)
        except queue.Full:
            logger.warning("overlay dispatch queue full; dropping frame")

    def _show() -> None:
        _show_window(hwnd, first=not first_shown["flag"])
        first_shown["flag"] = True

    def _hide() -> None:
        _hide_window(hwnd)

    def _set_mode(mode: HudMode) -> None:
        # Runs on the pump thread (marshalled via _dispatch), so mutating _state
        # is single-threaded. Re-arm the redraw timer at the mode's rate (ADR-0007
        # §5: ~10 Hz idle / ~60 Hz active) and redraw immediately so the mode flip
        # is visible now, not up to one idle interval later. A failed re-arm or
        # draw is swallowed (E1/E2 — a static/last-frame HUD beats a broken run).
        state["mode"] = mode
        try:
            _set_timer(hwnd, timer_id, _interval_ms(mode))
        except Exception:  # noqa: BLE001
            logger.debug("overlay timer re-arm failed", exc_info=True)
        try:
            tick = state.get("tick")
            if tick is not None:
                tick()  # immediate repaint at the new mode (analog of setNeedsDisplay_)
        except Exception:  # noqa: BLE001
            logger.debug("overlay set_mode redraw failed", exc_info=True)
        # Panel-shrink (#79): the immediate redraw above (and every _tick) blits at
        # _panel_size(mode) — 48×24 in IDLE, 160×48 active — so the window resizes to
        # match the sub-rect _paint drew, dimension-matched in the one ULW blit (no
        # tear, no separate SetWindowPos). E4/E4b.

    def _teardown() -> None:
        # Deterministic, idempotent, fail-open teardown (ADR-0008 §4, parity
        # Part 4). Order: KillTimer -> DestroyWindow -> UnregisterClassW ->
        # free DIB -> GdiplusShutdown. Each step guarded so one failure never
        # skips the rest (F1) and never masks a real crash (F4). The window is
        # destroyed BEFORE wndproc_ref can be dropped (held on the Overlay).
        steps: tuple[tuple[str, Callable[[], None]], ...] = (
            ("kill_timer", lambda: _kill_timer(hwnd, timer_id)),
            ("destroy_window", lambda: _destroy_window(hwnd)),
            ("unregister_class", lambda: _unregister_class(atom, hinst)),
            ("free_dib", lambda: _free_dib(backbuffer)),
            ("gdiplus_shutdown", lambda: _gdiplus_shutdown(token)),
        )
        for step, fn in steps:
            try:
                fn()
            except Exception:  # noqa: BLE001
                logger.debug("overlay teardown step %s failed", step, exc_info=True)

    def _tick() -> None:
        # WM_TIMER body (guarded, E1): sample the level and re-blit. Runs on the
        # pump thread. Draw failures are swallowed so the pump survives. The blit
        # size follows the current mode (IDLE shrinks to 48×24) — dimension-matched
        # with the sub-region _paint drew, in this one blit, so it never tears.
        state["level"] = level_source()
        state["frame"] = state["frame"] + 1
        _paint(backbuffer, state)
        _blit(hwnd, backbuffer, _placement(*_panel_size(state["mode"])))

    backbuffer["tick"] = _tick  # kept reachable for the WM_TIMER path (Step 4)
    # WNDPROC (built at line ~349 with only `state`) reaches the redraw via
    # state["tick"]. _tick is defined after backbuffer, so mirror it onto the same
    # state dict the WNDPROC captured by reference — WM_TIMER can then call it.
    # (No T1 test asserts state-key exclusivity — verified against test_gui_host_win.)
    state["tick"] = _tick

    return Overlay(
        show=_show,
        hide=_hide,
        dispatch_main=_dispatch,
        set_mode=_set_mode,
        teardown=_teardown,
        _hwnd=hwnd,
        _atom=atom,
        _hinst=hinst,
        _timer_id=timer_id,
        _wndproc_ref=wndproc_ref,
        _gdiplus_token=token,
        _backbuffer=backbuffer,
        _queue=q,
        _state=state,
        _first_shown=first_shown,
    )


def run_with_overlay(
    controller: AppController,
    *,
    build: Callable[[Callable[[], float]], Overlay] | None = None,
    register_overlay: Callable[[Overlay], None] | None = None,
    platform: str | None = None,
) -> bool:
    """Run *controller* under a Windows GUI host that owns the main thread.

    Thin adapter over the shared :func:`seda.gui._hostloop.run_hosted` (ADR-0009
    §2): supplies the ``win32`` gate, the transactional :func:`build_overlay`, and
    the :func:`_run_win32_loop` body. Signature matches the macOS
    :func:`seda.gui.host.run_with_overlay` because ``cli.run`` calls
    ``module.run_with_overlay(controller, register_overlay=...)`` on either host.

    Returns ``True`` only if the host took over the main thread and ran the
    controller to shutdown; ``False`` (fail-open) when the overlay is unavailable
    — non-Windows or a Win32/GDI+ build failure — so the caller falls back to
    ``controller.run()``.
    """
    from seda.gui._hostloop import run_hosted

    build_fn = build if build is not None else build_overlay

    return run_hosted(
        controller,
        supports=lambda plat: plat == "win32",
        build=build_fn,
        run_loop=_run_win32_loop,
        register_overlay=register_overlay,
        platform=platform,
    )


def _run_win32_loop(
    controller: AppController,
    overlay: Overlay,
    register_overlay: Callable[[Overlay], None] | None,
) -> None:
    """Windows ``run_loop`` body for :func:`run_hosted` (past the fail-open boundary).

    Mirrors the macOS ``_run_appkit_host`` shape (ADR-0009 §2): register the
    overlay, install signal handlers, start the controller (a failure here is the
    controller's own and PROPAGATES — the boundary is *before* this, in
    ``run_hosted``), then run the interruptible pump (ADR-0008 §2), and tear the
    overlay down in a ``finally`` on every exit.

    Divergence from macOS: no separate pump timer — the ``PeekMessageW`` pump
    *is* the servicing loop (ADR-0008 §2), and the stop flag is a bare loop-local
    the signal handler sets (§3).
    """
    if register_overlay is not None:
        register_overlay(overlay)

    # Loop-local stop flag (matches macOS's loop-local stop_requested — no
    # module-level singleton, so nothing leaks across runs/tests). The signal
    # handler does the one thing a handler should: record the request.
    stop_requested = {"flag": False}

    def _request_stop(_signum: int, _frame: Any) -> None:
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    q = overlay._queue

    try:
        # Non-blocking setup: load model, start hotkeys, notify READY. A failure
        # here PROPAGATES (D1) — falling back would re-run start() and fail again.
        controller.start()
        while not stop_requested["flag"]:
            _pump_once()  # PeekMessageW(PM_REMOVE) drain; WM_TIMER -> _tick (guarded, E1)
            _drain(q)  # run enqueued dispatch_main closures (E3)
            _sleep(_PUMP_SLEEP_SECONDS)
        # Normal stop path: quiesce the controller BEFORE the window vanishes, so
        # nothing reacts to a half-torn-down window (ADR-0008 §4). Inside the try,
        # after the loop breaks — NOT in the finally (which is teardown only).
        controller.shutdown()
    finally:
        # Tear the overlay down on EVERY exit — normal stop, a controller.start()
        # crash (D1/F2), or a pump raise (D2/F3). teardown() is itself fail-open,
        # but guard again so a teardown error never masks the original crash (F4).
        try:
            overlay.teardown()
        except Exception:  # noqa: BLE001
            logger.warning("overlay teardown failed during shutdown", exc_info=True)


def _drain(q: queue.Queue[Callable[[], None]] | None) -> None:
    """Drain and run every queued ``dispatch_main`` closure (pump thread, E3).

    Each closure is guarded so a raising one never stops the drain — a broken
    overlay callback degrades to "no HUD update", dictation intact.
    """
    if q is None:
        return
    while True:
        try:
            fn = q.get_nowait()
        except queue.Empty:
            return
        try:
            fn()
        except Exception:  # noqa: BLE001
            logger.warning("dispatched overlay closure failed", exc_info=True)
