"""Diagnostics for the ``doctor`` command (see IMPLEMENTATION_PLAN.md §20).

Each check returns a :class:`CheckResult` with a ``PASS`` / ``WARN`` / ``FAIL``
/ ``SKIP`` status and a short, human-readable detail. Output must never
include secrets, transcript content, clipboard content, or environment
variable values.

Phase 0 has no audio/model/hotkey implementation yet, so checks that would
need those subsystems report ``SKIP`` (with the reason) rather than failing.
They are filled in as later phases land.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from enum import StrEnum
from importlib.util import find_spec
from pathlib import Path

from local_flow import __version__
from local_flow.config import Config, ConfigError, default_config_path, load_config

MIN_PYTHON = (3, 11)


class Status(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status.value, "detail": self.detail}


def _module_available(module: str) -> bool:
    """Whether an import would succeed, without importing (and its side effects)."""
    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def check_python_version() -> CheckResult:
    version = ".".join(str(p) for p in sys.version_info[:3])
    if sys.version_info[:2] >= MIN_PYTHON:
        return CheckResult("Python version", Status.PASS, f"Python {version}")
    required = ".".join(str(p) for p in MIN_PYTHON)
    return CheckResult("Python version", Status.FAIL, f"Python {version} (requires >= {required})")


def check_platform() -> CheckResult:
    detail = f"{platform.system()} {platform.release()} ({platform.machine()})"
    return CheckResult("Operating system", Status.PASS, detail)


def check_config(config: Config | None, config_error: str | None) -> CheckResult:
    if config_error is not None:
        return CheckResult("Configuration", Status.FAIL, config_error.splitlines()[0])
    return CheckResult("Configuration", Status.PASS, "loaded and valid")


def _check_dependency(name: str, module: str, warn_detail: str) -> CheckResult:
    """PASS if ``module`` is importable, else WARN with ``warn_detail``.

    Optional and native dependencies are expected to be absent in a base
    Phase-0 install, so their absence is a WARN (actionable) rather than a FAIL.
    """
    if _module_available(module):
        return CheckResult(name, Status.PASS, f"{module} importable")
    return CheckResult(name, Status.WARN, warn_detail)


def check_transcription_backend(config: Config | None) -> CheckResult:
    return _check_dependency(
        "Transcription backend",
        "faster_whisper",
        "faster-whisper not installed (install the 'whisper' extra)",
    )


def check_microphone() -> CheckResult:
    if not _module_available("sounddevice"):
        return CheckResult("Microphone", Status.WARN, "sounddevice not installed")
    # Enumerating devices is a Phase 2 concern; the dependency being present is
    # all we can honestly assert at Phase 0 without touching hardware.
    return CheckResult("Microphone", Status.SKIP, "device enumeration not implemented yet")


def check_clipboard() -> CheckResult:
    return _check_dependency("Clipboard", "pyperclip", "pyperclip not installed")


def check_hotkeys() -> CheckResult:
    return _check_dependency("Global hotkeys", "pynput", "pynput not installed")


def check_config_location() -> CheckResult:
    path = default_config_path()
    parent = path.parent
    if path.exists():
        return CheckResult("Config location", Status.PASS, "config file present")
    if parent.exists():
        return CheckResult("Config location", Status.WARN, "no config file yet (run 'config init')")
    return CheckResult(
        "Config location",
        Status.WARN,
        "config directory does not exist yet (run 'config init')",
    )


def run_checks(config_path: str | None = None) -> list[CheckResult]:
    """Run all diagnostics and return their results in display order."""
    config: Config | None = None
    config_error: str | None = None
    try:
        config = load_config(Path(config_path) if config_path else None)
    except ConfigError as exc:
        config_error = str(exc)

    return [
        CheckResult("Local Flow version", Status.PASS, __version__),
        check_python_version(),
        check_platform(),
        check_config(config, config_error),
        check_config_location(),
        check_microphone(),
        check_clipboard(),
        check_hotkeys(),
        check_transcription_backend(config),
    ]


def worst_status(results: list[CheckResult]) -> Status:
    """Return the most severe status across ``results`` (FAIL > WARN > PASS/SKIP)."""
    if any(r.status is Status.FAIL for r in results):
        return Status.FAIL
    if any(r.status is Status.WARN for r in results):
        return Status.WARN
    return Status.PASS
