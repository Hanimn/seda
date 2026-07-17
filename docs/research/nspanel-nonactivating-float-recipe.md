# NSPanel non-activating float recipe (issue #17)

Research date: 2026-07-17. Sources are PRIMARY: Apple's AppKit/Foundation developer
documentation (`developer.apple.com/documentation/appkit`, rendered from the
`developer.apple.com/tutorials/data/*.json` doc API — same content as the HTML pages),
Apple's archived Threading Programming Guide, the PyObjC docs
(`pyobjc.readthedocs.io`), and the `seda` repo itself for the run-loop/audio-level
grounding. Explicit statements from those sources are `[sourced]`; claims reasoned from
sourced facts + the repo are `[inferred]`; things a `/prototype` must confirm are
`[uncertain]`.

Scope: findings only. No production code or config was changed. This file is left
untracked for review.

> **Blocked-by note:** issue #17 is **blocked by #16** (main-thread inversion:
> `NSApplication.run()` vs the current `AppController.run()`). Item 6 and item 7 below
> are the findings that decide #16 — see the **"Findings that feed issue #16"** section
> at the end. In short: **AppKit forces the main thread to own the run loop**, which the
> current `AppController.run()` (blocking on a `threading.Event`) does not provide.

---

## Summary for our design (most relevant recipe decisions)

- **Use `NSPanel`, not `NSWindow`, with style mask
  `NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel`.** The
  non-activating-panel bit is the one documented flag whose whole purpose is "a panel …
  that does not activate the owning app" — exactly the no-focus-theft requirement.
  `[sourced]`
- **Belt-and-braces the focus rule: subclass the panel and override
  `canBecomeKeyWindow` / `canBecomeMainWindow` to return `False`.** A borderless window
  is already non-key/non-main by default, but the override makes it explicit and
  immune to any later style change. `[sourced]`
- **Show it with `orderFrontRegardless()`, never `makeKeyAndOrderFront_`.**
  `orderFrontRegardless` is documented to move the window to the front of its level
  "even if its application isn't active, **without changing either the key window or the
  main window**" — this is the show-without-stealing-focus primitive. `[sourced]`
- **Float over everything (incl. full-screen apps) with
  `level = NSStatusWindowLevel` (or `NSScreenSaverWindowLevel` if it must sit above
  status items) plus `collectionBehavior = canJoinAllSpaces | stationary |
  fullScreenAuxiliary | ignoresCycle`.** Levels stack strictly (a higher level always
  obscures a lower one), and the collection-behavior flags put the panel on every Space
  and over full-screen windows without joining Mission Control cycling. `[sourced]`
- **Transparency = `setOpaque_(False)` + `backgroundColor = NSColor.clearColor()` +
  `setHasShadow_(False)`**, then draw a translucent rounded rect in a **layer-backed**
  content `NSView` (or its `CALayer`). `[sourced]`
- **Run the app as an accessory agent:
  `NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)`** (equivalent to
  `LSUIElement=1`) so it has no Dock icon / menu bar and won't grab activation; do **not**
  call `NSApp.activate(ignoringOtherApps:)`. `[sourced]`
- **Make the HUD click-through with `setIgnoresMouseEvents_(True)`** — it's a pure
  feedback surface, so mouse events should pass to the app underneath, which also removes
  the one remaining path by which a panel could take focus on click. `[sourced/inferred]`
- **All AppKit work (creating the panel, `setNeedsDisplay_`, `draw_`) MUST happen on
  the main thread**, driven by a main-run-loop `NSTimer` (or GCD dispatch to the main
  queue / `performSelectorOnMainThread_`) that reads the latest audio level. This is the
  hard constraint that forces the #16 decision. `[sourced]`

---

## 1. Style mask — why NSPanel, borderless + non-activating

- **`NSWindowStyleMaskNonactivatingPanel`**: "The window is a panel or a subclass of
  [NSPanel] that does **not activate the owning app**." This single flag is the core of
  the recipe — it lets the panel come forward and receive its own events without
  `NSApp` activating (which would deactivate and steal focus from the app being dictated
  into). `[sourced]`
  <https://developer.apple.com/documentation/appkit/nswindow/stylemask-swift.struct/nonactivatingpanel>
