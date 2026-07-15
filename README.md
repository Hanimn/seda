# Local Flow

Local-first, system-wide voice dictation — optimized for dictating prompts into
Claude Code and other terminal applications. Hold a global push-to-talk hotkey,
speak, release, and get **editable** text at your cursor.

> **Status:** early development. This repository currently contains the
> **Phase 0** skeleton — configuration, logging, diagnostics, and the CLI. No
> audio capture, transcription, or model code is implemented yet. See
> [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) for the full roadmap.

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

Later phases add the speech backend:

```bash
uv sync --extra whisper --extra dev
uv run local-flow models download small.en   # (available from Phase 1)
```

## CLI

Phase 0 implements the non-hardware commands:

```bash
local-flow version              # print the version
local-flow config path          # show the default config file path
local-flow config init          # write a default config file
local-flow config validate      # validate a config file (readable errors)
local-flow config show-effective  # print the effective, privacy-safe config
local-flow doctor               # environment + config diagnostics
local-flow doctor --json        # machine-readable diagnostics
```

Commands that need audio, models, or global hotkeys (`run`, `transcribe`,
`devices`, `test-mic`, `models`) are listed in `--help` but report that they
are not yet implemented.

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

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest -m "not integration"
```

## License

[MIT](LICENSE)
