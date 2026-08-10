"""Unit tests for `seda setup` — the guided first-run wizard (#150).

Interactive flows driven via CliRunner's stdin; doctor checks and the model
download are faked (no mic, no network).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from seda.cli import app

runner = CliRunner()


@pytest.fixture
def fake_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the wizard's doctor step deterministic (no real mic probing)."""
    from seda import cli as cli_module
    from seda.diagnostics import CheckResult, Status

    monkeypatch.setattr(
        cli_module,
        "run_checks",
        lambda _path=None: [
            CheckResult("Python version", Status.PASS, "Python 3.11"),
            CheckResult("Microphone", Status.SKIP, "faked in tests"),
        ],
    )


@pytest.fixture
def fake_model_utils(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    calls: dict[str, list[str]] = {"downloads": []}

    class _Utils:
        def download_model(self, model: str, **_kw: object) -> str:
            calls["downloads"].append(model)
            return f"/cache/{model}"

    monkeypatch.setattr("seda.cli._require_faster_whisper_utils", lambda: _Utils())
    return calls


def _answers(*lines: str) -> str:
    return "\n".join(lines) + "\n"


class TestSetupWizard:
    def test_fresh_setup_writes_config_with_defaults(
        self, tmp_path: Path, fake_doctor: None, fake_model_utils: dict[str, list[str]]
    ) -> None:
        target = tmp_path / "config.toml"
        # No download, keep the default hotkey.
        result = runner.invoke(app, ["setup", "--config", str(target)], input=_answers("n", ""))
        assert result.exit_code == 0, result.output
        assert target.exists()
        text = target.read_text(encoding="utf-8")
        assert "push_to_talk_macos" in text
        # The wizard surfaces the reused doctor checks and next steps.
        assert "Python version" in result.output
        assert "seda doctor" in result.output

    def test_existing_config_overwrite_declined(
        self, tmp_path: Path, fake_doctor: None, fake_model_utils: dict[str, list[str]]
    ) -> None:
        target = tmp_path / "config.toml"
        target.write_text("# my config\n", encoding="utf-8")
        result = runner.invoke(
            app, ["setup", "--config", str(target)], input=_answers("n", "", "n")
        )
        assert result.exit_code == 0, result.output
        assert target.read_text(encoding="utf-8") == "# my config\n"

    def test_custom_hotkey_is_written(
        self, tmp_path: Path, fake_doctor: None, fake_model_utils: dict[str, list[str]]
    ) -> None:
        target = tmp_path / "config.toml"
        result = runner.invoke(
            app, ["setup", "--config", str(target)], input=_answers("n", "<ctrl>+<alt>+d")
        )
        assert result.exit_code == 0, result.output
        assert 'push_to_talk = "<ctrl>+<alt>+d"' in target.read_text(encoding="utf-8")

    def test_invalid_hotkey_reprompts(
        self, tmp_path: Path, fake_doctor: None, fake_model_utils: dict[str, list[str]]
    ) -> None:
        target = tmp_path / "config.toml"
        result = runner.invoke(
            app,
            ["setup", "--config", str(target)],
            input=_answers("n", "<ctrl>+notakey", "<esc>"),
        )
        assert result.exit_code == 0, result.output
        assert 'push_to_talk = "<esc>"' in target.read_text(encoding="utf-8")

    def test_model_download_when_confirmed(
        self, tmp_path: Path, fake_doctor: None, fake_model_utils: dict[str, list[str]]
    ) -> None:
        target = tmp_path / "config.toml"
        result = runner.invoke(app, ["setup", "--config", str(target)], input=_answers("y", ""))
        assert result.exit_code == 0, result.output
        assert fake_model_utils["downloads"] == ["small.en"]