- **`NSWindowStyleMaskBorderless`**: "The window displays none of the usual peripheral
  elements. Useful only for display or caching purposes. A window that uses
  `NSWindowStyleMaskBorderless` **can't become key or main, unless the value of
  [`canBecomeKeyWindow`] or [`canBecomeMainWindow`] is [true]**." So borderless gives us
  the chrome-free HUD *and* a default no-key/no-main posture for free. `[sourced]`
  <https://developer.apple.com/documentation/appkit/nswindow/stylemask-swift.struct/borderless>
- **Why `NSPanel` not `NSWindow`:** `NSPanel` is "A special kind of window that
  typically performs a function that is auxiliary to the main window." The
  non-activating-panel style mask and the "float above other windows" panel semantics
  (`isFloatingPanel`, `becomesKeyOnlyIfNeeded`) live on the panel side of the hierarchy;
  `NSWindow` has no non-activating behavior. `[sourced]`
  <https://developer.apple.com/documentation/appkit/nspanel>
- **Combine the two** as a bitmask: in PyObjC that's
  `NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel`. (The old names
  `NSBorderlessWindowMask` / `NSNonactivatingPanelMask` are the pre-10.12 spellings of
  the same bits; use the `StyleMask` names.) `[inferred]`
- Utility-panel flags such as `NSWindowStyleMaskUtilityWindow` / `HUDWindow` add a
  small title bar / HUD chrome; **we don't want them** for a borderless feedback HUD —
  they'd re-introduce peripheral elements. `[inferred]`

## 2. Window level — float over everything, including full-screen apps

- Levels are documented as a strict stack: "The stacking of levels takes precedence over
  the stacking of windows within each level. That is, even the bottom window in a level
  will obscure the top window of the next level down. Levels are listed in order from
  lowest to highest." And on the property: "Floating windows … appear in front of all
  normal-level windows." `[sourced]`
  <https://developer.apple.com/documentation/appkit/nswindow/level-swift.struct>
  <https://developer.apple.com/documentation/appkit/nswindow/level-swift.property>
