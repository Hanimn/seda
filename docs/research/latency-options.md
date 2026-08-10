# Latency options for post-release transcription

Research date: 2026-08-10 (ticket #146). Question: how do we cut the
release → text-at-cursor latency, which today is bounded by faster-whisper on
CPU? Sources are cited inline; benchmark numbers are third-party and marked
as such.

## TL;DR / recommendation

1. **Adopt an MLX backend on Apple Silicon** (new `seda[mlx]` extra, behind the
   existing `TranscriptionBackend` protocol): measured **~5–7× faster** than
   faster-whisper CPU for the same model class. This is the only change that
   moves the needle by multiples rather than percents, and it lands exactly
   where the daily-driver author works (macOS).
2. **Cheap immediate lever (no code)**: for latency-sensitive use,
   `tiny.en`/`base.en` with `compute_type = "int8"` already reaches ~0.05 RTF
   on CPU — a 10 s utterance transcribes in ~0.5 s. Document it as the
   "fast profile"; accuracy loss is partially offset by our
   `custom_vocabulary` + VAD (`vad_filter`) pipeline.
3. **Reject streaming / partial transcription.** The polished commercial
   reference (Wispr Flow) deliberately does **batch-per-utterance** with a
   status HUD and no live text — matching our existing architecture and HUD.
   Partial-while-held would mean chunked overlapping re-decodes (Whisper is
   not incremental), high complexity and flicker risk for marginal gain.

## 1. MLX backend on Apple Silicon

**Facts.** faster-whisper runs on CPU on macOS (CTranslate2 has no Metal
backend); the v0.1.0 CHANGELOG already defers an MLX backend "to a future
release." MLX is Apple's array framework with first-class Whisper ports:

- `mlx-whisper` (mlx-examples) — full Whisper family incl. `large-v3-turbo`;
  models on HF under `mlx-community`.
- `lightning-whisper-mlx` — batched/optimized variant of the same.
- `parakeet-mlx` — NVIDIA Parakeet-TDT 0.6B (not Whisper): English-only,
  state-of-the-art WER for its size, even faster than Whisper ports.

**Measured** (third-party benchmark, MacBook Pro M4 24 GB, large-class model,
same utterance; [mac-whisper-speedtest](https://github.com/anvanvan/mac-whisper-speedtest)):

| implementation | model | time |
|---|---|---|
| parakeet-mlx | parakeet-tdt-0.6b-v2 | 0.50 s |
| **mlx-whisper** | large-v3-turbo | **1.02 s** |
| whisper.cpp (CoreML) | large-v3-turbo-q5_0 | 1.23 s |
| lightning-whisper-mlx | large | 1.82 s |
| **faster-whisper (CPU int8)** | large-v3-turbo | **6.96 s** |

→ MLX ≈ **6.8× faster** than our current backend on the same model class.
Even accounting for benchmark-vs-production spread, this is a multiples-level
win. At `small.en`-class sizes the gap persists directionally (CPU int8 tiny/
base reach ~15–20× real-time; MLX small-class models exceed that by several
times — [promptquorum 2026](https://www.promptquorum.com/power-local-llm/local-whisper-stt-comparison-2026),
[voicci Apple Silicon benchmarks](https://www.voicci.com/blog/apple-silicon-whisper-performance.html)).

**Dependency cost.** `mlx-whisper` pulls `mlx`, `numba`, `numpy`, `scipy` —
all pure wheels on macOS/arm64, no native build. macOS-only extra keeps other
platforms untouched: `seda[mlx]`, backend selected by
`transcription.backend = "mlx"` (name TBD at decision time), with the
`TranscriptionBackend` protocol unchanged (`load`/`transcribe`/`close`).
Model identifiers differ (`mlx-community/whisper-*` on HF) — the backend maps
the configured Whisper size to its MLX port, so config keeps saying
`small.en`.

**Risk notes.** MLX is Apple-Silicon-only (Intel Macs fall back to
faster-whisper). `parakeet-mlx` is the speed champion but is a different
model family (English-only, no multilingual) — worth a prototype, not the
default. The init-prompt / vocabulary-biasing path (#101) must be re-checked
against the MLX decode API (it accepts `initial_prompt`; verify at
implementation).

## 2. Faster CPU paths (no new dependency)

- `compute_type = "int8"` is already our CPU default via `auto` — nothing to
  change there.
- Dropping the model size is the only real CPU lever: tiny.en/base.en int8
  reach **~0.05–0.07 RTF** on a modern CPU ([promptquorum](https://www.promptquorum.com/power-local-llm/local-whisper-stt-comparison-2026),
  [HF benchmark](https://huggingface.co/ogulcanakca/faster-whisper-small-tr)),
  i.e. a 10 s dictation returns in ~0.5–0.7 s — already fine for PTT bursts.
- The accuracy gap vs small.en is real for messy audio, but dictation into a
  terminal is close-mic, scripted speech; `custom_vocabulary` (#101) and
  `vad_filter` (#113) claw back much of it.
- Action: document a "fast profile" (model = `base.en`, int8) in
  CLAUDE_CODE_USAGE.md rather than changing the default.

## 3. Streaming / partial transcription — rejected

**What the best-in-class product does.** Our Wispr Flow research note
(`docs/research/wispr-flow-streaming-dictation.md`, 2026-07-17) shows the
polished commercial reference records the whole utterance, transcribes
server-side, and **inserts once** — "words do NOT appear live/incrementally
in the target app"; the on-screen element is a status/waveform bubble, not a
text preview. That is *precisely* Seda's architecture (batch per hold +
waveform HUD + busy-on-release, #44/#78).

**Why partial-while-held is not worth it here.** Whisper-family decoding is
not incremental: partial output requires re-decoding sliding windows and
reconciling overlaps, which produces visible token churn ("to" → "two"
flicker) exactly where our users read text before submitting — the problem
Wispr dodged by never showing provisional text. The engineering cost (chunker,
overlap reconciler, new cancellation semantics mid-cycle) is a rewrite of the
`_process_audio` worker for a perceived-latency gain that the HUD's busy state
already covers emotionally.

**Alternative perceived-latency lever, if ever wanted:** begin transcription
of the *leading* audio while the key is still held (a single speculative
pre-decode of the first N seconds, finalized on release). Same batch model,
no token streaming. Out of scope now; noted for the future.

## Decision-relevant summary

| option | speedup | dep cost | risk | verdict |
|---|---|---|---|---|
| MLX backend (`seda[mlx]`) | ~5–7× on Apple Silicon | medium (pure wheels) | low (protocol seam exists) | **adopt** |
| base.en/tiny.en int8 profile | ~3× vs small.en CPU | none | accuracy trade-off | document |
| Streaming / partial decode | perceived only | very high | token flicker, rewrite | **reject** |

Follow-up: decision ticket for the MLX backend (extra name, backend key,
model mapping, initial-prompt parity) → then implementation.
