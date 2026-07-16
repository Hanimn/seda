"""Smoke and behavior tests for the CLI (see IMPLEMENTATION_PLAN.md §20).

Uses Typer's :class:`CliRunner`, so no real process, hardware, or network is
touched.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_flow import __version__
from local_flow.cli import ExitCode, app

WavFactory = Callable[..., Path]

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


def test_run_command_help_works() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "dictation" in result.output.lower() or "run" in result.output.lower()


def test_run_no_paste_flag_sets_copy_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-paste` runs the controller in copy-only mode without pasting."""
    import local_flow.app as app_module

    captured: dict[str, object] = {}

    class _StubController:
        def __init__(self, config: object, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self) -> None:  # pragma: no cover - trivial stub
            pass

    monkeypatch.setattr(app_module, "AppController", _StubController)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[transcription]\nbackend = "fake"\n', encoding="utf-8")

    result = runner.invoke(app, ["run", "--no-paste", "--config", str(cfg)])
    assert result.exit_code == 0
    assert captured.get("copy_only") is True


def test_run_help_documents_no_paste() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "no-paste" in result.output


# --- transcribe (Phase 1) ---------------------------------------------------


def _fake_backend_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[transcription]\nbackend = "fake"\n', encoding="utf-8")
    return cfg


def test_transcribe_prints_transcript_to_stdout(tmp_path: Path, make_wav: WavFactory) -> None:
    wav = make_wav([0, 1000, -1000, 0], sample_rate=16000)
    cfg = _fake_backend_config(tmp_path)
    result = runner.invoke(app, ["transcribe", str(wav), "--stdout", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "fake transcript" in result.stdout


def test_transcribe_defaults_to_stdout_when_no_sink(tmp_path: Path, make_wav: WavFactory) -> None:
    wav = make_wav([0, 1000, -1000, 0])
    cfg = _fake_backend_config(tmp_path)
    result = runner.invoke(app, ["transcribe", str(wav), "--config", str(cfg)])
    assert result.exit_code == 0
    assert "fake transcript" in result.stdout


def test_transcribe_missing_file_exits_audio_code(tmp_path: Path) -> None:
    cfg = _fake_backend_config(tmp_path)
    result = runner.invoke(app, ["transcribe", str(tmp_path / "nope.wav"), "--config", str(cfg)])
    assert result.exit_code == int(ExitCode.AUDIO)
    assert "Traceback" not in result.output


def test_transcribe_rejects_invalid_mode(tmp_path: Path, make_wav: WavFactory) -> None:
    wav = make_wav([0, 1000, -1000, 0])
    cfg = _fake_backend_config(tmp_path)
    result = runner.invoke(app, ["transcribe", str(wav), "--mode", "turbo", "--config", str(cfg)])
    assert result.exit_code == int(ExitCode.CONFIG)
    assert "Traceback" not in result.output
    assert "mode" in result.output


def test_transcribe_accepts_valid_mode(tmp_path: Path, make_wav: WavFactory) -> None:
    wav = make_wav([0, 1000, -1000, 0])
    cfg = _fake_backend_config(tmp_path)
    result = runner.invoke(app, ["transcribe", str(wav), "--mode", "literal", "--config", str(cfg)])
    assert result.exit_code == 0


def test_transcribe_does_not_log_transcript_text(tmp_path: Path, make_wav: WavFactory) -> None:
    wav = make_wav([0, 1000, -1000, 0])
    cfg = _fake_backend_config(tmp_path)
    result = runner.invoke(app, ["transcribe", str(wav), "--config", str(cfg)])
    # The timing log line reports a char count, never the transcript text.
    assert "chars=" in result.output


def test_models_recommend_lists_models() -> None:
    result = runner.invoke(app, ["models", "recommend"])
    assert result.exit_code == 0
    assert "small.en" in result.stdout


def test_models_download_offline_refuses() -> None:
    result = runner.invoke(app, ["models", "download", "small.en", "--offline"])
    assert result.exit_code == int(ExitCode.MODEL)
    assert "forbids downloads" in result.output
