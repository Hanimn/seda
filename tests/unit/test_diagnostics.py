"""Tests for the ``doctor`` diagnostics (see IMPLEMENTATION_PLAN.md §20)."""

from __future__ import annotations

from pathlib import Path

from local_flow.diagnostics import Status, check_python_version, run_checks, worst_status


def test_python_version_check_passes_on_supported_runtime() -> None:
    # The test suite itself runs on a supported interpreter (>= 3.11).
    assert check_python_version().status is Status.PASS


def test_run_checks_returns_results(tmp_path: Path) -> None:
    results = run_checks(str(tmp_path / "none.toml"))
    assert results
    names = {r.name for r in results}
    assert "Python version" in names
    assert "Configuration" in names


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
    from local_flow.diagnostics import CheckResult

    passing = [CheckResult("a", Status.PASS, "")]
    warned = [*passing, CheckResult("b", Status.WARN, "")]
    failed = [*warned, CheckResult("c", Status.FAIL, "")]
    assert worst_status(passing) is Status.PASS
    assert worst_status(warned) is Status.WARN
    assert worst_status(failed) is Status.FAIL
