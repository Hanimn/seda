"""The ``local-flow`` command-line interface (see IMPLEMENTATION_PLAN.md §20).

Phase 0 implements the non-hardware commands — ``version``, ``config``, and
``doctor`` — end to end. Commands that need audio, models, or global hotkeys
are declared so they appear in ``--help`` and the exit-code contract is
stable, but they report that they are not yet implemented rather than
pretending to work.
"""

from __future__ import annotations

import json
from enum import IntEnum
from pathlib import Path

import typer

from local_flow import __version__
from local_flow.config import (
    Config,
    ConfigError,
    default_config_path,
    load_config,
    render_toml,
)
from local_flow.diagnostics import Status, run_checks, worst_status
from local_flow.errors import LocalFlowError, ModelUnavailableError
from local_flow.logging_config import configure_logging


class ExitCode(IntEnum):
    """Process exit codes (see §20 'Exit codes')."""

    SUCCESS = 0
    RUNTIME = 1
    CONFIG = 2
    AUDIO = 3
    MODEL = 4
    TRANSCRIPTION = 5
    CLIPBOARD = 6
    PERMISSION = 7
    CLEANUP = 8


app = typer.Typer(
    name="local-flow",
    help="Local-first, system-wide voice dictation.",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="Inspect and manage configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")
models_app = typer.Typer(help="Inspect and manage transcription models.", no_args_is_help=True)
app.add_typer(models_app, name="models")


def _err(message: str) -> None:
    typer.echo(message, err=True)


def _not_implemented(command: str, code: ExitCode) -> None:
    """Report a command that is declared but not implemented in this phase."""
    _err(f"'{command}' is not implemented yet (Phase 0 provides the skeleton only).")
    raise typer.Exit(code=int(code))


def _require_faster_whisper_utils() -> object:
    """Import ``faster_whisper.utils`` or exit with a readable MODEL error."""
    try:
        from faster_whisper import utils
    except ImportError:
        _err("faster-whisper is not installed; install the 'whisper' extra")
        raise typer.Exit(code=int(ExitCode.MODEL)) from None
    return utils


# --- Top-level commands -----------------------------------------------------


@app.command()
def version() -> None:
    """Print the Local Flow version."""
    typer.echo(__version__)


@app.command()
def doctor(
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of text."
    ),
    config: Path | None = typer.Option(None, "--config", help="Path to a config file to check."),
) -> None:
    """Run environment and configuration diagnostics."""
    results = run_checks(str(config) if config else None)
    overall = worst_status(results)

    if json_output:
        payload = {
            "overall": overall.value,
            "checks": [r.to_dict() for r in results],
        }
        typer.echo(json.dumps(payload, indent=2))
    else:
        for result in results:
            typer.echo(f"[{result.status.value:>4}] {result.name}: {result.detail}")
        typer.echo(f"\nOverall: {overall.value}")

    # A failing check is the only condition that yields a non-zero exit;
    # WARN/SKIP are informational and must not break scripted callers. A
    # failing *configuration* check maps to the config exit code (2); any
    # other failing check (e.g. Python version) is a general runtime failure
    # (1) per §20.
    if overall is Status.FAIL:
        config_failed = any(r.name == "Configuration" and r.status is Status.FAIL for r in results)
        code = ExitCode.CONFIG if config_failed else ExitCode.RUNTIME
        raise typer.Exit(code=int(code))


# --- config sub-commands ----------------------------------------------------


@config_app.command("path")
def config_path() -> None:
    """Print the default configuration file path."""
    typer.echo(str(default_config_path()))


@config_app.command("init")
def config_init(
    config: Path | None = typer.Option(
        None, "--config", help="Write to this path instead of the default."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config file."),
) -> None:
    """Write a default configuration file if none exists."""
    target = config or default_config_path()
    if target.exists() and not force:
        _err(f"config file already exists at {target} (use --force to overwrite)")
        raise typer.Exit(code=int(ExitCode.CONFIG))

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_toml(Config()), encoding="utf-8")
    except OSError as exc:
        _err(f"could not write config file {target}: {exc}")
        raise typer.Exit(code=int(ExitCode.CONFIG)) from exc

    typer.echo(f"wrote default configuration to {target}")


@config_app.command("validate")
def config_validate(
    config: Path | None = typer.Option(
        None, "--config", help="Path to the config file to validate."
    ),
) -> None:
    """Validate a configuration file and report readable errors."""
    try:
        load_config(config)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(code=int(ExitCode.CONFIG)) from exc
    source = config or default_config_path()
    typer.echo(f"configuration is valid ({source})")


@config_app.command("show-effective")
def config_show_effective(
    config: Path | None = typer.Option(None, "--config", help="Path to the config file."),
) -> None:
    """Print the effective configuration (privacy-safe, JSON)."""
    try:
        loaded = load_config(config)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(code=int(ExitCode.CONFIG)) from exc
    typer.echo(json.dumps(loaded.effective(), indent=2))


# --- Commands declared but not implemented in Phase 0 -----------------------


@app.command()
def run() -> None:
    """Run the background dictation loop (not implemented in Phase 0)."""
    _not_implemented("run", ExitCode.RUNTIME)


