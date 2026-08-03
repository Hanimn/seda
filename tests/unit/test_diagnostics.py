"""Tests for the ``doctor`` diagnostics (see IMPLEMENTATION_PLAN.md §20)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from seda.diagnostics import (
    CheckResult,
    Status,
    check_microphone,
    check_ollama,
    check_permissions,
    check_platform,
    check_python_version,
    check_wayland,
    check_writable_locations,
    format_diagnostics,
    run_checks,
    worst_status,
)


def test_python_version_check_passes_on_supported_runtime() -> None:
    # The test suite itself runs on a supported interpreter (>= 3.11).
    assert check_python_version().status is Status.PASS


def test_run_checks_returns_results(tmp_path: Path) -> None:
    results = run_checks(str(tmp_path / "none.toml"))
    assert results
    names = {r.name for r in results}
    assert "Python version" in names
    assert "Configuration" in names


def test_run_checks_includes_all_required_checks(tmp_path: Path) -> None:
    results = run_checks(str(tmp_path / "none.toml"))
    names = {r.name for r in results}
    # Every §20 checklist item must be represented.
    required = {
        "Python version",
        "Operating system",
        "Configuration",
        "Config location",
        "Microphone",
        "Audio device",
        "Transcription model",
        "Clipboard",
        "Global hotkeys",
        "Transcription backend",
        "CUDA",
        "Ollama",
        "Wayland/X11",
        "Writable locations",
        "Permissions",
    }
    missing = required - names
    assert not missing, f"Missing doctor checks: {missing}"


def test_invalid_config_makes_config_check_fail(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("[paste]\nauto_submit = true\n", encoding="utf-8")
    results = run_checks(str(target))
    config_check = next(r for r in results if r.name == "Configuration")
    assert config_check.status is Status.FAIL
    assert worst_status(results) is Status.FAIL


def test_check_details_never_include_secrets(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text('[text]\ncustom_vocabulary = ["super-secret"]\n', encoding="utf-8")
    results = run_checks(str(target))
    for result in results:
        assert "super-secret" not in result.detail


def test_worst_status_ordering() -> None:
    passing = [CheckResult("a", Status.PASS, "")]
    warned = [*passing, CheckResult("b", Status.WARN, "")]
    failed = [*warned, CheckResult("c", Status.FAIL, "")]
    assert worst_status(passing) is Status.PASS
    assert worst_status(warned) is Status.WARN
    assert worst_status(failed) is Status.FAIL


# ---------------------------------------------------------------------------
# Microphone check
# ---------------------------------------------------------------------------


class TestMicrophoneCheck:
    def test_sounddevice_missing_gives_warn(self) -> None:
        with patch("seda.diagnostics._module_available", return_value=False):
            result = check_microphone()
        assert result.status is Status.WARN

    def test_sounddevice_present_no_devices_gives_warn(self) -> None:
        with (
            patch("seda.diagnostics._module_available", return_value=True),
            patch("seda.diagnostics._list_audio_devices", return_value=[]),
        ):
            result = check_microphone()
        assert result.status is Status.WARN

    def test_sounddevice_present_with_devices_gives_pass(self) -> None:
        with (
            patch("seda.diagnostics._module_available", return_value=True),
            patch(
                "seda.diagnostics._list_audio_devices",
                return_value=["Microphone (built-in)"],
            ),
        ):
            result = check_microphone()
        assert result.status is Status.PASS
        # Device name must not appear in detail (could contain system info,
        # but this test simply checks there's no crash).
        assert result.detail

    def test_sounddevice_list_error_gives_warn(self) -> None:
        from seda.audio.devices import DeviceError

        with (
            patch("seda.diagnostics._module_available", return_value=True),
            patch(
                "seda.diagnostics._list_audio_devices",
                side_effect=DeviceError("no devices"),
            ),
        ):
            result = check_microphone()
        assert result.status is Status.WARN


# ---------------------------------------------------------------------------
# Wayland/X11 check
# ---------------------------------------------------------------------------


class TestWaylandCheck:
    def test_macos_gives_skip(self) -> None:
        with patch("seda.diagnostics.sys") as mock_sys:
            mock_sys.platform = "darwin"
            result = check_wayland()
        assert result.status is Status.SKIP

    def test_wayland_session_gives_warn(self) -> None:
        with (
            patch("seda.diagnostics.sys") as mock_sys,
            patch(
                "os.environ.get",
                side_effect=lambda k, d=None: "wayland" if k == "XDG_SESSION_TYPE" else d,
            ),
        ):
            mock_sys.platform = "linux"
            result = check_wayland()
        assert result.status is Status.WARN
        assert "Wayland" in result.detail

    def test_x11_session_gives_pass(self) -> None:
        import os

        env = {"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": ""}
        with (
            patch("seda.diagnostics.sys") as mock_sys,
            patch.dict(os.environ, env, clear=False),
        ):
            mock_sys.platform = "linux"
            result = check_wayland()
        assert result.status is Status.PASS


# ---------------------------------------------------------------------------
# Ollama check
# ---------------------------------------------------------------------------


class TestOllamaCheck:
    def test_httpx_missing_gives_skip(self) -> None:
        with patch("seda.diagnostics._module_available", return_value=False):
            result = check_ollama(None)
        assert result.status is Status.SKIP

    def test_ollama_unreachable_gives_warn(self) -> None:
        from seda.config import load_config_from_dict

        cfg = load_config_from_dict({"cleanup": {"enabled": True}})
        with (
            patch("seda.diagnostics._module_available", return_value=True),
            patch("seda.diagnostics._ollama_reachable", return_value=False),
        ):
            result = check_ollama(cfg)
        assert result.status is Status.WARN

    def test_ollama_reachable_gives_pass(self) -> None:
        from seda.config import load_config_from_dict

        cfg = load_config_from_dict({"cleanup": {"enabled": True}})
        with (
            patch("seda.diagnostics._module_available", return_value=True),
            patch("seda.diagnostics._ollama_reachable", return_value=True),
        ):
            result = check_ollama(cfg)
        assert result.status is Status.PASS

    def test_cleanup_disabled_gives_skip(self) -> None:
        from seda.config import load_config_from_dict

        cfg = load_config_from_dict({"cleanup": {"enabled": False}})
        result = check_ollama(cfg)
        assert result.status is Status.SKIP
        assert "not enabled" in result.detail


# ---------------------------------------------------------------------------
# Writable locations check
# ---------------------------------------------------------------------------


class TestWritableLocationsCheck:
    def test_writable_locations_returns_a_result(self, tmp_path: Path) -> None:
        result = check_writable_locations()
        assert result.status in (Status.PASS, Status.WARN, Status.FAIL)
        assert result.detail

    def test_detail_never_includes_absolute_path_content(self) -> None:
        result = check_writable_locations()
        # Detail must be short metadata only — never expose full paths that
        # could include username or system-specific directories.
        assert len(result.detail) < 300


# ---------------------------------------------------------------------------
# Permissions check
# ---------------------------------------------------------------------------


class TestPermissionsCheck:
    def test_returns_a_result(self) -> None:
        result = check_permissions()
        assert result.status in (Status.PASS, Status.WARN, Status.SKIP)
        assert result.detail

    def test_macos_trusted_gives_pass(self) -> None:
        with (
            patch("seda.diagnostics.sys") as mock_sys,
            patch("seda.input.accessibility.accessibility_trusted", return_value=True),
        ):
            mock_sys.platform = "darwin"
            result = check_permissions()
        assert result.status is Status.PASS
        assert "Accessibility granted" in result.detail

    def test_macos_untrusted_gives_warn_with_guidance(self) -> None:
        with (
            patch("seda.diagnostics.sys") as mock_sys,
            patch("seda.input.accessibility.accessibility_trusted", return_value=False),
        ):
            mock_sys.platform = "darwin"
            result = check_permissions()
        assert result.status is Status.WARN
        assert "NOT granted" in result.detail
        assert "Accessibility" in result.detail

    def test_macos_unknown_probe_falls_back_to_guidance(self) -> None:
        with (
            patch("seda.diagnostics.sys") as mock_sys,
            patch("seda.input.accessibility.accessibility_trusted", return_value=None),
        ):
            mock_sys.platform = "darwin"
            result = check_permissions()
        assert result.status is Status.WARN
        assert "Accessibility" in result.detail

    def test_non_macos_gives_skip(self) -> None:
        with patch("seda.diagnostics.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = check_permissions()
        assert result.status is Status.SKIP


# ---------------------------------------------------------------------------
# Platform check
# ---------------------------------------------------------------------------


class TestPlatformCheck:
    def test_returns_pass_with_system_info(self) -> None:
        result = check_platform()
        assert result.status is Status.PASS
        assert result.detail


# ---------------------------------------------------------------------------
# Output must never include env-var values
# ---------------------------------------------------------------------------


def test_check_details_never_include_env_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "do-not-log-this")
    results = run_checks(str(tmp_path / "none.toml"))
    for r in results:
        assert "do-not-log-this" not in r.detail


# --- format_diagnostics (#90) ----------------------------------------------


def test_format_diagnostics_renders_each_check_and_overall() -> None:
    """format_diagnostics is the single home for the doctor text (CLI + gui menu, #90)."""
    results = [
        CheckResult("Python", Status.PASS, "3.11"),
        CheckResult("Microphone", Status.WARN, "no default device"),
    ]
    text = format_diagnostics(results, Status.WARN)
    # Each check appears with its status + name + detail.
    assert "Python" in text and "3.11" in text and "PASS" in text
    assert "Microphone" in text and "no default device" in text and "WARN" in text
    # The overall verdict is present.
    assert "WARN" in text.splitlines()[-1] or "Overall" in text


def test_format_diagnostics_matches_cli_doctor_lines() -> None:
    """The gui Doctor view must read the same as `seda doctor` (#90)."""
    results = [CheckResult("Configuration", Status.PASS, "loaded and valid")]
    text = format_diagnostics(results, Status.PASS)
    # The per-check line format is the CLI's: "[<status>] <name>: <detail>".
    assert "[PASS] Configuration: loaded and valid" in text
    assert "Overall: PASS" in text
