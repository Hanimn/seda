# Local Flow

Local-first, system-wide voice dictation — optimized for dictating prompts into
Claude Code and other terminal applications. Hold a global push-to-talk hotkey,
speak, release, and get **editable** text at your cursor.

> **Status:** v0.1.0 — all 8 phases complete, verified end-to-end on macOS.
> Implemented: the
> project skeleton, local `faster-whisper` transcription, microphone capture,
> global push-to-talk hotkeys, deterministic text processing (spoken commands,
> technical-token protection), clipboard paste at the cursor, optional local LLM
> cleanup (Ollama), and full diagnostics and packaging.
> See [`CHANGELOG.md`](CHANGELOG.md) for the complete history.

## Safety and privacy

Local Flow is built to be safe around terminals and coding agents:

- **It never presses Enter.** Automatic submission is not implemented; pasted
  text always stays editable before you submit it.
- **It runs locally.** No cloud transcription, no cloud cleanup, no telemetry,
  analytics, or update checks. After models are installed it works offline.
- **Conservative defaults.** LLM cleanup is disabled, transcript logging is
  disabled, and debug-audio retention is disabled out of the box.

## Requirements

- Python **3.11+**
- [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`

## Install (development)

```bash
uv sync --extra dev
uv run local-flow doctor
uv run local-flow config init
```

With `pip`:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
local-flow doctor
```

The speech backend and optional cleanup are separate extras:

```bash
uv sync --extra whisper --extra dev
uv run local-flow models download small.en
```

## CLI

Non-hardware commands:

```bash
local-flow version              # print the version
local-flow config path          # show the default config file path
local-flow config init          # write a default config file
local-flow config validate      # validate a config file (readable errors)
local-flow config show-effective  # print the effective, privacy-safe config
local-flow doctor               # environment + config diagnostics
local-flow doctor --json        # machine-readable diagnostics
```

File transcription — needs the `whisper` extra and a local model:

```bash
local-flow models recommend         # list recommended models
local-flow models download small.en # fetch a model into the local cache
local-flow models list-local        # list known model identifiers
local-flow transcribe recording.wav --stdout   # transcribe a PCM WAV file
local-flow transcribe recording.wav --copy      # copy the transcript to the clipboard
local-flow transcribe recording.wav --offline   # require a local model; never download
```

Push-to-talk dictation — hold the hotkey, speak, release, and the
transcript is pasted at your cursor:

```bash
local-flow run                  # start the push-to-talk loop
local-flow run --no-paste       # copy the transcript instead of pasting it
local-flow run --no-cleanup     # disable optional LLM cleanup for this run
local-flow devices              # list microphone input devices
local-flow test-mic             # record a short sample and report levels
```

Local Flow never presses Enter: pasted text always stays editable. If a paste
fails, the transcript is left on the clipboard and you are notified.

### Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | success |
| 1 | general runtime failure |
| 2 | invalid configuration or CLI usage |
| 3 | microphone/audio failure |
| 4 | model unavailable |
| 5 | transcription failure |
| 6 | clipboard/paste failure |
| 7 | permission failure |
| 8 | cleanup provider failure (strict mode) |

## Configuration

Configuration is TOML. See [`config.example.toml`](config.example.toml) for a
fully-commented starting point. Default location:

- macOS: `~/Library/Application Support/local-flow/config.toml`
- Linux: `~/.config/local-flow/config.toml`
- Windows: `%APPDATA%\local-flow\config.toml`

Override with `local-flow run --config /path/to/config.toml`.

## Documentation

| Document | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | State machine, data flow, thread model, backend interfaces, privacy boundaries |
| [`docs/PRIVACY.md`](docs/PRIVACY.md) | What stays local, when network access occurs, clipboard limitations |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | Microphone, model, hotkey, paste, Wayland, Ollama issues |
| [`docs/CLAUDE_CODE_USAGE.md`](docs/CLAUDE_CODE_USAGE.md) | Prompt styles, spoken commands, modes, multiline policy |
| [`CHANGELOG.md`](CHANGELOG.md) | Version history and known limitations |

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest -m "not integration"
```

## License

[MIT](LICENSE)
