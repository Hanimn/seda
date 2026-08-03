"""The ``seda`` command-line interface (see IMPLEMENTATION_PLAN.md §20).

Phase 0 implements the non-hardware commands — ``version``, ``config``, and
``doctor`` — end to end. Phase 2 adds ``devices`` and ``test-mic``.
Commands that need global hotkeys are declared as stubs.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from seda import __version__
from seda.config import (
    Config,
    ConfigError,
    default_config_path,
    load_config,
    render_toml,
)
from seda.diagnostics import Status, format_diagnostics, run_checks, worst_status
from seda.errors import ModelUnavailableError, SedaError
from seda.logging_config import configure_logging, get_logger

if TYPE_CHECKING:
    from seda.app import AppController
    from seda.config import Config
    from seda.gui.host import Overlay
    from seda.notifications import FanOutNotifier

# Platform → GUI-host module (ADR-0009 §3). Each host module exposes a
# ``run_with_overlay``-shaped entry point and its own ``Overlay`` struct; the
# sibling hosts share only the ``OverlayNotifier`` seam and the ``_hostloop``
# lifecycle helper. An unknown platform maps to ``None`` → the terminal path,
# so a platform without a host never even imports one. The ``-> bool`` return
# of the resolved host is the single fail-open signal ``run`` keys on, produced
# by the shared ``run_hosted`` helper (identical on every platform).
_HOST_MODULES = {"darwin": "seda.gui.host", "win32": "seda.gui.host_win"}


def _select_host_module(plat: str) -> str | None:
    """Return the GUI-host module name for *plat*, or ``None`` if unsupported."""
    return _HOST_MODULES.get(plat)


def _silence_benign_semaphore_warning() -> None:
    """Suppress the benign leaked-semaphore warning any backend-loading path emits.

    Emitted at interpreter shutdown by a native dependency (ctranslate2/
    faster-whisper spins native worker pools; the multiprocessing
    resource_tracker then can't account for a semaphore it didn't create). The
    OS reclaims it — it never affects dictation, and it does not accumulate (one
    long-lived model per process). See #29.

    Two layers, because the warning can fire in two different interpreters:

    - ``PYTHONWARNINGS`` in the environment — the warning is emitted from the
      *resource_tracker's own spawned process*, a separate interpreter a parent
      ``warnings.filterwarnings`` can never reach. That subprocess reads
      ``PYTHONWARNINGS`` at startup, so seeding the env var **before** the tracker
      spawns (i.e. before any backend import registers a semaphore) is the only
      lever that silences it. The env-filter mini-language matches the message by
      substring, so the ``resource_tracker:`` prefix is as tight as it allows —
      broader than the regex below, but confined to that one tracker message. Only
      set if unset, so an operator's own ``PYTHONWARNINGS`` is never clobbered.
    - ``warnings.filterwarnings`` in *this* process — the surgical #29 filter,
      scoped to the EXACT message + category + module, catching anything the
      tracker emits in-process. Do not broaden this filter.
    """
    import os
    import warnings

    _tracker_filter = "ignore:resource_tracker:UserWarning"
    existing = os.environ.get("PYTHONWARNINGS")
    if existing is None:
        os.environ["PYTHONWARNINGS"] = _tracker_filter
    elif _tracker_filter not in existing:
        # Append so we never drop the operator's own filters.
        os.environ["PYTHONWARNINGS"] = f"{existing},{_tracker_filter}"

    warnings.filterwarnings(
        "ignore",
        message=r"resource_tracker: There appear to be \d+ leaked semaphore objects",
        category=UserWarning,
        module=r"multiprocessing\.resource_tracker",
    )


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
    name="seda",
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
    """Print the Seda version."""
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
        typer.echo(format_diagnostics(results, overall))

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


def _build_controller(
    config: Path | None, *, no_paste: bool, no_cleanup: bool
) -> tuple[AppController, FanOutNotifier, Config]:
    """Shared Phase-1 wiring for ``run`` and ``gui`` (ADR-0003).

    Loads config, configures logging, and builds the ``AppController`` behind a
    ``FanOutNotifier([ConsoleNotifier])`` so the overlay (and, for ``gui``, the
    status item) can be added as further sinks. Returns ``(controller, notifier,
    cfg)`` — the notifier fan-out is returned so the caller can ``.add`` more
    sinks, and ``cfg`` so the caller can read overlay/notification settings.
    """
    from seda.app import AppController
    from seda.notifications import ConsoleNotifier, FanOutNotifier

    cfg = _safe_load(config)
    configure_logging(cfg)
    # ``--no-cleanup`` force-disables cleanup; otherwise the config flag decides.
    cleanup_enabled = None if not no_cleanup else False
    notifier = FanOutNotifier([ConsoleNotifier(enabled=cfg.notifications.console_enabled)])
    controller = AppController(
        cfg, copy_only=no_paste, cleanup_enabled=cleanup_enabled, notifier=notifier
    )
    return controller, notifier, cfg


@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", help="Path to a config file."),
    no_paste: bool = typer.Option(
        False,
        "--no-paste",
        help="Copy the transcript to the clipboard without simulating paste.",
    ),
    no_cleanup: bool = typer.Option(
        False,
        "--no-cleanup",
        help="Disable optional LLM cleanup for this run, regardless of config.",
    ),
    no_overlay: bool = typer.Option(
        False,
        "--no-overlay",
        help="Disable the macOS recording overlay for this run, regardless of config.",
    ),
) -> None:
    """Run the background dictation loop."""
    # Seed the env-var half of the semaphore-warning filter (#29) before any
    # backend import can spawn the resource_tracker subprocess it must reach.
    _silence_benign_semaphore_warning()

    from seda.config import migration_notice, select_overlay_enabled

    controller, notifier, cfg = _build_controller(config, no_paste=no_paste, no_cleanup=no_cleanup)

    # Surface the macOS Accessibility permission gap up front (both run paths
    # block below). Without it, pynput installs its event tap but receives no
    # keys, so push-to-talk silently does nothing — pynput only logs a cryptic
    # "not trusted" line. A clear, actionable warning here saves the confusion.
    # Non-macOS / unknown probe → stay silent (returns None).
    _warn_if_accessibility_untrusted()

    # After the Local Flow → Seda rename the config dir moved; if the user still
    # has config under the old name, point them at it once (fail-open).
    _notice = migration_notice()
    if _notice is not None:
        _err(f"notice: {_notice}")

    logger = get_logger()
    # Resolve whether the overlay is *requested* (ADR-0004): --no-overlay >
    # explicit config > platform. Even when requested, the GUI host still fails
    # open if AppKit is unavailable (ADR-0001), so a non-macOS request is
    # neutralized there. When not requested, skip the host entirely.
    overlay_requested = select_overlay_enabled(cfg.overlay, no_overlay=no_overlay)

    hosted = False
    if overlay_requested:
        # Select the GUI host by platform (ADR-0009 §3). An unknown platform →
        # None → terminal path (the host is never imported). The chosen host
        # owns the main thread and drives the controller; run_with_overlay()
        # fails open by RETURNING False (unsupported platform or a toolkit setup
        # failure) — in which case we run the controller's own blocking loop
        # below, exactly as before. Once the host has committed to the run
        # (toolkit up, controller.start() called), a failure is the controller's
        # own error and PROPAGATES — we must not fall back and re-run start().
        # Only a failure importing the optional host module is treated as
        # "overlay unavailable".
        import importlib

        host_module_name = _select_host_module(sys.platform)
        if host_module_name is None:
            logger.info("no overlay host for platform %r; running in terminal mode", sys.platform)
        else:
            try:
                host_module = importlib.import_module(host_module_name)
                from seda.notifications import OverlayNotifier
            except ImportError:
                logger.info("overlay host unavailable; running in terminal mode")
            else:

                def _register(overlay: Overlay) -> None:
                    # Wire the built panel into the fan-out (ADR-0003): RECORDING ->
                    # show + listening mode, BUSY -> show + busy mode (fired on
                    # release), terminal -> hide, all marshalled onto the main thread.
                    notifier.add(
                        OverlayNotifier(
                            show=overlay.show,
                            hide=overlay.hide,
                            set_mode=overlay.set_mode,
                            dispatch_main=overlay.dispatch_main,
                        )
                    )

                hosted = host_module.run_with_overlay(controller, register_overlay=_register)
    if not hosted:
        controller.run()


@app.command()
def gui(
    config: Path | None = typer.Option(None, "--config", help="Path to a config file."),
    no_paste: bool = typer.Option(
        False,
        "--no-paste",
        help="Copy the transcript to the clipboard without simulating paste.",
    ),
    no_cleanup: bool = typer.Option(
        False,
        "--no-cleanup",
        help="Disable optional LLM cleanup for this run, regardless of config.",
    ),
) -> None:
    """Run the macOS menu-bar app (status item + Quit)."""
    _silence_benign_semaphore_warning()

    # gui's only feature is the NSStatusBar item, so off-macOS there is nothing to
    # degrade into — error clearly and point at `run` (unlike run's fail-open).
    if sys.platform != "darwin":
        _err("seda gui is macOS-only; use `seda run` for terminal dictation.")
        raise typer.Exit(code=1)

    from seda.config import migration_notice

    controller, notifier, _cfg = _build_controller(config, no_paste=no_paste, no_cleanup=no_cleanup)
    _warn_if_accessibility_untrusted()
    _notice = migration_notice()
    if _notice is not None:
        _err(f"notice: {_notice}")

    import seda.gui.host as host  # lazy — AppKit only touched inside the loop
    from seda.notifications import HudMode, OverlayNotifier

    # The status item's apply sink only exists once the item is built (inside the
    # host loop). A mutable holder lets the composed set_mode reach it; a mode that
    # arrives before the item exists is a harmless no-op that self-heals on the next
    # event (mirrors the HUD's one-shot show latch).
    status: dict[str, Callable[[HudMode], None]] = {"apply": lambda _m: None}

    def _register_status(apply: Callable[[HudMode], None]) -> None:
        status["apply"] = apply

    def _register_overlay(overlay: Overlay) -> None:
        # Compose ONE set_mode that fans the same HudMode to the HUD and the status
        # item. The event->HudMode mapping stays single-sourced in OverlayNotifier;
        # both surfaces run in its single marshalled main-thread turn (ADR-0003).
        def _set_mode(mode: HudMode) -> None:
            overlay.set_mode(mode)
            status["apply"](mode)

        notifier.add(
            OverlayNotifier(
                show=overlay.show,
                hide=overlay.hide,
                set_mode=_set_mode,
                dispatch_main=overlay.dispatch_main,
            )
        )

    hosted = host.run_with_menu_bar(
        controller,
        register_overlay=_register_overlay,
        register_status=_register_status,
    )
    if not hosted:
        # macOS but AppKit genuinely failed to build (fail-open inside run_hosted).
        # gui has nothing to fall back INTO — surface it rather than run headless.
        _err("could not start the macOS menu-bar app (AppKit unavailable).")
        raise typer.Exit(code=1)


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
    # Seed the semaphore-warning filter (#29) before the backend import below can
    # spawn the resource_tracker subprocess the env-var half must reach.
    _silence_benign_semaphore_warning()

    # Deferred imports keep `--help` and the config/doctor commands from paying
    # for numpy/backend import cost on every invocation.
    from seda.audio.loading import load_wav
    from seda.transcription.factory import create_backend

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
    except SedaError as exc:
        _err(str(exc))
        raise typer.Exit(code=int(ExitCode.AUDIO)) from exc

    backend = create_backend(loaded_config, offline=offline)
    try:
        backend.load()
        result = backend.transcribe(audio.samples, audio.sample_rate)
    except ModelUnavailableError as exc:
        _err(str(exc))
        raise typer.Exit(code=int(ExitCode.MODEL)) from exc
    except SedaError as exc:
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
def devices(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List audio input devices."""
    from seda.audio.devices import DeviceError, list_devices

    try:
        devs = list_devices()
    except DeviceError as exc:
        _err(str(exc))
        raise typer.Exit(code=int(ExitCode.AUDIO)) from exc
    except Exception as exc:  # noqa: BLE001
        _err(f"could not list audio devices: {exc}")
        raise typer.Exit(code=int(ExitCode.AUDIO)) from exc

    if json_output:
        payload = [
            {
                "index": d.index,
                "name": d.name,
                "input_channels": d.input_channels,
                "default_sample_rate": d.default_sample_rate,
                "default": d.is_default,
            }
            for d in devs
        ]
        typer.echo(json.dumps(payload, indent=2))
    else:
        for d in devs:
            marker = " (default)" if d.is_default else ""
            typer.echo(
                f"{d.index:>3}  {d.name}"
                f"  [{d.input_channels}ch, {d.default_sample_rate:.0f} Hz]{marker}"
            )


