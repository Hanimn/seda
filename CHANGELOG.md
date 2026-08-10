# Changelog

All notable changes to Seda are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

## [0.2.0] — 2026-08-10

Second release. The macOS menu-bar app and Windows HUD epics, the
advertised-config wiring map, and a hardening pass from a deep quality audit
(ReDoS, paste-key safety, WAV robustness, config validation, debug knobs).

### Changed

- **Renamed the project from "Local Flow" to Seda** (صدا — Persian for "voice").
  This is a breaking change to align the code with the repository name:
  - PyPI/project name → `seda`.
  - CLI command → `seda` (was `local-flow`).
  - Python import path → `seda` (was `local_flow`); base exception
    `LocalFlowError` → `SedaError`.
  - Config/cache directory → `<config>/seda` (was `<config>/local-flow`).
- Added a one-time startup notice: if configuration still lives under the old
  `local-flow` directory and no `seda` directory exists yet, `seda run` points
  you to it and how to move it (no automatic copy).
- Added brand assets under `assets/brand/` and a README banner + status badges.
- **Default VAD is now Silero** (`audio.vad_backend` defaults to `"silero"`,
  wired to faster-whisper's `vad_filter`); `"energy"` is a deprecated alias for
  `"none"` (#107, #113).

### Added

**macOS menu-bar app — `seda gui` (map #83, tickets #84–#90)**
- `NSStatusBar` status item with live idle/listening/busy status, and a dropdown
  with Settings, Open Logs, Doctor (in-process), and Quit (#87, #90).
- Settings window (AppKit) reading/writing config, with an editable push-to-talk
  chord: chord capture + live re-registration of the global listener without a
  restart (#88, #89, #98).
- Menu-bar phase labels, chord header, settings model popup, and an active-mode
  indicator with a transient flash on mode switch (#98, #115).
- Durable rotating file logging (1 MiB × 5) created owner-only (0600) from birth,
  alongside the existing console handler (#85).

**macOS floating waveform overlay (HUD) (epic #15, ADR-0001…0007)**
- Non-activating floating `NSPanel` driven by the notifier fan-out
  (ADR-0001/0003), with the GUI host owning the main thread and full fail-open
  gating (`--no-overlay`, `overlay.enabled`, platform default) (ADR-0004).
- Symmetric mirror-bar waveform fed by the recorder's latest-level RMS hand-off
  (ADR-0002), responsive wave + post-release busy visual (#44).
- Persistent-companion lifecycle: shown once on READY, never hidden by events
  (ADR-0007).

**Windows HUD overlay (epic #74, ADR-0008…0010)**
- Windows overlay host with its own threading/event-loop model, platform-keyed
  host selection, and the persistent-companion contract (#71–#73, #78).
- `HudMode.IDLE` with a true 48×24 idle panel-shrink (Option-B sub-rect blit)
  (#75–#82).

**Config knobs that now actually take effect (map #100)**
- `text.custom_vocabulary` biases Whisper decoding via the initial prompt (#101).
- `audio.trim_silence` and `audio.channels` are honored by the recorder (#102).
- `paste.method = "type"` types the transcript as keystrokes (never presses
  Enter) for apps that block synthetic paste (#103).
- `audio.maximum_duration_seconds` auto-stops a recording at the cap and
  transcribes it via the normal path (#105, #108).
- `hotkeys.toggle_mode` cycles the session dictation mode
  (literal → standard → polished) with a menu-bar indicator (#106, #109, #115).
- `audio.vad_backend` selects transcription-side VAD: `"silero"` enables
  faster-whisper's `vad_filter`; `"none"` disables it (#107, #113).

**Other**
- Clear startup warning when the macOS Accessibility permission is missing (#33).
- ADRs 0001–0011 and the supporting research notes and implementation specs
  (overlay, HUD lifecycle, Windows host, menu-bar toolkit).
- `app.log_transcripts` and `app.retain_debug_audio` now take effect (#126):
  transcript logging gates DEBUG records; debug audio writes timestamped,
  owner-only (0600) WAVs via the new `seda.audio.dump`.
- `transcription.max_audio_mb` guards `seda transcribe` input size — over-limit
  files are rejected on their stat size before being read into memory (#127).

### Fixed

- Lingering HUD overlay on shutdown and a stop hang on Ctrl-C (#37).
- Benign leaked-semaphore and `resource_tracker` warnings at shutdown and across
  `run`/`transcribe` (#29, #81).
- macOS listener-thread crashes: pre-warm `AXIsProcessTrusted` (#31) and the
  Carbon Text-Input-Source init (#44) on the main thread.
- Overlay fail-open boundary and startup/shutdown race (#47).
- ObjC class-name collision in the status-item runner (#90).
- **Quadratic ReDoS in the email token-protection pattern** — bounded per
  RFC 5321; 230× faster on adversarial dotted input (#120).
- Paste-shortcut failure could leave modifier keys held system-wide; every
  pressed key is now released in a `finally` unwind (#121).
- Mid-frame-truncated WAVs crashed `seda transcribe` with a raw traceback;
  they now raise a clean `AudioError` with exit code 3 (#122).
- Paste shortcuts are validated at config load; Enter/Return as the main key is
  rejected up front, naming the never-submit guarantee (#123).
- `FanOutNotifier.notify` iterates a snapshot, so a mid-flight `add()` can no
  longer inject a notifier into the in-flight event (#124).

---

## [0.1.0] — 2026-07-17

First release. Implements the full MVP (Phases 0–8), verified end-to-end on
macOS via the §39 procedure.

### Added

**Phase 0 — Project skeleton**
- Project configuration, logging, diagnostics, and CLI skeleton.
- `seda config init/validate/show-effective` commands.
- `seda doctor` with `--json` output.
- `seda version` and `seda doctor` commands.

**Phase 1 — File transcription**
- `seda transcribe FILE` for PCM WAV transcription.
- `faster-whisper` backend with auto CUDA/CPU device selection.
- `seda models download/list-local/recommend` commands.

**Phase 2 — Microphone recording**
- Push-to-talk microphone recording via `sounddevice`.
- Configurable VAD (energy-based), silence trimming, leading/trailing padding.
- `seda devices` and `seda test-mic` commands.
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
- Full `seda doctor` checklist: Python version, OS, config, mic enumeration, clipboard, hotkeys, transcription backend, CUDA, Ollama reachability, Wayland/X11, writable locations.
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

### Fixed

Found during on-device (§39) verification on macOS:
- **Platform-aware push-to-talk default** (#9): the default hotkey is now
  `<ctrl>+<shift>+space` on macOS (avoids the input-source switcher and
  Spotlight), with per-platform `push_to_talk_*` config fields and a runtime
  selector. Windows/Linux keep `<ctrl>+<alt>+space`.
- **Recording no longer stops on any key release** (#10): releasing a modifier
  of a multi-key chord previously ended the hold immediately, producing empty
  recordings; release is now tied to the trigger key.
- **Push-to-talk keys no longer leak to the focused app** (#11, #12): on macOS
  the chord keys are suppressed via `darwin_intercept`, but only while the
  chord is engaged — so ordinary typing (including the space bar) keeps working
  system-wide. This closes an Enter/newline-injection safety issue.
- **Cancel (Esc) key no longer leaks** during a hold (#13), while a bare Esc
  still reaches the focused app when idle.

### Known limitations

- **Wayland**: global hotkeys and simulated input do not work on Wayland compositors without additional setup. See `docs/TROUBLESHOOTING.md`.
- **macOS accessibility**: push-to-talk requires the Accessibility permission. Input Monitoring may also be required depending on the terminal.
- **Non-text clipboard**: the prior clipboard is not restored when it holds non-text content (images, files). Only text clipboard restoration is supported in this release.
- **Apple Silicon**: `faster-whisper` runs on CPU. MLX backend is deferred to a future release.
- **Windows**: CI passes on Windows but the release has not been manually verified on Windows. Consider this platform untested.
- **Terminal paste behaviour**: multiline paste behaviour varies by terminal configuration. Set `paste.multiline_policy = "flatten"` if you experience unexpected command execution.
- **Copy-only hotkey**: a dedicated second hotkey for copy-only mode is not implemented; use `--no-paste` or `paste.multiline_policy = "copy_only"` instead.
- **Key suppression is macOS-only**: the focused-app key-suppression fixes (#11–#13) use the macOS `darwin_intercept` path. Windows (`win32_event_filter`) and Linux suppression are follow-ups; on those platforms the push-to-talk chord may still pass through to the focused application.

---

[0.2.0]: https://github.com/Hanimn/seda/releases/tag/v0.2.0
[0.1.0]: https://github.com/Hanimn/seda/releases/tag/v0.1.0