@app.command()
def transcribe(
    file: Path = typer.Argument(..., help="Audio file (PCM WAV) to transcribe."),
    stdout: bool = typer.Option(False, "--stdout", help="Print the transcript to stdout."),
    copy: bool = typer.Option(False, "--copy", help="Copy the transcript to the clipboard."),
    mode: str | None = typer.Option(
        None, "--mode", help="Override the pipeline mode (e.g. 'literal')."
    ),
    offline: bool = typer.Option(
        False, "--offline", help="Refuse model downloads; require a local model."
    ),
    config: Path | None = typer.Option(None, "--config", help="Path to a config file."),
) -> None:
    """Transcribe an audio file with a local model and emit the transcript."""
    # Deferred imports keep `--help` and the config/doctor commands from paying
    # for numpy/backend import cost on every invocation.
    from local_flow.audio.loading import load_wav
    from local_flow.transcription.factory import create_backend

    try:
        loaded_config = load_config(config)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(code=int(ExitCode.CONFIG)) from exc

    if mode is not None:
        try:
            loaded_config = loaded_config.with_mode(mode)
        except ConfigError as exc:
            _err(str(exc))
            raise typer.Exit(code=int(ExitCode.CONFIG)) from exc

    logger = configure_logging(loaded_config)

    try:
        audio = load_wav(file)
    except LocalFlowError as exc:
        _err(str(exc))
        raise typer.Exit(code=int(ExitCode.AUDIO)) from exc

    backend = create_backend(loaded_config, offline=offline)
    try:
        backend.load()
        result = backend.transcribe(audio.samples, audio.sample_rate)
    except ModelUnavailableError as exc:
        _err(str(exc))
        raise typer.Exit(code=int(ExitCode.MODEL)) from exc
    except LocalFlowError as exc:
        _err(str(exc))
        raise typer.Exit(code=int(ExitCode.TRANSCRIPTION)) from exc
    finally:
        backend.close()

    # Timing diagnostics — metadata only, never the transcript text (§21).
    logger.info(
        "transcribed audio: duration=%.2fs processing=%.2fs chars=%d language=%s",
        result.duration_seconds,
        result.processing_seconds,
        len(result.text),
        result.language,
    )

    if copy:
        _copy_to_clipboard(result.text)
    # Default to stdout when no explicit sink is requested, so the command is
    # useful on its own.
    if stdout or not copy:
        typer.echo(result.text)


def _copy_to_clipboard(text: str) -> None:
    """Copy ``text`` to the clipboard, warning (not failing) if unavailable."""
    try:
        import pyperclip

        pyperclip.copy(text)
    except Exception as exc:  # noqa: BLE001 - clipboard is best-effort here
        _err(f"could not copy to clipboard: {exc}")


@app.command()
def devices() -> None:
    """List audio input devices (not implemented in Phase 0)."""
    _not_implemented("devices", ExitCode.AUDIO)


@app.command(name="test-mic")
def test_mic() -> None:
    """Record a short sample to test the microphone (not implemented in Phase 0)."""
    _not_implemented("test-mic", ExitCode.AUDIO)


# --- models sub-commands ----------------------------------------------------

# A small, conservative recommendation table. English-only "small.en" is a good
# latency/accuracy default for dictation; see IMPLEMENTATION_PLAN.md §5.
_RECOMMENDED_MODELS = [
    ("small.en", "English-only default: good accuracy at low latency"),
    ("base.en", "English-only, faster and lighter than small.en"),
    ("medium.en", "English-only, higher accuracy, more RAM/compute"),
    ("large-v3", "Multilingual, highest accuracy, heaviest"),
]


@models_app.command("recommend")
def models_recommend() -> None:
    """Print recommended transcription models."""
    for name, description in _RECOMMENDED_MODELS:
        typer.echo(f"{name}\t{description}")


@models_app.command("list-local")
def models_list_local(
    config: Path | None = typer.Option(None, "--config", help="Path to a config file."),
) -> None:
    """List the model identifiers the backend recognizes (no network access).

    Note: this lists the known/available model identifiers, not a scan of the
    on-disk cache. True cache enumeration is a later refinement.
    """
    utils = _require_faster_whisper_utils()
    for name in utils.available_models():  # type: ignore[attr-defined]
        typer.echo(name)


@models_app.command("download")
def models_download(
    model: str = typer.Argument(..., help="Model name to download (e.g. small.en)."),
    offline: bool = typer.Option(
        False, "--offline", help="Refuse to download (fails if not already local)."
    ),
    config: Path | None = typer.Option(None, "--config", help="Path to a config file."),
) -> None:
    """Download a transcription model to the local cache."""
    if offline:
        _err("--offline forbids downloads; a missing model cannot be fetched")
        raise typer.Exit(code=int(ExitCode.MODEL))

    utils = _require_faster_whisper_utils()

    loaded_config = _safe_load(config)
    download_root = loaded_config.transcription.download_root or None
    try:
        path = utils.download_model(  # type: ignore[attr-defined]
            model, output_dir=download_root, local_files_only=False
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a clean CLI error
        _err(f"could not download model '{model}': {exc}")
        raise typer.Exit(code=int(ExitCode.MODEL)) from exc
    typer.echo(f"model '{model}' available at {path}")


def _safe_load(config: Path | None) -> Config:
    """Load config or exit with a readable config error (exit code 2)."""
    try:
        return load_config(config)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(code=int(ExitCode.CONFIG)) from exc


if __name__ == "__main__":
    app()
