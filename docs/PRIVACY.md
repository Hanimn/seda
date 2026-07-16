# Privacy

Local Flow is designed to keep dictated content on your machine. This document describes what the application does with your data, what can leave your device, and what the defaults are.

## What stays local

Under the default configuration, the following data never leaves your device:

- **Audio recordings** – captured in memory, transcribed, and discarded. Not written to disk unless `app.retain_debug_audio = true` (off by default).
- **Transcript text** – processed in memory, passed to the clipboard, and discarded. Not written to logs unless `app.log_transcripts = true` (off by default).
- **Clipboard contents** – the prior clipboard is held in memory during paste and restored immediately. Never logged.
- **Custom vocabulary** – used only as transcription hints sent to the local model process.

## When network access can occur

Local Flow initiates network connections in these cases only:

1. **Model downloads** – when you run `local-flow models download <model>`. This is an explicit, user-initiated command and downloads from Hugging Face. It can be prevented with `--offline`.
2. **Cleanup requests** – when `cleanup.enabled = true`, transcripts are sent to the configured Ollama endpoint for prose cleanup. The endpoint defaults to `http://127.0.0.1:11434` (loopback only). A non-loopback endpoint requires `cleanup.allow_remote_endpoint = true`.

No other network connections are made. There is no telemetry, no update check, and no analytics.

## Model download behaviour

- Models are downloaded on demand when `local-flow models download` is run, or on first use when `--offline` is not set and the model is absent.
- Models are stored in the platform user-cache directory (`~/Library/Caches/local-flow` on macOS, `~/.cache/local-flow` on Linux).
- Pass `--offline` to `local-flow run` or `local-flow transcribe` to refuse downloads and require a locally cached model.

## Cleanup endpoint behaviour

- Cleanup is **disabled by default** (`cleanup.enabled = false`).
- When enabled, the transcript (with technical tokens replaced by opaque placeholders) is sent to the Ollama endpoint.
- Only the placeholder-protected text is sent; original technical tokens are restored locally after cleanup.
- The endpoint is loopback-only by default. Non-loopback endpoints require explicit opt-in and log a one-time privacy warning.
- Request and response bodies are **never logged**. Only aggregate metrics (character counts, edit ratio, validation result) are recorded.

> **Qualified claim**: Local Flow does not send data to external services under the default configuration. However, if you configure a non-loopback cleanup endpoint or download models from Hugging Face, data does leave your device for those specific operations. The defaults prevent this.

## Logging defaults

| Item | Default | Override |
|---|---|---|
| Transcript text | Not logged | `app.log_transcripts = true` |
| Audio samples | Not retained | `app.retain_debug_audio = true` |
| Clipboard contents | Never logged | — |
| Cleanup request/response | Never logged | — |
| Aggregate metrics (counts, durations) | Logged at INFO | `app.log_level = "DEBUG"` for more detail |

## Audio retention defaults

Audio is not retained after transcription. When `app.retain_debug_audio = true` and `app.debug_audio_directory` is set, WAV files are written there. This is intended for debugging transcription quality only; disable it when not actively debugging.

## Repository vocabulary behaviour

Custom vocabulary (`text.custom_vocabulary`) is used to build an initial prompt hint for the transcription model. It is passed to the local model process only — it is not sent to any network service.

## Clipboard handling limitations

The current release uses `pyperclip`, which provides text-only clipboard access. Non-text clipboard contents (images, files, rich text) are read as empty text. This means:

- If your prior clipboard held a non-text item, it cannot be restored after paste.
- The application detects this and skips restoration (it does not claim to have restored a non-text clipboard).
- A native, multi-format clipboard provider is deferred to a future release.
