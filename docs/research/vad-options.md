# Research: VAD options for `audio.vad_backend` (#104)

**Map:** #100 · **Ticket:** #104 · **Decision ticket (human-in-the-loop):** #107
**Status:** findings only — this note does **not** choose a resolution.

## The question

`audio.vad_backend` accepts `energy | silero | none` (`src/seda/config.py:136`) but
**nothing reads the knob today**. The recorder only does energy-based silence
*trimming* (`src/seda/audio/recorder.py:270` `_trim_silence`, gated by
`audio.trim_silence`, not by `vad_backend`), and the transcription backend calls
`WhisperModel.transcribe(...)` **without** `vad_filter`
(`src/seda/transcription/faster_whisper_backend.py:121-128`). Before deciding what
each knob value should resolve to, this note gathers the facts on the two real VAD
paths available to Seda:

- (a) faster-whisper's built-in `vad_filter` (transcription-side).
- (b) Silero VAD as a recorder-level backend (recorder-side).
- (c) how (a)/(b)/off map onto the existing `energy | silero | none` values.

Seda records **mono 16 kHz float32** (`AudioConfig.sample_rate = 16000`,
`channels = 1`), which matters for both options below.

---

## Option A — faster-whisper built-in `vad_filter` (transcription-side)

faster-whisper **bundles the Silero VAD model** and can filter non-speech audio
*before* the Whisper decode. README: *"The library integrates the Silero VAD model
to filter out parts of the audio without speech."*
[SYSTRAN/faster-whisper README](https://github.com/SYSTRAN/faster-whisper)

**API surface.** `WhisperModel.transcribe(...)` exposes:

- `vad_filter: bool` — enable the filter. *"enabled by default for batched
  transcription."*
- `vad_parameters: dict | VadOptions` — tuning; README example
  `vad_parameters=dict(min_silence_duration_ms=500)`, and *"The default behavior is
  conservative and only removes silence longer than 2 seconds."*
- `no_speech_threshold` — Whisper's own per-segment gate (probability of the
  `<|nospeech|>` token above which a segment is treated as silence; default `0.6`,
  inherited from OpenAI Whisper). This is independent of `vad_filter` and is always
  active.
  [SYSTRAN/faster-whisper README](https://github.com/SYSTRAN/faster-whisper)

**`VadOptions` fields + defaults** (from
[`faster_whisper/vad.py`](https://raw.githubusercontent.com/SYSTRAN/faster-whisper/master/faster_whisper/vad.py)):

| field | default |
|---|---|
| `threshold` | `0.5` |
| `neg_threshold` | `None` |
| `min_speech_duration_ms` | `0` |
| `max_speech_duration_s` | `inf` |
| `min_silence_duration_ms` | `2000` |
| `speech_pad_ms` | `400` |
| `min_silence_at_max_speech` | `98` |
| `use_max_poss_sil_at_max_speech` | `True` |

The VAD runs at `sampling_rate = 16000` with a `window_size_samples = 512`
frame — an exact match for Seda's 16 kHz mono capture. `collect_chunks` merges the
detected speech regions before decoding.
[`faster_whisper/vad.py`](https://raw.githubusercontent.com/SYSTRAN/faster-whisper/master/faster_whisper/vad.py)

**Hallucination-on-silence.** This is the well-known Whisper failure mode where the
decoder emits boilerplate (e.g. "Thank you for watching") over silent/non-speech
audio. `vad_filter` addresses it structurally by **not feeding silence to the
decoder at all**; `no_speech_threshold` is the complementary post-hoc gate. The
README frames VAD as filtering non-speech segments; combined with the conservative
2 s default it materially reduces (does not mathematically guarantee elimination of)
silence hallucinations.
[SYSTRAN/faster-whisper README](https://github.com/SYSTRAN/faster-whisper)

**Dependency weight: effectively zero.** The Silero model is shipped *inside*
faster-whisper (ONNX), which Seda already depends on. Enabling `vad_filter` adds
**no new package**.

**Runtime cost.** The VAD is a ~2 MB model over 512-sample windows; per the Silero
project *"One audio chunk (30+ ms) takes less than 1 ms ... on a single CPU
thread."* For push-to-talk clips (seconds, not hours) the pre-pass cost is
negligible relative to the Whisper decode.
[snakers4/silero-vad README](https://github.com/snakers4/silero-vad)

**Seda implementation effort:** one dict/kwargs change in
`faster_whisper_backend.transcribe()` (`faster_whisper_backend.py:121`) plus config
plumbing. No recorder changes.

---

## Option B — Silero VAD as a recorder-level backend (recorder-side)

Standalone [snakers4/silero-vad](https://github.com/snakers4/silero-vad):
*"enterprise-grade Voice Activity Detector"* trained on 6000+ languages, robust to
noise and quality levels.

**Sample rates.** Explicitly supports **8000 Hz and 16000 Hz** only — Seda's 16 kHz
mono is directly supported (no resampling).
[snakers4/silero-vad README](https://github.com/snakers4/silero-vad)

**Chunk size.** faster-whisper's vendored copy uses `window_size_samples = 512` at
16 kHz; Silero v5/v6 requires exactly **512 samples** per window at 16 kHz (256 at
8 kHz). A sounddevice callback recorder would need to buffer input into 512-sample
(32 ms) frames before calling the model.
([`faster_whisper/vad.py`](https://raw.githubusercontent.com/SYSTRAN/faster-whisper/master/faster_whisper/vad.py);
[snakers4/silero-vad README](https://github.com/snakers4/silero-vad))

**License.** MIT — *"zero strings attached — no telemetry, no keys, no
registration."*
[snakers4/silero-vad README](https://github.com/snakers4/silero-vad)

**Dependency weight (the catch).** The `silero-vad` PyPI package (v6.2.1, 2026-02-24)
lists `Requires: torch>=1.12.0`, `torchaudio>=0.12.0`, `onnxruntime>=1.16.1`,
`packaging`. Wheel 9.1 MB / sdist 28.9 MB, but the transitive **torch + torchaudio**
pull is hundreds of MB. Using the model via `onnxruntime` alone (the JIT/ONNX model
is ~2 MB) avoids torch, but the published package metadata still declares torch as a
required dep.
[silero-vad on PyPI](https://pypi.org/pypi/silero-vad/json) ·
[snakers4/silero-vad README](https://github.com/snakers4/silero-vad)

**Cross-platform (macOS + Windows).** Runs *"everywhere where these runtimes are
available"* (PyTorch/ONNX); onnxruntime ships wheels for macOS and Windows. Viable
on both Seda targets.
[snakers4/silero-vad README](https://github.com/snakers4/silero-vad)

**Real-time cost.** <1 ms per 30 ms+ chunk on one CPU thread — cheap enough for a
live sounddevice callback.
[snakers4/silero-vad README](https://github.com/snakers4/silero-vad)

**Streaming integration shape.** README's high-level API is offline
(`get_speech_timestamps(wav, model)`); streaming needs the stateful model over
512-sample frames (see project wiki examples). In a sounddevice callback recorder,
Silero would replace/augment the current RMS `_frame_energy` gate
(`recorder.py:258`) — a **larger change** than Option A: frame buffering, model
load/warm-up, cross-platform onnxruntime dependency, and state reset per recording.

---

## Option C — mapping onto the `energy | silero | none` knob

The three existing values map naturally onto the paths above. Restated as options
(not a decision):

- **`energy`** — today's behaviour: RMS energy trim in the recorder
  (`_trim_silence`), no model. The current default.
- **`silero`** — could mean **either** (A) turn on faster-whisper's `vad_filter`
  (transcription-side, zero new deps) **or** (B) a recorder-level Silero gate (new
  torch/onnxruntime dep). These are materially different in cost and effort; #107
  must disambiguate which "silero" means.
- **`none`** — no VAD/trim at all (raw audio to Whisper; relies only on
  `no_speech_threshold`).

Note the knob is currently orthogonal to `audio.trim_silence`, which independently
gates the energy trim — #107 should also decide how the two interact.

---

## Tradeoffs

| Dimension | `energy` (current) | `silero` via faster-whisper `vad_filter` (A) | `silero` recorder backend (B) | `none` |
|---|---|---|---|---|
| Accuracy on silence / hallucination | Trims edges only; no decoder-level gate | Strong: silence not decoded + `no_speech_threshold` | Strong at capture; still relies on decoder gate | Weakest (decoder gate only) |
| New dependency weight | None | **None** (bundled in faster-whisper) | **Heavy**: torch+torchaudio (or onnxruntime-only ~2 MB model) | None |
| License | n/a | MIT (bundled Silero) | MIT | n/a |
| macOS + Windows | Yes | Yes | Yes (onnxruntime wheels) | Yes |
| 16 kHz mono fit | Native | Native (512-sample window @16 kHz) | Native (16 kHz supported; 512-sample frames) | Native |
| Real-time cost | Trivial (RMS) | Negligible pre-pass vs decode | <1 ms/chunk, but needs frame buffering in callback | Zero |
| Impl. effort | Done | Low — kwargs in `faster_whisper_backend.transcribe()` | High — recorder frame buffering + model + deps | Trivial |
| Layer | Recorder | Transcription config | Recorder | — |

Sources for the table rows: faster-whisper README + `vad.py`; silero-vad README +
PyPI (all cited inline above).

---

## Decision framing for #107 (no choice made here)

Open questions the decision ticket needs to resolve:

1. **What does `silero` resolve to?** Transcription-side `vad_filter` (A, zero new
   deps, ~5-line change) or a recorder-side Silero gate (B, new torch/onnxruntime
   dependency, recorder rework)? These share a name but not a cost profile.
2. **Is a recorder-level model VAD even wanted**, given faster-whisper already ships
   Silero and can filter at decode time for free? Option B's main added value is
   VAD *during capture* (e.g. auto-stop, live UI), which PTT may not need.
3. **How do `vad_backend` and `trim_silence` interact?** Today they are separate;
   the resolved design should define whether `vad_backend != "energy"` disables or
   composes with the energy trim.
4. **`none` semantics** — no trim + no `vad_filter`, leaving only
   `no_speech_threshold` (0.6). Acceptable for the hallucination-on-silence case?
5. **Defaults / tuning** — if `vad_filter` is adopted, whether to expose
   `min_silence_duration_ms` / `threshold` or ship the conservative defaults.

## Sources

- faster-whisper repo & README — <https://github.com/SYSTRAN/faster-whisper>
- faster-whisper `vad.py` (VadOptions, 512-sample window, 16 kHz) —
  <https://raw.githubusercontent.com/SYSTRAN/faster-whisper/master/faster_whisper/vad.py>
- Silero VAD repo & README — <https://github.com/snakers4/silero-vad>
- silero-vad PyPI metadata (v6.2.1; torch/torchaudio/onnxruntime deps; sizes) —
  <https://pypi.org/pypi/silero-vad/json>
- Seda code: `src/seda/config.py:136`, `src/seda/audio/recorder.py:258-298`,
  `src/seda/transcription/faster_whisper_backend.py:121-128`