- Documented abstracts of the relevant constants (ascending): `NSNormalWindowLevel`
  ("The default level for NSWindow objects") < `NSFloatingWindowLevel` ("Useful for
  floating palettes") < `NSModalPanelWindowLevel` ("The level for a modal panel") <
  `NSStatusWindowLevel` ("The level for a status window") < `NSScreenSaverWindowLevel`
  ("The level for a screen saver"). `[sourced]`
  <https://developer.apple.com/documentation/appkit/nswindow/level-swift.struct/floating>
  <https://developer.apple.com/documentation/appkit/nswindow/level-swift.struct/statusbar>
  <https://developer.apple.com/documentation/appkit/nswindow/level-swift.struct/screensaver>
- **Tradeoff / recommendation:**
  - `NSFloatingWindowLevel` floats over normal windows but sits *below* menu-bar/status
    UI and can be occluded by other floating palettes; too low for a "over everything"
    HUD. `[inferred]`
  - **`NSStatusWindowLevel`** is the pragmatic default: it's the level Apple uses for
    status items, high enough to sit over ordinary and floating windows. `[inferred]`
  - **`NSScreenSaverWindowLevel`** is the highest of these; use only if the HUD must sit
    above status-bar UI too. It's aggressive (screen-saver height) and can cover system
    overlays, so prefer `NSStatusWindowLevel` unless a prototype shows it's occluded.
    `[inferred]`
  - **Note on full-screen:** the *level* alone does not guarantee visibility over a
    full-screen (Space-hosted) app — that's what `collectionBehavior`
    `fullScreenAuxiliary` is for (item 3). Level controls z-order *within* the Space;
    collection behavior controls *which Spaces* the panel appears on. Both are needed.
    `[sourced/inferred]`

## 3. collectionBehavior — all Spaces + over full-screen

`collectionBehavior` is "A value that identifies the window's behavior in window
collections"; set it to the OR of these documented options. `[sourced]`
<https://developer.apple.com/documentation/appkit/nswindow/collectionbehavior-swift.property>

- **`NSWindowCollectionBehaviorCanJoinAllSpaces`** — "The window can appear in all
  spaces. The menu bar behaves this way." → the HUD shows regardless of which Space the
  user switches to. `[sourced]`
  <https://developer.apple.com/documentation/appkit/nswindow/collectionbehavior-swift.struct/canjoinallspaces>
- **`NSWindowCollectionBehaviorStationary`** — "Mission Control doesn't affect the
  window, so it stays visible and stationary, like the desktop window." → the HUD isn't
  swept up / repositioned by Mission Control. `[sourced]`
  <https://developer.apple.com/documentation/appkit/nswindow/collectionbehavior-swift.struct/stationary>
- **`NSWindowCollectionBehaviorFullScreenAuxiliary`** — "The window displays on the same
  space as the full screen window." → this is the flag that lets the HUD appear over a
  full-screen app (e.g. full-screen browser/editor being dictated into). `[sourced]`
  <https://developer.apple.com/documentation/appkit/nswindow/collectionbehavior-swift.struct/fullscreenauxiliary>
- **`NSWindowCollectionBehaviorIgnoresCycle`** — "The window isn't part of the window
  cycle for use with the Cycle Through Windows menu item." → keeps the HUD out of
  Cmd-` window cycling. `[sourced]`
  <https://developer.apple.com/documentation/appkit/nswindow/collectionbehavior-swift.struct/ignorescycle>
- Aside: the default `transient` behavior ("The window floats in Spaces and hides in
  Mission Control") is "the default behavior if `windowLevel` isn't equal to
  [normal]". We override it explicitly with the flags above so the HUD is stationary and
  all-Spaces rather than transient. `[sourced/inferred]`
  <https://developer.apple.com/documentation/appkit/nswindow/collectionbehavior-swift.struct/transient>

## 4. Non-activation / no focus theft (the central requirement)

Four independent, documented mechanisms combine so the panel never steals key/main/focus:

1. **Style mask `NonactivatingPanel`** — by definition "does not activate the owning
   app" (item 1). `[sourced]`
2. **`canBecomeKeyWindow` / `canBecomeMainWindow` default to False for borderless**, and
   attempts to make them key/main are *abandoned* when these are false:
   - `canBecomeKey`: "Attempts to make the window the key window are abandoned if the
     value of this property is [false]. The value … is [true] if the window has a title
     bar or a resize bar, or [false] otherwise." A borderless HUD has neither → false.
     `[sourced]`
     <https://developer.apple.com/documentation/appkit/nswindow/canbecomekey>
   - `canBecomeMain`: "Attempts to make the window the main window are abandoned if the
     value of this property is [false]. The value … is [true] if the window is visible,
     is not an [NSPanel] object, and has a title bar or a resize mechanism. Otherwise …
     [false]." An `NSPanel` is *explicitly excluded* → false. `[sourced]`
     <https://developer.apple.com/documentation/appkit/nswindow/canbecomemain>
   - **Belt-and-braces:** still override both to return `False` in a PyObjC subclass so
     the posture can't be undone by a future style tweak. `[inferred]`
3. **`becomesKeyOnlyIfNeeded` + `NSView.needsPanelToBecomeKey`** — for a non-activating
   panel, "the panel becomes key only if the hit view returns [true] from
   [`needsPanelToBecomeKey`]. This way, a non-activating panel can control whether it
   takes keyboard focus." Our content view's `needsPanelToBecomeKey` defaults to `False`
   ("The default value of this property is [false]"), so even a click can't pull
   keyboard focus. `[sourced]`
   <https://developer.apple.com/documentation/appkit/nspanel/becomeskeyonlyifneeded>
   <https://developer.apple.com/documentation/appkit/nsview/needspaneltobecomekey>
4. **Activation policy `Accessory`** — "The application doesn't appear in the Dock and
   doesn't have a menu bar, but it may be activated programmatically or by clicking on
   one of its windows. This corresponds to … `LSUIElement` … being 1." Setting
   `NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)` keeps the whole
   process out of the Dock/menu-bar activation model. `[sourced]`
   <https://developer.apple.com/documentation/appkit/nsapplication/activationpolicy-swift.enum/accessory>
   - `NSApplicationActivationPolicyProhibited` ("doesn't appear in the Dock and **may not
     create windows** or be activated") is **too strong** — it forbids creating windows,
     so it can't host the HUD. Use `Accessory`, not `Prohibited`. `[sourced]`
     <https://developer.apple.com/documentation/appkit/nsapplication/activationpolicy-swift.enum/prohibited>
   - `setActivationPolicy_` returns a bool and "You can set any activation policy in
     macOS 10.9 and later." `[sourced]`
     <https://developer.apple.com/documentation/appkit/nsapplication/setactivationpolicy(_:)>

**Show without activating — the decisive API:** call **`orderFrontRegardless()`**, not
`makeKeyAndOrderFront_`:
- `orderFrontRegardless`: "Moves the window to the front of its level, even if its
  application isn't active, **without changing either the key window or the main
  window**." This is the documented proof (item 7) that the panel appears over another
  app's window without that app losing key/focus. `[sourced]`
  <https://developer.apple.com/documentation/appkit/nswindow/orderfrontregardless()>
- `makeKeyAndOrderFront_`: "Moves the window to the front … and **makes it the key
  window**" — the opposite of what we want; it would try to take key status (abandoned
  for our panel, but still the wrong call). Avoid it. `[sourced]`
  <https://developer.apple.com/documentation/appkit/nswindow/makekeyandorderfront(_:)>
- **Do NOT call `NSApp.activate(ignoringOtherApps:)`** — "Makes the receiver the active
  app … You don't need to send this message to make one of the app's NSWindows key."
  Calling it would activate our process and deactivate the target app. `[sourced]`
  <https://developer.apple.com/documentation/appkit/nsapplication/activate(ignoringotherapps:)>

## 5. Transparency

- **`setOpaque_(False)`** — `isOpaque` is "A Boolean value that indicates whether the
  window is opaque." Must be false for any see-through content. `[sourced]`
  <https://developer.apple.com/documentation/appkit/nswindow/isopaque>
- **`setBackgroundColor_(NSColor.clearColor())`** — `backgroundColor` is "The color of
  the window's background." A clear color + non-opaque window yields a fully transparent
  window canvas onto which only your content view draws. `[sourced]`
  <https://developer.apple.com/documentation/appkit/nswindow/backgroundcolor>
- **`setHasShadow_(False)`** — `hasShadow` controls the window's drop shadow; a
  borderless translucent HUD generally wants it off so the transparent bounds don't cast
  a rectangular shadow. `[sourced]`
  <https://developer.apple.com/documentation/appkit/nswindow/hasshadow>
- **Draw a translucent rounded rect in the content view.** Two documented options:
  - **Layer-backed (recommended):** set the content view's `wantsLayer = True` — "turns
    the view into a layer-backed view … the view uses a [CALayer] object to manage its
    rendered content … more performant than redrawing the view contents explicitly."
    Then set `layer.cornerRadius` / `layer.backgroundColor` and add sub-layers for the
    bars. `[sourced]`
    <https://developer.apple.com/documentation/appkit/nsview/wantslayer>
  - **`drawRect_` (`draw(_:)`):** "Overridden by subclasses to draw the view's image
    within the specified rectangle … If the view's [isOpaque] property is [true], the
    view must completely fill the dirtyRect …" — for a translucent HUD keep the view
    non-opaque and fill with a semi-transparent `NSColor`/`NSBezierPath`. `[sourced]`
    <https://developer.apple.com/documentation/appkit/nsview/draw(_:)>
- **Click-through:** `setIgnoresMouseEvents_(True)` — "A Boolean value that indicates
  whether the window is transparent to mouse events." A pure feedback HUD should let
  clicks pass to the app underneath; this also removes the last click-to-focus path.
  `[sourced/inferred]`
  <https://developer.apple.com/documentation/appkit/nswindow/ignoresmouseevents>

## 6. Level-meter drawing & animation (drives the #16 decision)

The repo already produces the input: `audio/recorder.py` computes `peak_level` and
`_rms(...)` — one float per audio block (`AudioBuffer.peak_level`, `_rms` returning a
float32). So the HUD just needs to read "the latest level float" and redraw a few bars.
`[sourced — repo: src/local_flow/audio/recorder.py]`

**The hard rule: all AppKit UI work must happen on the main thread.** Apple's Threading
Programming Guide is explicit:
- "The main thread of the application is responsible for handling events. The main
  thread is the one **blocked in the `run` method of `NSApplication`**, usually invoked
  in an application's `main` function." `[sourced]`
- "The `NSView` class is generally not thread-safe. You should create, destroy, resize,
  move, and perform other operations on `NSView` objects **only from the main thread**."
  `NSView` and `NSCell` and all descendants are listed as **main-thread-only** classes.
  `[sourced]`
- "If a secondary thread … wants to cause portions of the view to be redrawn on the main
  thread, it must not do so using methods like `display`, `setNeedsDisplay:` … Instead,
  it should send a message to the main thread or call those methods using the
  **`performSelectorOnMainThread:withObject:waitUntilDone:`** method." `[sourced]`
  <https://developer.apple.com/library/archive/documentation/Cocoa/Conceptual/Multithreading/ThreadSafetySummary/ThreadSafetySummary.html>

**Correct main-thread redraw driver (options, in order of preference):**
- **`NSTimer` scheduled on the main run loop** (e.g. `scheduledTimerWithTimeInterval_...`
  at ~30–60 Hz while the HUD is up). "Timers work in conjunction with run loops. Run
  loops maintain strong references to their timers …" — simple, no extra framework, and
  its callback runs on the main thread that scheduled it. Recommended default. `[sourced]`
  <https://developer.apple.com/documentation/foundation/timer>
- **`CADisplayLink`** synchronizes drawing to the display refresh — "A timer object that
  allows your app to synchronize its drawing to the refresh rate of the display." It is
  the smoothest option, **but on macOS/AppKit it's only available in macOS 14+** (it was
  an iOS API historically), so it can't be the baseline for a tool targeting older
  macOS. Consider it an enhancement. `[sourced/uncertain — verify min macOS]`
  <https://developer.apple.com/documentation/quartzcore/cadisplaylink>
- **GCD dispatch to the main queue** (`dispatch_get_main_queue`) or
  **`performSelectorOnMainThread_withObject_waitUntilDone_`** — for the case where the
  audio callback thread wants to hand a level to the UI. Use these to *marshal onto* the
  main thread; do the actual `setNeedsDisplay_` there. `[sourced]`
- **Redraw request:** on each tick, read the latest level float (a plain shared
  `float`/atomic set by the audio thread — cheap, no lock needed for a single value) and
  call the content view's `setNeedsDisplay_(True)`. "View objects marked as needing
  display are automatically redisplayed on each pass through the application's event
  loop." So `setNeedsDisplay_` (main thread) → AppKit repaints on the next run-loop pass.
  `[sourced]`
  <https://developer.apple.com/documentation/appkit/nsview/setneedsdisplay(_:)>

**Consequence:** the audio thread never touches AppKit. It only updates a shared level
value; a **main-thread** timer reads it and requests redraw. This is the pattern that
requires the main thread to be running an AppKit run loop — see #16 below. `[inferred]`

## 7. Show-without-activating confirmation (documented proof)

The specific documented behavior proving the panel appears over another app's window
without that app losing key/focus:
- `orderFrontRegardless` moves the window to the front of its level "even if its
  application isn't active, **without changing either the key window or the main
  window**." (Owning-app not active + key/main unchanged = target app keeps focus.)
  `[sourced]`
  <https://developer.apple.com/documentation/appkit/nswindow/orderfrontregardless()>
- Reinforced by the `NonactivatingPanel` style mask ("does not activate the owning
  app"), by `canBecomeKey`/`canBecomeMain` returning false (attempts to make it key/main
  are "abandoned"), and by `becomesKeyOnlyIfNeeded` gating keyboard focus on the hit
  view's `needsPanelToBecomeKey` (default false). Together these are the full documented
  chain: **order-front-without-key + can't-become-key/main + non-activating panel +
  accessory policy ⇒ the dictated-into app never loses focus.** `[sourced]`

---

## PyObjC bridge specifics (how the ObjC APIs are called from Python)

- **Import:** `from AppKit import NSPanel, NSColor, NSApp, NSApplication, ...` and
  `from Foundation import NSTimer, NSMakeRect` and `import objc`. (AppKit ships in
  `pyobjc-framework-Cocoa`, already declared for this repo — see the sibling doc
  `overlay-pyobjc-dependency.md`.) `[sourced]`
- **Selector → Python name mapping:** Objective-C selectors map to Python by replacing
  every colon with an underscore, and the number of trailing/embedded underscores equals
  the argument count. So `initWithContentRect:styleMask:backing:defer:` →
  `initWithContentRect_styleMask_backing_defer_`, `setLevel:` → `setLevel_`,
  `setCollectionBehavior:` → `setCollectionBehavior_`, `setOpaque:` → `setOpaque_`,
  `setBackgroundColor:` → `setBackgroundColor_`; no-colon selectors keep their name
  (`orderFrontRegardless`, `canBecomeKeyWindow`). `[sourced]`
  <https://pyobjc.readthedocs.io/en/latest/core/intro.html>
- **Subclassing + overriding:** subclass the ObjC class with normal Python `class`
  syntax and define the override method by its bridged name; use `objc.super(...)` for
  designated initializers, which **must return `self`** (unlike Python `__init__`).
  `[sourced]`
- **Autorelease pool / run loop:** "Most of Cocoa … requires an `NSAutoreleasePool` …
  PyObjC does this automatically on the first thread it is imported from, but other
  threads will require explicit `NSAutoreleasePool` management." Another reason to keep
  all AppKit calls on the main thread. `[sourced]`
  <https://pyobjc.readthedocs.io/en/latest/core/intro.html>

### Concrete, runnable-looking PyObjC snippet

```python
# overlay_panel.py  — macOS-only; wrap import + construction in try/except to fail open.
import objc
from AppKit import (
    NSPanel, NSView, NSColor, NSApp, NSApplication, NSBezierPath,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
    NSStatusWindowLevel, NSBackingStoreBuffered,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSApplicationActivationPolicyAccessory,
)
from Foundation import NSMakeRect, NSTimer


