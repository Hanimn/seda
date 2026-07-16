# Changelog

All notable changes to Local Flow are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] — 2026-07-16

First release candidate. Implements the full MVP (Phases 0–8).

### Added

**Phase 0 — Project skeleton**
- Project configuration, logging, diagnostics, and CLI skeleton.
- `local-flow config init/validate/show-effective` commands.
- `local-flow doctor` with `--json` output.
- `local-flow version` and `local-flow doctor` commands.

**Phase 1 — File transcription**
- `local-flow transcribe FILE` for PCM WAV transcription.
- `faster-whisper` backend with auto CUDA/CPU device selection.
- `local-flow models download/list-local/recommend` commands.

**Phase 2 — Microphone recording**
- Push-to-talk microphone recording via `sounddevice`.
- Configurable VAD (energy-based), silence trimming, leading/trailing padding.
- `local-flow devices` and `local-flow test-mic` commands.
- `SounddeviceRecorder` with overflow detection.

**Phase 3 — Hotkeys and controller**
- Global push-to-talk hotkey via `pynput`.
- Cancel hotkey (Escape by default).
- `AppController` state machine: RECORDING → TRANSCRIBING → PASTING.
- Console notifications for all state transitions.

**Phase 4 — Deterministic text processing**
- Spoken command engine: longest-phrase-wins, contextual path-separator detection, `symbol` prefix for forced replacements.
- Technical-token protection with opaque `__LF_<PREFIX>_<NNNN>__` placeholders; integrity validation on restore.
- Conservative filler removal (polished mode by default).
- Beginning-of-transcript mode commands (`literal mode`, `polished mode`, `cancel dictation`).
- Full sanitization pipeline (C0/C1 control characters, null-byte rejection).

**Phase 5 — Clipboard paste (MVP complete)**
- `ClipboardProvider` protocol with `PyperclipClipboard`.
- `TextInserter` with the §16 save→write→paste→race-safe-restore sequence.
- Multiline policy: `preserve` / `flatten` / `copy_only`.
- Per-platform and per-application paste shortcuts.
- `--no-paste` CLI flag for copy-only mode.

**Phase 6 — Optional Ollama cleanup**
- Pluggable `CleanupProvider` with Ollama HTTP backend (lazy `httpx` import).
- Strict output-only system prompt; mode-aware standard/polished instructions.
- Output validation: empty, whitespace, missing/dup/reordered/extra placeholders, over-expansion, assistant prefaces, apparent answers.
- Fail-open: any error or rejected output falls back to the deterministic transcript.
- `--no-cleanup` CLI flag; loopback-only endpoint by default.

**Phase 7 — Diagnostics and hardening**
- Full `local-flow doctor` checklist: Python version, OS, config, mic enumeration, clipboard, hotkeys, transcription backend, CUDA, Ollama reachability, Wayland/X11, writable locations.
- Application-specific paste overrides (`[[paste.application_overrides]]`).
- Per-cycle DEBUG-level performance metrics (audio/transcription/pipeline/cleanup/paste timing).
- Pre-existing ANSI color bug fixed in `--help` output assertions.

**Phase 8 — Packaging and docs**
- Version bumped to `0.1.0`.
- `uv build` produces a clean wheel and sdist; IMPLEMENTATION_PLAN.md, `.claude/`, `.github/`, `uv.lock`, and `docs/agents/` excluded from the sdist.
- `docs/ARCHITECTURE.md` — state machine, data flow, thread model, backend interfaces, privacy boundaries, failure recovery.
- `docs/PRIVACY.md` — qualified privacy claims, what stays local, when network access occurs, clipboard limitations.
- `docs/TROUBLESHOOTING.md` — 14-item troubleshooting guide covering all §20 items.
- `docs/CLAUDE_CODE_USAGE.md` — prompt styles, modes, multiline policy, known limitations.
- `CHANGELOG.md` (this file).

### Known limitations

- **Wayland**: global hotkeys and simulated input do not work on Wayland compositors without additional setup. See `docs/TROUBLESHOOTING.md`.
- **macOS accessibility**: push-to-talk requires the Accessibility permission. Input Monitoring may also be required depending on the terminal.
- **Non-text clipboard**: the prior clipboard is not restored when it holds non-text content (images, files). Only text clipboard restoration is supported in this release.
- **Apple Silicon**: `faster-whisper` runs on CPU. MLX backend is deferred to a future release.
- **Windows**: CI passes on Windows but the release has not been manually verified on Windows. Consider this platform untested.
- **Terminal paste behaviour**: multiline paste behaviour varies by terminal configuration. Set `paste.multiline_policy = "flatten"` if you experience unexpected command execution.
- **Copy-only hotkey**: a dedicated second hotkey for copy-only mode is not implemented; use `--no-paste` or `paste.multiline_policy = "copy_only"` instead.

---

[0.1.0]: https://github.com/Hanimn/seda/releases/tag/v0.1.0
