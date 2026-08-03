"""Smoke and behavior tests for the CLI (see IMPLEMENTATION_PLAN.md §20).

Uses Typer's :class:`CliRunner`, so no real process, hardware, or network is
touched.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from seda import __version__
from seda.cli import ExitCode, app

WavFactory = Callable[..., Path]

runner = CliRunner()


def _force_macos_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``cli.run``'s platform to ``darwin`` so it dispatches to ``seda.gui.host``.

    The host is selected by ``sys.platform`` via ``_HOST_MODULES`` (ADR-0009 §3),
    so tests that monkeypatch ``seda.gui.host.run_with_overlay`` must force the
    darwin branch to be selected — otherwise they pass vacuously on a Linux/Windows
    CI runner (which would resolve to no host / ``host_win``) and never exercise the
    patched macOS host.
    """
    monkeypatch.setattr("seda.cli.sys.platform", "darwin")


@pytest.fixture(autouse=True)
def _silence_migration_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the config migration notice by default.

    ``run`` calls :func:`seda.config.migration_notice`, which reads the *real*
    user config directories — so on a machine that still has an old
    ``local-flow`` config dir, the notice would leak into unrelated CLI tests.
    Default it to silent; the dedicated migration test overrides this.
    """
    import seda.config as config_module

    monkeypatch.setattr(config_module, "migration_notice", lambda: None)


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


def test_run_no_paste_flag_sets_copy_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--no-paste` runs the controller in copy-only mode without pasting."""
    import seda.app as app_module

    captured: dict[str, object] = {}

    class _StubController:
        def __init__(self, config: object, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self) -> None:  # pragma: no cover - trivial stub
            pass

    monkeypatch.setattr(app_module, "AppController", _StubController)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[transcription]\nbackend = "fake"\n', encoding="utf-8")

    result = runner.invoke(app, ["run", "--no-paste", "--no-overlay", "--config", str(cfg)])
    assert result.exit_code == 0
    assert captured.get("copy_only") is True


def test_run_help_documents_no_paste() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    # Strip ANSI escape codes before asserting on the option text.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "no-paste" in plain