class WaveformView(NSView):
    # A pure feedback HUD: never wants keyboard focus.
    def needsPanelToBecomeKey(self):          # -> keeps the panel from taking key on click
        return False

    def isOpaque(self):                        # translucent content
        return False

    def drawRect_(self, dirty_rect):
        # `level` is the latest audio level (0.0–1.0), set from the audio thread.
        level = getattr(self, "_level", 0.0)
        bounds = self.bounds()
        # translucent rounded backing
        NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.55).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 8.0, 8.0).fill()
        # a few level-meter bars driven by `level`
        NSColor.whiteColor().colorWithAlphaComponent_(0.9).set()
        bars, w, gap = 5, 6.0, 4.0
        for i in range(bars):
            h = max(2.0, bounds.size.height * level * (0.5 + 0.1 * i))
            x = 10.0 + i * (w + gap)
            NSBezierPath.bezierPathWithRect_(
                NSMakeRect(x, (bounds.size.height - h) / 2.0, w, h)
            ).fill()


class OverlayPanel(NSPanel):
    # Belt-and-braces: never key, never main.
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


def make_overlay():
    # Accessory policy: no Dock icon / menu bar, cannot steal activation.
    NSApp().setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    rect = NSMakeRect(0, 0, 160, 48)
    style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
    panel = OverlayPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, NSBackingStoreBuffered, False
    )
    panel.setLevel_(NSStatusWindowLevel)                 # float over normal + floating
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorFullScreenAuxiliary   # over full-screen apps
        | NSWindowCollectionBehaviorIgnoresCycle
    )
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setHasShadow_(False)
    panel.setIgnoresMouseEvents_(True)                   # click-through HUD

    view = WaveformView.alloc().initWithFrame_(rect)
    view.setWantsLayer_(True)
    panel.setContentView_(view)
    return panel, view