@app.command(name="test-mic")
def test_mic(
    duration: float = typer.Option(5.0, "--duration", help="Recording length in seconds."),
    save: Path | None = typer.Option(None, "--save", help="Save the recording to this WAV file."),
    device: str | None = typer.Option(None, "--device", help="Device index or name."),
) -> None:
    """Record a short sample and report microphone levels."""
    import time
    import wave

    import numpy as np

    from seda.audio.recorder import (
        RecorderConfig,
        RecordingTooShortError,
        SounddeviceRecorder,
    )

    cfg = RecorderConfig(
        device=device,
        max_duration_seconds=duration,
    )
    recorder = SounddeviceRecorder(cfg)

    typer.echo(f"Recording for {duration:.0f} s … (speak now)")
    try:
        recorder.start()
        time.sleep(duration)
        audio = recorder.stop()
    except RecordingTooShortError as exc:
        _err(str(exc))
        raise typer.Exit(code=int(ExitCode.AUDIO)) from exc
    except Exception as exc:  # noqa: BLE001
        _err(f"microphone error: {exc}")
        raise typer.Exit(code=int(ExitCode.AUDIO)) from exc

    peak_pct = audio.peak_level * 100
    rms_pct = float(np.sqrt(np.mean(audio.samples.astype(np.float64) ** 2))) * 100
    typer.echo(f"Duration : {audio.duration_seconds:.2f} s")
    typer.echo(f"Peak     : {peak_pct:.1f}%{'  *** CLIPPING ***' if audio.clipping else ''}")
    typer.echo(f"RMS      : {rms_pct:.1f}%")
    typer.echo(f"Speech   : {'yes' if audio.speech_detected else 'no (check mic level)'}")
    if audio.overflow_count:
        typer.echo(f"Overflows: {audio.overflow_count} (check system audio load)", err=True)

    if save is not None:
        samples_int16 = (audio.samples * 32767).clip(-32768, 32767).astype(np.int16)
        try:
            with wave.open(str(save), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(audio.sample_rate)
                wf.writeframes(samples_int16.tobytes())
        except OSError as exc:
            _err(f"could not write WAV file {save}: {exc}")
            raise typer.Exit(code=int(ExitCode.AUDIO)) from exc
        typer.echo(f"Saved    : {save}")


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


def _warn_if_accessibility_untrusted() -> None:
    """Print a clear, actionable warning if macOS Accessibility is not granted.

    Best-effort and silent on non-macOS or an unknown probe result — only warns
    when we positively know the process is *not* trusted (see
    :mod:`seda.input.accessibility`).
    """
    from seda.input.accessibility import ACCESSIBILITY_HELP, accessibility_trusted

    if accessibility_trusted() is False:
        _err(f"warning: {ACCESSIBILITY_HELP}")


if __name__ == "__main__":
    app()
