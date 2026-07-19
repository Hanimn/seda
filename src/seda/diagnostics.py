"""Diagnostics for the ``doctor`` command (see IMPLEMENTATION_PLAN.md §20).

Each check returns a :class:`CheckResult` with a ``PASS`` / ``WARN`` / ``FAIL``
/ ``SKIP`` status and a short, human-readable detail. Output must never include
secrets, transcript content, clipboard content, or environment variable values.

The full §20 checklist covered here:

- Python version
- OS and architecture
- Configuration validity
- Microphone availability + device count
- Selected audio device
- Clipboard support
- Global hotkey support
- Transcription backend (faster-whisper)
- CUDA availability
- Ollama availability (when cleanup is enabled)
- Wayland/X11 status (Linux only)
- Writable cache/config locations
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from enum import StrEnum
from importlib.util import find_spec
from pathlib import Path

from platformdirs import user_cache_path, user_config_path

from seda import __version__
from seda.config import APP_NAME, Config, ConfigError, default_config_path, load_config

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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _module_available(module: str) -> bool:
    """Whether an import would succeed, without importing (and its side effects)."""
    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _list_audio_devices() -> list[str]:
    """Return input device names; raises :class:`~seda.audio.devices.DeviceError`
    if sounddevice cannot enumerate devices."""
    from seda.audio.devices import list_devices

    return [d.name for d in list_devices() if d.input_channels > 0]


def _ollama_reachable(base_url: str) -> bool:
    """Best-effort probe: True if Ollama answers /api/tags quickly."""
    if not _module_available("httpx"):
        return False
    import httpx

    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=1.5)
    except Exception:  # noqa: BLE001
        return False
    return bool(response.status_code < 400)


def _cuda_available() -> bool:
    """True if at least one CUDA device is visible via CTranslate2."""
    try:
        from ctranslate2 import get_cuda_device_count

        return bool(get_cuda_device_count() > 0)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


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
    """PASS if ``module`` is importable, else WARN with ``warn_detail``."""
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
    """Enumerate audio input devices and report count/status."""
    if not _module_available("sounddevice"):
        return CheckResult("Microphone", Status.WARN, "sounddevice not installed")
    try:
        names = _list_audio_devices()
    except Exception as exc:  # noqa: BLE001 – DeviceError or sounddevice init failure
        return CheckResult(
            "Microphone", Status.WARN, f"device enumeration failed: {type(exc).__name__}"
        )

    count = len(names)
    if count == 0:
        return CheckResult("Microphone", Status.WARN, "no input devices detected")
    plural = "device" if count == 1 else "devices"
    return CheckResult("Microphone", Status.PASS, f"{count} input {plural} found")


def check_audio_device(config: Config | None) -> CheckResult:
    """Report the configured audio input device (§20 "Selected audio device")."""
    if not _module_available("sounddevice"):
        return CheckResult("Audio device", Status.SKIP, "sounddevice not installed")
    device = (config.audio.device if config else "") or ""
    label = f"'{device}'" if device else "system default"
    return CheckResult("Audio device", Status.PASS, f"configured device: {label}")


def check_transcription_model(config: Config | None) -> CheckResult:
    """Report the configured transcription model (§20 "Model availability").

    A full on-disk cache check requires loading the backend at startup cost,
    so we report the configured model name and skip the cache probe here. Use
    ``seda models list-local`` to verify what is cached.
    """
    if not _module_available("faster_whisper"):
        return CheckResult("Transcription model", Status.SKIP, "whisper extra not installed")
    if config is None:
        return CheckResult("Transcription model", Status.SKIP, "config unavailable")
    model = config.transcription.model or config.transcription.model_path or "(not set)"
    return CheckResult(
        "Transcription model",
        Status.PASS,
        f"configured: {model} (run 'models download' to ensure it is cached)",
    )


def check_permissions() -> CheckResult:
    """Report macOS permission state (§20 "Required permissions").

    Accessibility (input monitoring) *can* be probed without a system prompt via
    ``AXIsProcessTrusted`` — this is the permission global hotkeys actually
    require, so we report it concretely (PASS/WARN). Microphone (TCC) state
    cannot be probed without triggering a prompt, so it stays advisory.
    """
    if sys.platform != "darwin":
        return CheckResult(
            "Permissions",
            Status.SKIP,
            "permission checks are only implemented for macOS; "
            "see docs/TROUBLESHOOTING.md for platform-specific guidance",
        )

    from seda.input.accessibility import accessibility_trusted

    trusted = accessibility_trusted()
    mic_note = "also verify Microphone permission (System Settings → Privacy & Security)"
    if trusted is True:
        return CheckResult(
            "Permissions",
            Status.PASS,
            f"Accessibility granted (global hotkeys enabled); {mic_note}",
        )
    if trusted is False:
        return CheckResult(
            "Permissions",
            Status.WARN,
            "Accessibility NOT granted — global hotkeys will not work; enable this app "
            "under System Settings → Privacy & Security → Accessibility, then restart. "
            "See docs/TROUBLESHOOTING.md",
        )
    # Probe unavailable (e.g. HIServices import failed): fall back to guidance.
    return CheckResult(
        "Permissions",
        Status.WARN,
        "macOS: verify Accessibility and Microphone permission for the app you launch "
        "seda from (System Settings → Privacy & Security); see docs/TROUBLESHOOTING.md",
    )


def check_clipboard() -> CheckResult:
    return _check_dependency("Clipboard", "pyperclip", "pyperclip not installed")


def check_hotkeys() -> CheckResult:
    return _check_dependency("Global hotkeys", "pynput", "pynput not installed")


def check_cuda() -> CheckResult:
    """Check CUDA availability via CTranslate2 (fail-soft)."""
    if not _module_available("ctranslate2"):
        return CheckResult("CUDA", Status.SKIP, "ctranslate2 not installed (whisper extra)")
    if _cuda_available():
        return CheckResult("CUDA", Status.PASS, "CUDA device(s) available")
    return CheckResult("CUDA", Status.WARN, "no CUDA devices; transcription will use CPU")


def check_ollama(config: Config | None) -> CheckResult:
    """Check Ollama reachability (only relevant when cleanup is enabled)."""
    if config is None or not config.cleanup.enabled:
        return CheckResult("Ollama", Status.SKIP, "cleanup not enabled")
    if not _module_available("httpx"):
        return CheckResult("Ollama", Status.SKIP, "httpx not installed (cleanup extra)")
    base_url = config.cleanup.ollama.base_url
    if _ollama_reachable(base_url):
        model = config.cleanup.ollama.model
        return CheckResult("Ollama", Status.PASS, f"reachable; model={model}")
    return CheckResult(
        "Ollama",
        Status.WARN,
        "cleanup is enabled but Ollama is not reachable (start Ollama or disable cleanup)",
    )


def check_wayland() -> CheckResult:
    """Warn on Wayland; global hotkeys and input simulation may not work."""
    if sys.platform not in ("linux", "linux2"):
        return CheckResult("Wayland/X11", Status.SKIP, "not Linux")

    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    wayland_display = os.environ.get("WAYLAND_DISPLAY", "")

    if session == "wayland" or wayland_display:
        return CheckResult(
            "Wayland/X11",
            Status.WARN,
            "Wayland session detected — global hotkeys and paste simulation may not work; "
            "see docs/TROUBLESHOOTING.md",
        )
    if session in ("x11", "xcb") or os.environ.get("DISPLAY"):
        return CheckResult("Wayland/X11", Status.PASS, f"X11 session ({session or 'DISPLAY set'})")
    return CheckResult(
        "Wayland/X11",
        Status.WARN,
        "cannot detect display session type (XDG_SESSION_TYPE not set)",
    )


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


def check_writable_locations() -> CheckResult:
    """Verify cache and config directories are writable (or creatable)."""
    issues: list[str] = []
    locations = {
        "config": user_config_path(APP_NAME, appauthor=False),
        "cache": user_cache_path(APP_NAME, appauthor=False),
    }
    for label, path in locations.items():
        if path.exists():
            if not os.access(path, os.W_OK):
                issues.append(f"{label} dir not writable")
        else:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:
                issues.append(f"cannot create {label} dir")

    if issues:
        return CheckResult("Writable locations", Status.WARN, "; ".join(issues))
    return CheckResult("Writable locations", Status.PASS, "config and cache directories accessible")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_checks(config_path: str | None = None) -> list[CheckResult]:
    """Run all diagnostics and return their results in display order."""
    config: Config | None = None
    config_error: str | None = None
    try:
        config = load_config(Path(config_path) if config_path else None)
    except ConfigError as exc:
        config_error = str(exc)

    return [
        CheckResult("Seda version", Status.PASS, __version__),
        check_python_version(),
        check_platform(),
        check_wayland(),
        check_config(config, config_error),
        check_config_location(),
        check_writable_locations(),
        check_microphone(),
        check_audio_device(config),
        check_transcription_model(config),
        check_clipboard(),
        check_hotkeys(),
        check_transcription_backend(config),
        check_cuda(),
        check_ollama(config),
        check_permissions(),
    ]


def worst_status(results: list[CheckResult]) -> Status:
    """Return the most severe status across ``results`` (FAIL > WARN > PASS/SKIP)."""
    if any(r.status is Status.FAIL for r in results):
        return Status.FAIL
    if any(r.status is Status.WARN for r in results):
        return Status.WARN
    return Status.PASS
