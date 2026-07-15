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


def _err(message: str) -> None:
    typer.echo(message, err=True)


def _not_implemented(command: str, code: ExitCode) -> None:
    """Report a command that is declared but not implemented in this phase."""
    _err(f"'{command}' is not implemented yet (Phase 0 provides the skeleton only).")
    raise typer.Exit(code=int(code))


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
def transcribe(file: Path = typer.Argument(..., help="Audio file to transcribe.")) -> None:
    """Transcribe an audio file (not implemented in Phase 0)."""
    _not_implemented("transcribe", ExitCode.TRANSCRIPTION)


@app.command()
def devices() -> None:
    """List audio input devices (not implemented in Phase 0)."""
    _not_implemented("devices", ExitCode.AUDIO)


@app.command(name="test-mic")
def test_mic() -> None:
    """Record a short sample to test the microphone (not implemented in Phase 0)."""
    _not_implemented("test-mic", ExitCode.AUDIO)


if __name__ == "__main__":
    app()
