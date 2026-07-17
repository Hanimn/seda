# ADR-0002 — Recorder exposes a pulled `latest_level` (RMS) for the overlay

- **Status:** Accepted
- **Date:** 2026-07-17
- **Deciders:** grilling session (Hani Momeninia + agent)
- **Issue:** [#19 — Recorder-to-GUI audio level hand-off contract](https://github.com/Hanimn/seda/issues/19)
- **Epic:** [#15 — live recording waveform overlay (macOS)](https://github.com/Hanimn/seda/issues/15)
- **Depends on:** [ADR-0001](0001-gui-host-owns-main-thread.md) (#16 — GUI host owns main thread)
- **Informed by:** [#17 research](https://github.com/Hanimn/seda/issues/17) — `docs/research/nspanel-nonactivating-float-recipe.md`

## Context

The overlay (epic #15) shows a few voice-driven bars while recording. It needs a live audio
level. The producing side already exists: `SounddeviceRecorder._callback` (`recorder.py:180`)
runs on **sounddevice's realtime C thread** and appends each block to `self._blocks` under
`self._lock`. But the recorder exposes **no live level** — only the finished
`RecordedAudio` at `stop()`.

Two hard constraints frame the contract:

1. **The C audio callback must stay cheap** — no disk I/O, no inference, no expensive
   logging (already the rule; `recorder.py:189`).
2. From [#17](0001-gui-host-owns-main-thread.md) research (`[sourced]`): the **audio thread
   must never call into AppKit**, and all UI work happens on the main thread. #17's redraw
   design is **pull-based** — a main-thread `NSTimer` reads the latest level each tick and
   calls `setNeedsDisplay_`.

## Decision

Add a **pulled latest-value contract**: the recorder computes a per-block RMS on the audio
thread and stores it behind the existing lock; the GUI **pulls** it from its own main-thread
timer. Four sub-decisions, each grilled:

### 1. Shape — a pulled `latest_level` property, not a push callback

`SounddeviceRecorder` gains a thread-safe read:

```python
@property
def latest_level(self) -> float:
    """Most recent per-block RMS (0.0–~1.0). Thread-safe; 0.0 when idle."""
    with self._lock:
        return self._latest_level
```

The GUI's main-thread `NSTimer` (per #17) reads `recorder.latest_level` each tick — the
recorder never calls into the GUI, and there is **no per-block cross-thread call**.

**Rejected — `on_level: Callable[[float], None]` push callback** (the ticket's sketch). It
would fire on the realtime C thread at block rate (~16 Hz at blocksize 1024 / 16 kHz), and
because the audio thread can't touch AppKit, the GUI would *still* have to marshal every call
onto main — strictly more work than a pull. Its only advantage was letting non-GUI consumers
subscribe, but there is no such consumer (the terminal meter was explicitly deferred in #15).
A property matches #17's pull design exactly.

### 2. Metric — RMS (reuse `_rms`)

`_latest_level` is computed with the existing `_rms` helper (`recorder.py:213`), not peak.
RMS is perceptually smooth, feels like loudness, and matches the energy metric the recorder
already uses for VAD (`_DEFAULT_VAD_THRESHOLD`, `RecordedAudio.speech_detected`). A bar meter
driven by RMS settles naturally; peak (`RecordedAudio.peak_level`, `recorder.py:45`) is
spikier and better suited to a clip indicator, which the HUD does not need.

### 3. Thread-safe hand-off — compute lock-free, assign under the lock

In `_callback`, the RMS is computed **before** acquiring the lock; only the assignment
happens under it, minimizing hold time on the realtime thread:

```python
def _callback(self, indata, frames, time, status):
    if status:
        with self._lock:
            self._overflow_count += 1
    block = indata.copy()
    level = _rms(block)                 # np math, lock-free
    with self._lock:
        self._blocks.append(block)
        self._latest_level = level      # single float write under the existing lock
```

A single-float read/write is cheap; the lock keeps it coherent with the block append and
matches how every other bit of recorder state is already guarded. The GUI reads the latest
value; it never blocks the audio thread beyond the existing lock window.

### 4. Idle value — reset to `0.0` on `start()` and `cancel()`

`_latest_level` is initialized to `0.0`, **reset to `0.0` on `start()` and `cancel()`**, and
left at its last value after `stop()`. So:

- before recording / after a cancel → the GUI reads `0.0` (bars flat);
- during recording → live RMS;
- after `stop()` → the bars settle at the last level until the overlay hides (they don't snap
  to zero mid-fade). The overlay's hide lifecycle (#20) decides when to stop reading.

### Exception discipline

`_rms` on a copied float32 block cannot meaningfully raise, but to honor the "a display error
never harms recording" rule the level computation is defensive: if computing/assigning the
level ever raised, it must be swallowed inside `_callback` (the callback already swallows —
`if status:` only counts overflow, never propagates). The recording path must never fail
because of the level hand-off.

## Consequences

**Positive**

- Matches #17's pull-based main-thread redraw with no per-block cross-thread call.
- Reuses `_rms` — no new metric, consistent with existing VAD energy.
- Minimal realtime-thread cost: one lock-free RMS + one float write under the existing lock.
- Fail-open friendly: no overlay ⇒ nobody reads `latest_level`; the field is inert. The
  recorder works identically with or without a GUI reader.

**Negative / costs accepted**

- Adds one field (`_latest_level`) and one property to `SounddeviceRecorder`, and one RMS
  call per block on the audio thread (microseconds for 1024 samples, accepted).
- Pull means the meter's freshness is bounded by the GUI timer rate, not the block rate — for
  a feedback HUD at ~30–60 Hz timer vs ~16 Hz blocks, the timer is faster than the producer,
  so no visible staleness. `[inferred]`

**Follow-ups (implementation / later tickets)**

- #20 (notifier/overlay show-hide seam) decides when the GUI starts/stops reading
  `latest_level` and how the post-`stop()` settle interacts with the hide animation.
- #22 (test strategy) must cover: `latest_level` is `0.0` before `start()` and after
  `cancel()`; reflects a synthetic block's RMS during recording; and that a raising `_rms`
  (monkeypatched) never breaks `stop()`.
- `docs/ARCHITECTURE.md` §"Threads and executors" note (audio callback now also updates a
  latest-level float) — updated when the code lands, not now.

## Open questions (for implementation)

- Whether `latest_level` should be normalized/clamped to `[0, 1]` in the recorder or left raw
  for the view to scale (leaning: leave raw; the view owns visual scaling). `[uncertain]`
- Whether a light smoothing (e.g. exponential decay) belongs in the recorder or the view
  (leaning: the view — keep the recorder's value a faithful per-block RMS). `[uncertain]`