def show(panel):
    # Show WITHOUT taking key/main and WITHOUT activating our app.
    panel.orderFrontRegardless()      # NOT makeKeyAndOrderFront_ / NSApp.activate

def hide(panel):
    panel.orderOut_(None)

# --- main-thread redraw driver (must run on the main thread / main run loop) ---
def start_redraw(view, read_level):
    def _tick(timer):
        view._level = read_level()    # read latest float produced by the audio thread
        view.setNeedsDisplay_(True)   # AppKit repaints on next run-loop pass
    return NSTimer.scheduledTimerWithTimeInterval_repeats_block_(1.0 / 60.0, True, _tick)
```

(Constant names, `alloc().initWithContentRect_styleMask_backing_defer_`, `setLevel_`,
`setCollectionBehavior_`, `setOpaque_`, `setBackgroundColor_`, `orderFrontRegardless`,
and the `canBecomeKeyWindow` override are all as sourced above. The `NSTimer` block
variant name / exact `NSColor` convenience selectors should be confirmed by a
`/prototype` on a real machine.)

---

## Findings that feed issue #16 (main-thread inversion)

Issue #17 is **blocked by #16** because the recipe above imposes a run-loop-ownership
requirement the current architecture doesn't satisfy:

- **Today `AppController.run()` owns the main thread by blocking on a
  `threading.Event`** (`self._shutdown_event.wait()`), and pynput's hotkey callbacks run
  on a *listener* thread. `[sourced — repo: src/local_flow/app.py, input/hotkeys.py]`
- **AppKit requires the main thread to be blocked in `NSApplication.run()`** ("The main
  thread … is the one blocked in the `run` method of `NSApplication`") and requires all
  `NSView`/`NSWindow`/`NSPanel` work on that thread. `[sourced — item 6]`
- **These two are mutually exclusive on one thread.** So #16 must decide **who owns the
  main run loop**: either (a) `NSApp.run()` becomes the main-thread blocker and the
  existing controller logic is driven from AppKit callbacks / a main-queue dispatch, or
  (b) the AppKit event loop is pumped cooperatively — but option (b) fights the "main
  thread blocked in `run`" model and is not the documented path. The clean answer the
  findings point to: **`NSApplication.run()` should own the main thread; the shutdown
  `Event` wait is replaced by the AppKit run loop; hotkey/audio threads marshal UI work
  onto the main thread via `NSTimer`/`performSelectorOnMainThread_`/GCD-main.** `[inferred]`
- **The all-UI-on-main-thread rule (item 6) is the specific constraint that forces this
  inversion**, and the audio-thread-updates-a-float / main-thread-timer-redraws pattern
  is the concrete shape the #16 solution must support. `[inferred]`

---

## Open questions

- **[uncertain] Minimum macOS for `CADisplayLink` on AppKit.** The Quartz doc confirms
  the API but not the AppKit availability floor (believed macOS 14+). If seda targets
  older macOS, the baseline redraw driver must be `NSTimer`, with `CADisplayLink` as an
  opt-in enhancement. Confirm the deployment target and the API floor. `[uncertain]`
- **[uncertain] Does `NSStatusWindowLevel` reliably sit over full-screen apps with
  `fullScreenAuxiliary`, or is `NSScreenSaverWindowLevel` needed?** Level vs. Space
  behavior interaction over full-screen is best settled by a `/prototype` on a real
  machine across macOS versions. `[uncertain]`
- **[uncertain] Exact PyObjC `NSTimer` block-selector spelling and `NSColor`
  convenience-selector names.** The snippet uses
  `scheduledTimerWithTimeInterval_repeats_block_` and
  `colorWithCalibratedWhite_alpha_`; verify the precise bridged names against the
  installed PyObjC (or use the target/selector `NSTimer` variant). `[uncertain]`
- **[uncertain] Whether `Accessory` policy is compatible with the app being launched as
  a plain CLI (`python -m local_flow`) with no bundle/Info.plist**, and whether
  `setActivationPolicy_` alone suffices without `LSUIElement` in a plist. Docs say the
  policy is settable at runtime in 10.9+, but a prototype should confirm no Dock icon
  flashes. `[uncertain]`
- **[inferred, needs #16 first] Ownership of `NSApp.run()` vs. shutdown signalling** —
  resolved as part of issue #16, not #17.

---

## Sources

Apple AppKit / Foundation / QuartzCore developer documentation (primary):
- Style mask — nonactivatingPanel: <https://developer.apple.com/documentation/appkit/nswindow/stylemask-swift.struct/nonactivatingpanel>
- Style mask — borderless: <https://developer.apple.com/documentation/appkit/nswindow/stylemask-swift.struct/borderless>
- NSPanel: <https://developer.apple.com/documentation/appkit/nspanel>
- NSPanel.becomesKeyOnlyIfNeeded: <https://developer.apple.com/documentation/appkit/nspanel/becomeskeyonlyifneeded>
- NSPanel.isFloatingPanel: <https://developer.apple.com/documentation/appkit/nspanel/isfloatingpanel>
- Window levels (struct): <https://developer.apple.com/documentation/appkit/nswindow/level-swift.struct>
- Window level (property): <https://developer.apple.com/documentation/appkit/nswindow/level-swift.property>
- Levels: floating / statusBar / screenSaver / modalPanel / normal (children of the struct URL above)
- collectionBehavior (property): <https://developer.apple.com/documentation/appkit/nswindow/collectionbehavior-swift.property>
- collectionBehavior options: canJoinAllSpaces / stationary / fullScreenAuxiliary / ignoresCycle / transient (children of the struct)
- canBecomeKey: <https://developer.apple.com/documentation/appkit/nswindow/canbecomekey>
- canBecomeMain: <https://developer.apple.com/documentation/appkit/nswindow/canbecomemain>
- orderFrontRegardless(): <https://developer.apple.com/documentation/appkit/nswindow/orderfrontregardless()>
- makeKeyAndOrderFront(_:): <https://developer.apple.com/documentation/appkit/nswindow/makekeyandorderfront(_:)>
- isOpaque: <https://developer.apple.com/documentation/appkit/nswindow/isopaque>
- backgroundColor: <https://developer.apple.com/documentation/appkit/nswindow/backgroundcolor>
- hasShadow: <https://developer.apple.com/documentation/appkit/nswindow/hasshadow>
- ignoresMouseEvents: <https://developer.apple.com/documentation/appkit/nswindow/ignoresmouseevents>
- init(contentRect:styleMask:backing:defer:): <https://developer.apple.com/documentation/appkit/nswindow/init(contentrect:stylemask:backing:defer:)>
- NSView: <https://developer.apple.com/documentation/appkit/nsview>
- NSView.draw(_:): <https://developer.apple.com/documentation/appkit/nsview/draw(_:)>
- NSView.setNeedsDisplay(_:): <https://developer.apple.com/documentation/appkit/nsview/setneedsdisplay(_:)>
- NSView.wantsLayer: <https://developer.apple.com/documentation/appkit/nsview/wantslayer>
- NSView.needsPanelToBecomeKey: <https://developer.apple.com/documentation/appkit/nsview/needspaneltobecomekey>
- NSApplication.run(): <https://developer.apple.com/documentation/appkit/nsapplication/run()>
- NSApplication.setActivationPolicy(_:): <https://developer.apple.com/documentation/appkit/nsapplication/setactivationpolicy(_:)>
- NSApplication.activate(ignoringOtherApps:): <https://developer.apple.com/documentation/appkit/nsapplication/activate(ignoringotherapps:)>
- ActivationPolicy: accessory / prohibited / regular: <https://developer.apple.com/documentation/appkit/nsapplication/activationpolicy-swift.enum/accessory>
- Timer (Foundation): <https://developer.apple.com/documentation/foundation/timer>
- CADisplayLink (QuartzCore): <https://developer.apple.com/documentation/quartzcore/cadisplaylink>
- AppKit overview: <https://developer.apple.com/documentation/appkit>
- Threading Programming Guide — Thread Safety Summary (main thread owns NSApplication.run; NSView main-thread-only; performSelectorOnMainThread): <https://developer.apple.com/library/archive/documentation/Cocoa/Conceptual/Multithreading/ThreadSafetySummary/ThreadSafetySummary.html>

PyObjC (primary):
- PyObjC core intro (selector→underscore mapping, subclassing, objc.super, NSAutoreleasePool/run loop): <https://pyobjc.readthedocs.io/en/latest/core/intro.html>

Repo files grounding the #16 and item-6 findings:
- `/Users/I748258/Projects/seda/src/local_flow/app.py` (`AppController.run()` blocks on `threading.Event`)
- `/Users/I748258/Projects/seda/src/local_flow/input/hotkeys.py` (hotkey callbacks on a listener thread)
- `/Users/I748258/Projects/seda/src/local_flow/audio/recorder.py` (`peak_level`, `_rms` — one float per audio block)
