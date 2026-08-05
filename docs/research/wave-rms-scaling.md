# Research: speech RMS range & perceptual scaling for the wave

**Issue:** [#45](https://github.com/Hanimn/seda/issues/45) — Research: speech RMS range & perceptual scaling for the wave
**Goal:** Surface concrete facts a design decision can rest on for making the HUD wave *twitchy & sensitive* to voice.
**Scope:** Research only. No source under `src/` was modified.

## TL;DR

- The current mapping `level = clamp(rms * 4.0, 0, 1)` under-drives quiet speech badly: at the repo's own speech
  threshold `_DEFAULT_VAD_THRESHOLD = 0.015`, the bar is only **6% tall**. Normal conversational speech (~0.05 RMS)
  reaches only **20%**, and the bar **pins to 100% at RMS ≥ 0.25** — so most of the visible dynamic range is spent
  on loud speech that rarely happens.
- A **gated-sqrt** curve fixes both ends: gate out the noise floor, then `sqrt`-expand the low end.
  Recommended default: `level = clamp(sqrt(max(0, rms - 0.006)) * 2.6, 0, 1)`.
  This lifts VAD-threshold speech (0.015) to **~25%**, normal speech (0.05) to **~55%**, and reaches full height
  around RMS 0.15 — where loud speech actually lives.
- Responsiveness is **not** the problem. The recorder emits a fresh RMS every **64 ms** (~15.6 blocks/s) and the
  overlay redraws at **60 Hz**, so the redraw is ~4x faster than new data arrives. The wave already updates fast
  enough to feel twitchy; the felt sluggishness is entirely the *scaling curve*, not the polling rate.

---

## 1. Typical per-block RMS values (float32 [0,1])

### Repo configuration (anchors)

From `src/seda/audio/recorder.py`:

| Constant | Value | Meaning |
|---|---|---|
| `TARGET_SAMPLE_RATE` | `16_000` | mono capture rate |
| `RecorderConfig.blocksize` | `1024` | frames per callback block |
| `_DEFAULT_VAD_THRESHOLD` | `0.015` | RMS silence/speech boundary, already tuned in this repo |

`_rms()` (line 242) is the standard population RMS over a float32 block:
`float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))`. Values live in `[0, 1]` because float32 PCM is
normalized to `[-1, 1]`.

### The VAD threshold is the strongest in-repo signal

`_DEFAULT_VAD_THRESHOLD = 0.015` is used two ways that both make it authoritative for the real noise-floor/speech
boundary on this project's hardware:

- `RecordedAudio.speech_detected` returns `True` when whole-clip RMS ≥ 0.015.
- `_trim_silence()` / `_frame_energy()` treat 20 ms frames below 0.015 as silence to be trimmed.

So on the machines this was tuned for, **anything below ~0.015 RMS is "silence / room tone"** and speech starts at
or just above it. That is the number the wave curve must respect: don't animate below it (idle jitter), and expand
aggressively just above it (quiet speech should be visible).

### dBFS reference frame

The standard conversion (Wikipedia *dBFS*; AES17-1998 / IEC 61606) is:

```
dBFS = 20 * log10(rms)      (rms in [0,1], full-scale sine = 0 dBFS)
```

Anchoring the repo numbers and general speech references to that scale:

| RMS (float32) | dBFS | Interpretation |
|---|---|---|
| 0.002 | −54 | quiet room tone / mic self-noise (below VAD floor) |
| 0.005–0.008 | −46 to −42 | ambient noise floor, breathing |
| **0.015** | **−36.5** | **`_DEFAULT_VAD_THRESHOLD` — quiet speech / speech onset** |
| 0.02–0.03 | −34 to −30 | soft-to-normal speech |
| 0.05 | −26 | normal conversational speech (typical average) |
| 0.1 | −20 | animated / projected speech |
| 0.2–0.3 | −14 to −10 | loud speech, close mic |
| 0.5 | −6 | shouting / clipping-adjacent peaks |
| 1.0 | 0 | full scale (clip); `RecordedAudio.clipping` fires at peak ≥ 0.99 |

These align with the common practice of tracking dictation/conversational speech around −30 to −18 dBFS average,
with a noise floor below roughly −40 dBFS. The repo's 0.015 (−36.5 dBFS) threshold sits sensibly between the two.

**Working ranges to design against:**

- **Silence / noise floor:** RMS ≲ 0.008 (< −42 dBFS) — must render as *no* wave.
- **Quiet speech:** RMS ~0.015–0.03 (−36 to −30 dBFS) — the range that currently barely moves and matters most.
- **Normal speech:** RMS ~0.03–0.1 (−30 to −20 dBFS) — the bulk of dictation.
- **Loud speech:** RMS ~0.1–0.3 (−20 to −10 dBFS) — where the wave should approach full height.

---

## 2. Why `level * 4.0` linear + clamp under-moves quiet speech

Current code (`src/seda/gui/host.py`, `WaveformView.drawRect_`, line 118):

```python
level = max(0.0, min(1.0, float(self._level) * 4.0))
# ... half = (bounds.size.height * 0.42) * level * weight * jitter
```

Walk the arithmetic (`bar%` = `level * 100`, i.e. fraction of the max half-height a center bar can use):

| RMS | `rms * 4.0` | clamped level | bar height |
|---|---|---|---|
| 0.005 (noise) | 0.020 | 0.020 | **2%** |
| **0.015 (VAD speech onset)** | 0.060 | 0.060 | **6%** |
| 0.02 (quiet speech) | 0.080 | 0.080 | **8%** |
| 0.05 (normal speech) | 0.200 | 0.200 | **20%** |
| 0.1 (animated) | 0.400 | 0.400 | 40% |
| 0.2 (loud) | 0.800 | 0.800 | 80% |
| **0.25+ (loud)** | **1.000** | **1.000** | **100% (pinned)** |

Two structural problems:

1. **Quiet & normal speech are squashed into the bottom fifth of the bar.** RMS 0.02 → 8% and even normal 0.05
   speech → 20%. A linear map has *constant* slope, but perceived loudness is roughly logarithmic — so the
   perceptually large jump from noise floor to quiet speech produces a tiny visual jump. The bars look nearly dead
   during ordinary talking.
2. **The top 75% of the RMS scale is wasted.** The bar saturates at RMS 0.25, but real speech RMS rarely exceeds
   0.1–0.2. So the visible dynamic range (8%→40%) covers only the RMS 0.02–0.1 band with a shallow slope, while the
   entire 0.25–1.0 band collapses onto a single pinned state that almost never appears. The gain constant `4.0` is
   simultaneously *too weak* for quiet speech and *too generous* at the ceiling.

---

## 3. Candidate perceptual mappings

All operate on `rms = self._level` and must output a clamped `[0,1]` `level`. Numbers below are `bar%` (= level·100).

### 3a. Square-root (`clamp(sqrt(rms) * k, 0, 1)`), k ≈ 2.5

| RMS | 0.005 | 0.015 | 0.02 | 0.05 | 0.1 | 0.2 |
|---|---|---|---|---|---|---|
| bar% | 17.7 | 30.6 | 35.4 | 55.9 | 79.1 | 100 |

- **Effect on quiet speech:** big lift — 0.015 goes from 6% → 31%, 0.05 from 20% → 56%. Compresses the top so loud
  speech (0.2) reaches full height instead of at 0.25.
- **Trade-off:** `sqrt` *also* amplifies the noise floor — 0.005 room tone shows a visible 18% wave. Without a gate
  this produces **constant idle jitter**. Needs a gate (see 3d).

### 3b. Log / dB with noise-floor subtraction and rescale

Convert to dBFS, subtract a floor, rescale a chosen dB window to `[0,1]`:

```
level = clamp((20*log10(rms) - FLOOR) / (CEIL - FLOOR), 0, 1)   # rms>0 else 0
```

With `FLOOR = -45 dBFS` (≈ RMS 0.0056, just below VAD) and `CEIL = -6 dBFS` (≈ RMS 0.5):

| RMS | 0.005 | 0.008 | 0.015 | 0.02 | 0.05 | 0.1 | 0.2 | 0.5 |
|---|---|---|---|---|---|---|---|---|
| dBFS | −46 | −42 | −36.5 | −34 | −26 | −20 | −14 | −6 |
| bar% | 0 | 7.9 | 21.9 | 28.3 | 48.7 | 64.1 | 79.5 | 99.9 |

- **Effect on quiet speech:** perceptually the most "correct" — equal dB steps give equal bar steps, so the wave
  tracks loudness the way an ear does. 0.015 → 22%, normal 0.05 → 49%.
- **Trade-off:** most computation (a `log10` per redraw — trivial at 60 Hz), and it needs a `rms > 0` guard
  (`log10(0) = -inf`). Choosing `FLOOR`/`CEIL` is a two-knob tuning problem. The curve is fairly *flat at the top*
  (0.2→0.5 only moves 79%→100%), so it feels less "punchy" for loud emphasis than sqrt. Good for a VU-meter feel,
  slightly less for a "twitchy" toy.

### 3c. Gated linear (`0 below GATE, linear expand above`)

```
level = clamp((rms - GATE) / (SPAN - GATE), 0, 1)
```

With `GATE = 0.008`, `SPAN = 0.15`:

| RMS | 0.005 | 0.008 | 0.015 | 0.02 | 0.05 | 0.1 | 0.15 |
|---|---|---|---|---|---|---|---|
| bar% | 0 | 0 | 4.9 | 8.5 | 29.6 | 64.8 | 100 |

- **Effect on quiet speech:** the gate kills idle jitter cleanly (0.008 → 0%), and mapping the ceiling to 0.15
  instead of 0.25 uses the range better. But it's still *linear* above the gate, so quiet speech just above the gate
  (0.015 → 5%) is barely better than today.
- **Trade-off:** cheapest and jitter-free, but keeps the core perceptual flaw — quiet speech stays squashed.

### 3d. RECOMMENDED — Gated square-root

Combine the gate (kills noise-floor jitter) with sqrt expansion (lifts quiet speech):

```python
GATE, GAIN = 0.006, 2.6
level = max(0.0, min(1.0, math.sqrt(max(0.0, self._level - GATE)) * GAIN))
```

| RMS | 0.004 | 0.006 | 0.008 | 0.01 | **0.015** | 0.02 | 0.03 | 0.05 | 0.1 | 0.15 | 0.2 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| bar% | 0 | 0 | 11.6 | 16.4 | **24.7** | 30.8 | 40.3 | 54.5 | 79.7 | 98.7 | 100 |

- **Silence (RMS ≤ 0.006, ≈ −44 dBFS):** exactly 0% — no idle jitter. The gate sits just below the 0.008 ambient
  floor and well below the 0.015 VAD threshold, so room tone never animates but real speech onset does.
- **Quiet speech (0.015):** 6% → **25%** — a ~4x lift, now clearly visible.
- **Normal speech (0.05):** 20% → **55%** — lands mid-bar, lots of headroom to react.
- **Loud speech (0.15–0.2):** reaches full height where speech actually peaks, not at an unreachable 0.25.
- **Trade-offs:** the gate is a hard knee, so a sound hovering right at 0.006 can flicker on/off at the very bottom;
  keeping the gate below the ambient floor (0.006 < 0.008) minimizes this. `sqrt` still compresses the very top, but
  that's desirable here — emphasis peaks slam to full and hold. One `sqrt` per redraw is negligible.

**Recommended default curve:**

```python
# In WaveformView.drawRect_, replacing `level = max(0.0, min(1.0, float(self._level) * 4.0))`
import math
GATE, GAIN = 0.006, 2.6   # GATE just below the ambient floor; GAIN sets full-height RMS (~0.15)
raw = float(getattr(self, "_level", 0.0))
level = max(0.0, min(1.0, math.sqrt(max(0.0, raw - GATE)) * GAIN))
```

Tuning knobs: raise `GATE` toward 0.008–0.010 if the room is noisy and the bottom flickers; lower `GAIN` toward
2.2 if loud speech pins too early. If a more VU-meter/less-punchy feel is wanted later, swap in 3b (dB-domain).

---

## 4. Redraw / responsiveness — is it fast enough for "twitchy"?

### The two clocks

- **Data rate (recorder):** one RMS per audio block. `blocksize = 1024` at `16_000` Hz →
  `1024 / 16000 = 0.064 s` = **64 ms per block ≈ 15.6 new RMS values per second.** `_callback` writes
  `self._latest_level` every block (`recorder.py` line 218–224).
- **Redraw rate (overlay):** `_tick` runs on an `NSTimer` at `1.0 / 60.0` = **60 Hz** (`host.py` line 189–198),
  reading `level_source()` (→ `controller.latest_level`) and calling `setNeedsDisplay_(True)`.

### Verdict: responsiveness is fine; the curve is the bottleneck

The redraw (60 Hz, ~16.7 ms) is **~4x faster than new data arrives** (15.6 Hz, 64 ms). Every fresh RMS is picked up
within one block interval, and the same value is simply re-drawn ~4 times until the next block. So the wave reacts to
speech within ~64 ms — comfortably "twitchy." **The felt sluggishness today is the `*4.0` scaling curve flattening
the motion, not the polling cadence.** Fixing §3 fixes the feel without touching timing.

Note there is **no smoothing/decay** in the current code — `_tick` reads the raw latest level each frame and
`drawRect_` maps it directly. This matches the "twitchy & sensitive, NOT smoothed" destination, so **keep it**: no
EMA/attack-release should be added. Two caveats to flag:

- **Idle jitter:** because there's no smoothing, any curve that maps the noise floor above 0 will visibly shimmer at
  rest. The **gate in the recommended curve is what suppresses this** (RMS ≤ 0.006 → 0). This is the correct place to
  solve idle jitter — a gate, not smoothing.
- **Blockiness / staircase:** since one RMS covers 64 ms but is drawn ~4 times, the bars step rather than glide.
  For a deliberately twitchy EQ toy this reads as "responsive," not "janky," and the existing per-bar `jitter`
  animation (`0.7 + 0.3*sin(...)`, `host.py` line 133) already adds sub-block liveliness on top. If it ever looks too
  steppy, the minimal fix is a *tiny* rise-only smoothing (e.g. lerp toward the new value at ~0.5/frame) — but that
  trades away twitchiness and should not be the default given the stated goal.

---

## Sources

- In-repo: `src/seda/audio/recorder.py` (`_rms`, `_DEFAULT_VAD_THRESHOLD`, `RecorderConfig`, `_callback`),
  `src/seda/gui/host.py` (`WaveformView.drawRect_`, `_tick`).
- dBFS formula and full-scale convention: Wikipedia *dBFS* (AES17-1998, IEC 61606) — `dBFS = 20·log10(rms)`.
- Curve outputs computed with numpy/`math` (float64 RMS matches `_rms`'s `astype(np.float64)`); tables above are
  exact evaluations of each formula.
