# Architecture

Local Flow records audio, transcribes it locally, processes the transcript deterministically, optionally runs local LLM cleanup, then pastes the result at the cursor. Nothing leaves the device unless the user explicitly opts in.

## State machine

The controller (`app.py`) moves through these states per dictation:

```
STARTING → IDLE → RECORDING → PROCESSING_AUDIO
        → TRANSCRIBING → CLEANING (optional) → PASTING → IDLE
```

Cancel and error transitions branch off at any processing stage and return to `IDLE`. All transitions are guarded by a lock so the hotkey thread, the audio-callback thread, and the worker thread operate safely.

## Data flow

```
Microphone (sounddevice) → RecordedAudio (numpy float32)
  → FasterWhisperBackend.transcribe()  → TranscriptionResult.text
  → process_transcript()               → PipelineResult (text + protected_text + token_registry)
  → OllamaCleanupProvider.clean()      → cleaned protected text   (optional, disabled by default)
  → finalize_after_cleanup()           → final text
  → TextInserter.insert()              → text at cursor (via clipboard + simulated paste)
```

## Threads and executors

- **Main thread** – blocks on a shutdown `threading.Event`; installs SIGINT/SIGTERM handlers.
- **Hotkey listener thread** – managed by `pynput`; calls `_on_press`, `_on_release`, `_on_cancel` on the controller.
- **Audio callback thread** – managed by `sounddevice`; feeds PCM chunks into an internal ring buffer.
- **Worker thread** – a single-slot `ThreadPoolExecutor`; runs `_process_audio` (transcription, pipeline, cleanup, paste). Model inference happens only here, never on the listener thread.

## Backend interfaces

Three `Protocol`-based seams make subsystems testable without hardware:

| Protocol | Production impl | Fake |
|---|---|---|
| `TranscriptionBackend` | `FasterWhisperBackend` | `FakeBackend` |
| `CleanupProvider` | `OllamaCleanupProvider` | `FakeCleanupProvider` |
| `ClipboardProvider` | `PyperclipClipboard` | `FakeClipboard` |
| `PasteBackend` | `PynputPasteBackend` | `FakePasteBackend` (in tests) |
| `HotkeyProvider` | `PynputHotkeyProvider` | `FakeHotkeyProvider` (in tests) |

Each factory (`transcription/factory.py`, `cleanup/factory.py`, `input/paste.py`) builds the production provider from config and exposes a `"fake"` / `"noop"` backend for headless or dry-run use.

## Platform abstractions

- **Clipboard** – `PyperclipClipboard` wraps `pyperclip`; non-text clipboards read as `None` so restoration never falsely claims success.
- **Paste** – `PynputPasteBackend` simulates the configured shortcut. Platform defaults and per-application overrides are resolved by `select_shortcut()` in `input/paste.py`.
- **Hotkeys** – `PynputHotkeyProvider` registers a `HotKey` listener for push-to-talk and a `GlobalHotKeys` listener for cancel.
- **Audio** – `SounddeviceRecorder` captures mono float32 PCM at the configured sample rate.
- **Model storage** – `platformdirs` resolves cache and config directories platform-appropriately; models are never stored in the project directory.

## Privacy boundaries

The following data is processed only in-process and is never written to disk unless the user explicitly enables a retention option:

| Data | Default behaviour |
|---|---|
| Audio samples | Discarded after transcription |
| Transcript text | Never logged (requires `app.log_transcripts = true`) |
| Clipboard contents | Held in memory during paste; never logged |
| Custom vocabulary | Passed to transcription hints only |
| Cleanup request/response | Never logged (only aggregate metrics) |

The only network traffic the application initiates is:

- Model downloads (`local-flow models download`, on-demand, user-initiated).
- Cleanup requests to the configured Ollama endpoint (loopback by default; requires `cleanup.enabled = true`).

## Failure recovery

Every per-dictation failure returns to `IDLE` without crashing the loop:

| Failure | Recovery |
|---|---|
| Transcription error | Log `ERROR`, notify user, return to IDLE |
| Empty audio | Notify CANCELLED, return to IDLE |
| Cancel command in transcript | Notify CANCELLED, return to IDLE |
| Cleanup timeout / transport error | Fall back to deterministic transcript; continue to paste |
| Cleanup validation failure | Fall back to deterministic transcript; continue to paste |
| Paste failure | Leave transcript on clipboard; notify user; return to IDLE |

## Extension points

- **New transcription backend** – implement `TranscriptionBackend` protocol and add a branch to `transcription/factory.py`.
- **New cleanup provider** – implement `CleanupProvider` protocol and add a branch to `cleanup/factory.py`.
- **Platform-native clipboard** – replace `PyperclipClipboard` with a provider that reads multi-format clipboard data.
- **Application-specific paste** – add entries to `[[paste.application_overrides]]` in config; override the active-app detection in `input/paste.py`.
