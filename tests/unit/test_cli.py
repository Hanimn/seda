"""Smoke and behavior tests for the CLI (see IMPLEMENTATION_PLAN.md §20).

Uses Typer's :class:`CliRunner`, so no real process, hardware, or network is
touched.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from local_flow import __version__
from local_flow.cli import ExitCode, app

runner = CliRunner()


def test_version_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("version", "doctor", "config", "run", "transcribe", "devices"):
        assert command in result.stdout


def test_config_path_prints_a_path() -> None:
    result = runner.invoke(app, ["config", "path"])
    assert result.exit_code == 0
    assert "config.toml" in result.stdout


def test_config_init_then_validate_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    init = runner.invoke(app, ["config", "init", "--config", str(target)])
    assert init.exit_code == 0
    assert target.exists()

    validate = runner.invoke(app, ["config", "validate", "--config", str(target)])
    assert validate.exit_code == 0
    assert "valid" in validate.stdout


def test_config_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("[app]\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "init", "--config", str(target)])
    assert result.exit_code == int(ExitCode.CONFIG)
    assert "already exists" in result.output


def test_config_init_force_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text('[app]\nmode = "literal"\n', encoding="utf-8")
    result = runner.invoke(app, ["config", "init", "--config", str(target), "--force"])
    assert result.exit_code == 0


def test_config_validate_rejects_auto_submit(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("[paste]\nauto_submit = true\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "validate", "--config", str(target)])
    assert result.exit_code == int(ExitCode.CONFIG)
    # Error goes to stderr and must be human-readable, not a traceback.
    assert "Traceback" not in result.stdout
    assert "auto_submit" in result.output


def test_config_show_effective_is_json_and_privacy_safe(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text('[text]\ncustom_vocabulary = ["my-secret-repo"]\n', encoding="utf-8")
    result = runner.invoke(app, ["config", "show-effective", "--config", str(target)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "my-secret-repo" not in json.dumps(payload)


def test_doctor_text_output_exits_zero_on_healthy_config(tmp_path: Path) -> None:
    # A nonexistent config path loads defaults, which are valid → no FAIL.
    result = runner.invoke(app, ["doctor", "--config", str(tmp_path / "none.toml")])
    assert result.exit_code == 0
    assert "Overall:" in result.stdout


def test_doctor_json_output(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--json", "--config", str(tmp_path / "none.toml")])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "overall" in payload
    assert isinstance(payload["checks"], list)
    # doctor output must not include secrets or content.
    assert "custom_vocabulary" not in result.stdout


def test_doctor_fails_on_invalid_config(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("[paste]\nauto_submit = true\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor", "--config", str(target)])
    assert result.exit_code == int(ExitCode.CONFIG)
    assert "FAIL" in result.stdout


def test_unimplemented_command_reports_and_exits_nonzero() -> None:
    result = runner.invoke(app, ["run"])
    assert result.exit_code == int(ExitCode.RUNTIME)
    assert "not implemented" in result.output
    assert "Traceback" not in result.output