def test_run_no_cleanup_flag_force_disables_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-cleanup` passes cleanup_enabled=False to the controller."""
    import seda.app as app_module

    captured: dict[str, object] = {}

    class _StubController:
        def __init__(self, config: object, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self) -> None:  # pragma: no cover - trivial stub
            pass

    monkeypatch.setattr(app_module, "AppController", _StubController)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[transcription]\nbackend = "fake"\n', encoding="utf-8")

    result = runner.invoke(app, ["run", "--no-cleanup", "--no-overlay", "--config", str(cfg)])
    assert result.exit_code == 0
    assert captured.get("cleanup_enabled") is False


def test_run_without_no_cleanup_leaves_cleanup_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import seda.app as app_module

    captured: dict[str, object] = {}

    class _StubController:
        def __init__(self, config: object, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self) -> None:  # pragma: no cover - trivial stub
            pass

    monkeypatch.setattr(app_module, "AppController", _StubController)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[transcription]\nbackend = "fake"\n', encoding="utf-8")

    result = runner.invoke(app, ["run", "--no-overlay", "--config", str(cfg)])
    assert result.exit_code == 0
    # No flag → None (config decides).
    assert captured.get("cleanup_enabled") is None


def test_run_help_documents_no_cleanup() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "no-cleanup" in plain


def test_run_help_documents_no_overlay() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "no-overlay" in plain


def test_run_scopes_the_semaphore_warning_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run` suppresses ONLY the benign leaked-semaphore warning (#29), not others."""
    import warnings

    import seda.app as app_module

    class _StubController:
        def __init__(self, config: object, **kwargs: object) -> None:
            pass

        def run(self) -> None:
            pass

    monkeypatch.setattr(app_module, "AppController", _StubController)
    monkeypatch.delenv("PYTHONWARNINGS", raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[transcription]\nbackend = "fake"\n', encoding="utf-8")

    # Snapshot filters so this test doesn't leak state into others.
    with warnings.catch_warnings():
        result = runner.invoke(app, ["run", "--no-overlay", "--config", str(cfg)])
        assert result.exit_code == 0

        # The exact semaphore message is suppressed in-process...
        assert warnings.filters and any(
            f[0] == "ignore"
            and f[2] is UserWarning
            and f[1] is not None
            and f[1].search("resource_tracker: There appear to be 3 leaked semaphore objects")
            for f in warnings.filters
        ), "run() must install an ignore filter matching the leaked-semaphore message"

        # ...but an unrelated warning is NOT matched by that same filter.
        assert not any(
            f[0] == "ignore" and f[1] is not None and f[1].search("some unrelated warning")
            for f in warnings.filters
            if f[2] is UserWarning
        ), "the filter must not match unrelated warnings"

    # ...and PYTHONWARNINGS is seeded so the resource_tracker SUBPROCESS (which a
    # parent filter can't reach) inherits the ignore at startup (#29).
    assert "ignore:resource_tracker:UserWarning" in os.environ.get("PYTHONWARNINGS", "")


def test_transcribe_scopes_the_semaphore_warning_filter(
    tmp_path: Path, make_wav: WavFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`transcribe` installs the SAME two-layer leaked-semaphore filter as `run` (#29)."""
    import warnings

    wav = make_wav([0, 1000, -1000, 0], sample_rate=16000)
    cfg = _fake_backend_config(tmp_path)
    monkeypatch.delenv("PYTHONWARNINGS", raising=False)

    # Snapshot filters so this test doesn't leak state into others.
    with warnings.catch_warnings():
        result = runner.invoke(app, ["transcribe", str(wav), "--stdout", "--config", str(cfg)])
        assert result.exit_code == 0

        # The exact semaphore message is suppressed in-process...
        assert warnings.filters and any(
            f[0] == "ignore"
            and f[2] is UserWarning
            and f[1] is not None
            and f[1].search("resource_tracker: There appear to be 3 leaked semaphore objects")
            for f in warnings.filters
        ), "transcribe() must install an ignore filter matching the leaked-semaphore message"

        # ...but an unrelated warning is NOT matched by that same filter.
        assert not any(
            f[0] == "ignore" and f[1] is not None and f[1].search("some unrelated warning")
            for f in warnings.filters
            if f[2] is UserWarning
        ), "the filter must not match unrelated warnings"

    # ...and PYTHONWARNINGS is seeded for the tracker subprocess, same as run (#29).
    assert "ignore:resource_tracker:UserWarning" in os.environ.get("PYTHONWARNINGS", "")


def test_semaphore_filter_preserves_an_existing_pythonwarnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator's own PYTHONWARNINGS is appended to, never clobbered (#29)."""
    from seda.cli import _silence_benign_semaphore_warning

    monkeypatch.setenv("PYTHONWARNINGS", "error::DeprecationWarning")
    _silence_benign_semaphore_warning()

    value = os.environ["PYTHONWARNINGS"]
    assert "error::DeprecationWarning" in value, "must not drop the operator's filter"
    assert "ignore:resource_tracker:UserWarning" in value, "must append the tracker ignore"


def test_semaphore_filter_is_idempotent_on_pythonwarnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling the helper twice does not duplicate the tracker filter in the env (#29)."""
    from seda.cli import _silence_benign_semaphore_warning

    monkeypatch.delenv("PYTHONWARNINGS", raising=False)
    _silence_benign_semaphore_warning()
    _silence_benign_semaphore_warning()

    assert os.environ["PYTHONWARNINGS"].count("ignore:resource_tracker:UserWarning") == 1


def test_run_no_overlay_flag_skips_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-overlay` must not even attempt the GUI host; it runs the terminal path."""
    import seda.app as app_module
    import seda.gui.host as host_module

    ran: list[str] = []
    host_calls: list[str] = []

    class _StubController:
        def __init__(self, config: object, **kwargs: object) -> None:
            pass

        def run(self) -> None:
            ran.append("run")

    monkeypatch.setattr(app_module, "AppController", _StubController)
    monkeypatch.setattr(
        host_module, "run_with_overlay", lambda controller, **kw: host_calls.append("host") or True
    )
    cfg = tmp_path / "config.toml"
    cfg.write_text('[transcription]\nbackend = "fake"\n', encoding="utf-8")

    result = runner.invoke(app, ["run", "--no-overlay", "--config", str(cfg)])
    assert result.exit_code == 0
    assert host_calls == [], "--no-overlay must skip run_with_overlay entirely"
    assert ran == ["run"], "the terminal path runs when the overlay is disabled"


def test_run_warns_when_accessibility_untrusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When macOS Accessibility is NOT granted, `run` prints a clear warning."""
    import seda.app as app_module
    import seda.input.accessibility as accessibility_module

    class _StubController:
        def __init__(self, config: object, **kwargs: object) -> None:
            pass

        def run(self) -> None:
            pass

    monkeypatch.setattr(app_module, "AppController", _StubController)
    monkeypatch.setattr(accessibility_module, "accessibility_trusted", lambda: False)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[transcription]\nbackend = "fake"\n', encoding="utf-8")

    result = runner.invoke(app, ["run", "--no-overlay", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "Accessibility" in result.output
    assert "Privacy & Security" in result.output


def test_run_silent_when_accessibility_trusted_or_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No accessibility warning when trusted (True) or unknown (None, e.g. non-macOS)."""
    import seda.app as app_module
    import seda.input.accessibility as accessibility_module

    class _StubController:
        def __init__(self, config: object, **kwargs: object) -> None:
            pass

        def run(self) -> None:
            pass

    monkeypatch.setattr(app_module, "AppController", _StubController)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[transcription]\nbackend = "fake"\n', encoding="utf-8")

    for probe in (True, None):
        monkeypatch.setattr(
            accessibility_module, "accessibility_trusted", lambda probe=probe: probe
        )
        result = runner.invoke(app, ["run", "--no-overlay", "--config", str(cfg)])
        assert result.exit_code == 0
        assert "Accessibility permission is not granted" not in result.output


def test_run_prints_config_migration_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run` surfaces the Local Flow → Seda config migration notice when present."""
    import seda.app as app_module
    import seda.config as config_module

    class _StubController:
        def __init__(self, config: object, **kwargs: object) -> None:
            pass

        def run(self) -> None:
            pass

    monkeypatch.setattr(app_module, "AppController", _StubController)
    # Override the autouse silencer for this test only.
    monkeypatch.setattr(
        config_module, "migration_notice", lambda: "found configuration under the old 'local-flow'"
    )
    cfg = tmp_path / "config.toml"
    cfg.write_text('[transcription]\nbackend = "fake"\n', encoding="utf-8")

    result = runner.invoke(app, ["run", "--no-overlay", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "local-flow" in result.output
    assert "notice:" in result.output


def test_run_falls_back_to_blocking_run_when_overlay_declines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the GUI host returns False, cli.run() runs the controller's own loop (ADR-0001)."""
    import seda.app as app_module
    import seda.gui.host as host_module

    ran: list[str] = []

    class _StubController:
        def __init__(self, config: object, **kwargs: object) -> None:
            pass

        def run(self) -> None:
            ran.append("run")

    monkeypatch.setattr(app_module, "AppController", _StubController)
    # Overlay declines (non-macOS / unavailable / stub) → fallback path taken.
    _force_macos_host(monkeypatch)
    monkeypatch.setattr(host_module, "run_with_overlay", lambda controller, **kw: False)
    cfg = tmp_path / "config.toml"
    # Request the overlay explicitly so the host is attempted on any platform
    # (ADR-0004: enabled=True wins over the platform default).
    cfg.write_text(
        '[transcription]\nbackend = "fake"\n[overlay]\nenabled = true\n', encoding="utf-8"
    )

    result = runner.invoke(app, ["run", "--config", str(cfg)])
    assert result.exit_code == 0
    assert ran == ["run"], "controller.run() should be the fallback when the overlay declines"


def test_run_propagates_when_overlay_host_raises_after_committing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the host commits to the run, a failure PROPAGATES — no fall-back retry.

    Regression: previously a raising host was caught and cli.run() re-ran
    controller.run(), double-crashing on a backend that fails to load. The host
    now fails open only for AppKit-setup failures (by returning False); a failure
    after commit (e.g. controller.start()) must surface once, not fall back.
    """
    import seda.app as app_module
    import seda.gui.host as host_module

    ran: list[str] = []

    class _StubController:
        def __init__(self, config: object, **kwargs: object) -> None:
            pass

        def run(self) -> None:
            ran.append("run")

    def _boom(controller: object, **kw: object) -> bool:
        raise RuntimeError("host blew up after committing")

    monkeypatch.setattr(app_module, "AppController", _StubController)
    _force_macos_host(monkeypatch)
    monkeypatch.setattr(host_module, "run_with_overlay", _boom)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[transcription]\nbackend = "fake"\n[overlay]\nenabled = true\n', encoding="utf-8"
    )

    result = runner.invoke(app, ["run", "--config", str(cfg)])
    # The error surfaces (non-zero exit) rather than falling back to run().
    assert result.exit_code != 0
    assert ran == [], "a committed-host failure must NOT fall back to controller.run()"


def test_run_does_not_call_blocking_run_when_overlay_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the overlay host owns the loop (returns True), the fallback run() is skipped."""
    import seda.app as app_module
    import seda.gui.host as host_module

    ran: list[str] = []

    class _StubController:
        def __init__(self, config: object, **kwargs: object) -> None:
            pass

        def run(self) -> None:  # pragma: no cover - must NOT be called
            ran.append("run")

    monkeypatch.setattr(app_module, "AppController", _StubController)
    _force_macos_host(monkeypatch)
    monkeypatch.setattr(host_module, "run_with_overlay", lambda controller, **kw: True)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[transcription]\nbackend = "fake"\n[overlay]\nenabled = true\n', encoding="utf-8"
    )

    result = runner.invoke(app, ["run", "--config", str(cfg)])
    assert result.exit_code == 0
    assert ran == [], "controller.run() must be skipped when the overlay host owns the loop"


def test_select_host_module_maps_known_platforms_and_none_otherwise() -> None:
    """`_select_host_module` maps darwin/win32 to their hosts; unknown → None (ADR-0009 §3)."""
    from seda.cli import _select_host_module

    assert _select_host_module("darwin") == "seda.gui.host"
    assert _select_host_module("win32") == "seda.gui.host_win"
    assert _select_host_module("linux") is None
    assert _select_host_module("") is None


def test_run_unknown_platform_runs_controller_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A platform with no host entry runs the terminal path — the host is never imported (A2).

    Even with the overlay explicitly requested, an unsupported platform must fall
    straight through to ``controller.run()`` and must not attempt any host import.
    """
    import seda.app as app_module

    ran: list[str] = []

    class _StubController:
        def __init__(self, config: object, **kwargs: object) -> None:
            pass

        def run(self) -> None:
            ran.append("run")

    monkeypatch.setattr(app_module, "AppController", _StubController)
    # A platform absent from _HOST_MODULES.
    monkeypatch.setattr("seda.cli.sys.platform", "linux")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[transcription]\nbackend = "fake"\n[overlay]\nenabled = true\n', encoding="utf-8"
    )

    result = runner.invoke(app, ["run", "--config", str(cfg)])
    assert result.exit_code == 0
    assert ran == ["run"], "an unsupported platform must run the controller's terminal loop"


def test_run_host_import_error_runs_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host module that fails to import degrades to the terminal path (B1).

    Simulates a broken/absent optional host (e.g. a PyObjC install failure, or the
    Windows host on a box missing a dependency): the ImportError is caught and the
    controller's own blocking loop runs, exactly as today.
    """
    import importlib

    import seda.app as app_module

    ran: list[str] = []

    class _StubController:
        def __init__(self, config: object, **kwargs: object) -> None:
            pass

        def run(self) -> None:
            ran.append("run")

    monkeypatch.setattr(app_module, "AppController", _StubController)
    _force_macos_host(monkeypatch)

    real_import_module = importlib.import_module

    def _failing_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "seda.gui.host":
            raise ImportError("simulated broken host module")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _failing_import)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[transcription]\nbackend = "fake"\n[overlay]\nenabled = true\n', encoding="utf-8"
    )

    result = runner.invoke(app, ["run", "--config", str(cfg)])
    assert result.exit_code == 0
    assert ran == ["run"], "a host ImportError must degrade to the terminal path"


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


# --- seda gui (#87) ---------------------------------------------------------


def _stub_appcontroller(monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
    """Replace AppController with a stub that records its kwargs and never runs."""
    import seda.app as app_module

    class _StubController:
        def __init__(self, config: object, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.setattr(app_module, "AppController", _StubController)


def test_gui_errors_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """seda gui is macOS-only: off-mac it errors and never touches the host (#87)."""
    monkeypatch.setattr("seda.cli.sys.platform", "linux")
    called: list[str] = []
    import seda.gui.host as host_module

    monkeypatch.setattr(
        host_module, "run_with_menu_bar", lambda *a, **k: called.append("host") or True
    )
    result = runner.invoke(app, ["gui"])
    assert result.exit_code != 0
    assert "macOS-only" in result.output
    assert called == [], "the host must not be invoked off macOS"


def _gui_config(tmp_path: Path) -> Path:
    """A minimal valid config file, so gui tests never read the real user config
    (which, under a darwin-faked sys.platform on a Windows CI runner, can raise)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[transcription]\nbackend = "fake"\n', encoding="utf-8")
    return cfg


def test_gui_hosts_on_macos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On macOS, gui builds the controller and hands it to run_with_menu_bar (#87)."""
    _force_macos_host(monkeypatch)
    _stub_appcontroller(monkeypatch, {})
    seen: dict[str, object] = {}
    import seda.gui.host as host_module

    def _fake_host(controller: object, **kwargs: object) -> bool:
        seen["controller"] = controller
        seen["kwargs"] = set(kwargs)
        return True

    monkeypatch.setattr(host_module, "run_with_menu_bar", _fake_host)
    result = runner.invoke(app, ["gui", "--config", str(_gui_config(tmp_path))])
    assert result.exit_code == 0
    assert "controller" in seen
    assert seen["kwargs"] == {"register_overlay", "register_status"}


def test_gui_errors_when_host_declines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the host declines (AppKit broke), gui errors — no headless fallback (#87)."""
    _force_macos_host(monkeypatch)
    captured: dict[str, object] = {}
    _stub_appcontroller(monkeypatch, captured)
    import seda.gui.host as host_module

    monkeypatch.setattr(host_module, "run_with_menu_bar", lambda *a, **k: False)
    result = runner.invoke(app, ["gui", "--config", str(_gui_config(tmp_path))])
    assert result.exit_code != 0
    assert "ran" not in captured, "gui must NOT fall back to controller.run() headless"


def test_gui_threads_flags_to_controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-paste / --no-cleanup thread through _build_controller into the controller (#87)."""
    _force_macos_host(monkeypatch)
    captured: dict[str, object] = {}
    _stub_appcontroller(monkeypatch, captured)
    import seda.gui.host as host_module

    monkeypatch.setattr(host_module, "run_with_menu_bar", lambda *a, **k: True)
    result = runner.invoke(
        app, ["gui", "--config", str(_gui_config(tmp_path)), "--no-paste", "--no-cleanup"]
    )
    assert result.exit_code == 0
    assert captured.get("copy_only") is True
    assert captured.get("cleanup_enabled") is False


def test_build_controller_shared_wiring(tmp_path: Path) -> None:
    """_build_controller returns a controller + a fan-out with a ConsoleNotifier (#87)."""
    from seda.cli import _build_controller
    from seda.notifications import ConsoleNotifier, FanOutNotifier

    controller, notifier, cfg = _build_controller(
        _gui_config(tmp_path), no_paste=False, no_cleanup=True
    )
    assert isinstance(notifier, FanOutNotifier)
    assert any(isinstance(n, ConsoleNotifier) for n in notifier._notifiers), (
        "the fan-out always carries a ConsoleNotifier"
    )
    assert controller is not None and cfg is not None
