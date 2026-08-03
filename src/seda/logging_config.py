"""Logging configuration.

Structured, privacy-conscious logging (see IMPLEMENTATION_PLAN.md §21). By
default the application logs metadata only — state transitions, durations,
character counts, error types — and never transcript text, audio samples, or
clipboard contents. Transcript logging is a separate, explicit opt-in
(``app.log_transcripts``) that emits a startup warning when enabled.

Two sinks are installed on the same logger: a **console** ``StreamHandler`` and a
**durable rotating file** handler (``<log_dir>/seda.log``, default
``platformdirs.user_log_path`` — ``~/Library/Logs/seda`` on macOS). The file
exists so a non-terminal launcher (the menu-bar ``seda gui`` app, #83) has
something for its "Open Logs" action to open. Both handlers share one logger, so
the file logs exactly what the console logs — the metadata-only discipline and
the ``log_transcripts`` gate live at the call sites, not the sink, so the file
handler adds no new privacy surface. A file-sink failure (read-only dir, etc.)
is swallowed: a logging problem must never break the app, so the console handler
is always installed and the file handler is best-effort.
"""

from __future__ import annotations

import io
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import cast

from platformdirs import user_log_path

from seda.config import APP_NAME, Config

LOGGER_NAME = "seda"

# Rotation policy: cap each file at 1 MiB and keep 5 rotated backups (~6 MiB
# total). A dictation tool logs sparse metadata lines, so this holds a long
# history without unbounded growth.
_LOG_MAX_BYTES = 1024 * 1024
_LOG_BACKUP_COUNT = 5

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"

# Owner-only mode for the log file. It can contain transcript text when
# app.log_transcripts is enabled (and metadata always), so confine it to the
# owning user rather than the default umask (typically 0644, world-readable).
_LOG_FILE_MODE = 0o600


class _OwnerOnlyRotatingFileHandler(RotatingFileHandler):
    """A ``RotatingFileHandler`` whose files are *created* mode 0600 (owner-only).

    ``_open`` is the single point every file passes through — the initial file
    *and* each rotated backup created by ``doRollover``. It opens through an
    ``os.open`` *opener* that passes ``mode=0o600`` to the creating syscall, so
    the file is owner-only **from birth** — there is no window where it exists
    world-readable (a chmod-after-open would leave a TOCTOU gap). ``os.open``'s
    mode is still masked by the process umask, which can only *remove* bits, so
    the file is never wider than 0600. POSIX-only; on Windows the mode arg is
    effectively ignored by the OS, which is a harmless no-op.
    """

    @staticmethod
    def _owner_only_opener(path: str, flags: int) -> int:
        return os.open(path, flags, _LOG_FILE_MODE)

    def _open(self) -> io.TextIOWrapper:
        # Mirror FileHandler._open, but create the file 0600 atomically via the
        # opener rather than chmod'ing after the fact. Text mode ("a") yields a
        # TextIOWrapper; open()'s type widens to IO[Any] once an opener is passed.
        return cast(
            "io.TextIOWrapper",
            open(  # noqa: SIM115 -- handler owns the stream's lifetime
                self.baseFilename,
                self.mode,
                encoding=self.encoding,
                errors=self.errors,
                opener=self._owner_only_opener,
            ),
        )


def default_log_path() -> Path:
    """Return the platform-appropriate default log file path.

    Mirrors :func:`seda.config.default_config_path`, using
    :func:`platformdirs.user_log_path`, which resolves to
    ``~/Library/Logs/seda`` on macOS, ``~/.local/state/seda/log`` (or
    ``$XDG_STATE_HOME``) on Linux, and ``%LOCALAPPDATA%\\seda\\Logs`` on Windows.
    """
    return user_log_path(APP_NAME, appauthor=False) / "seda.log"


def configure_logging(config: Config, *, log_dir: Path | None = None) -> logging.Logger:
    """Configure and return the application's root logger.

    Installs a console ``StreamHandler`` and a rotating file handler at
    ``<log_dir>/seda.log`` (``log_dir`` defaults to :func:`default_log_path`'s
    parent). *log_dir* is injectable so tests can point the file at a temp dir.

    Idempotent: repeated calls replace handlers rather than stacking them, so
    tests and re-entrant CLI invocations don't duplicate log lines.

    Fail-open on the file sink: if the log directory cannot be created or the
    file handler cannot be opened (e.g. a read-only filesystem), the failure is
    swallowed and only the console handler is installed — a logging-sink problem
    must never break dictation.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(config.app.log_level)

    # Replace any existing handlers so the configuration is deterministic.
    # Close each before dropping it — a RotatingFileHandler holds an OPEN file,
    # so removing without closing would leak a file descriptor on every
    # (re)configure (run/transcribe both call this, and tests call it repeatedly).
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(fmt=_LOG_FORMAT, datefmt=_LOG_DATEFMT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    # Durable rotating file sink (best-effort — see the fail-open note above).
    directory = log_dir if log_dir is not None else default_log_path().parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = _OwnerOnlyRotatingFileHandler(
            directory / "seda.log",
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception:  # noqa: BLE001 -- a logging *sink* must be fully fail-open
        # A logging sink problem must NEVER break the app — not just OSError:
        # mkdir/open/the opener can raise other errors on odd platforms
        # (e.g. NotImplementedError from pathlib on Windows). Keep the console
        # handler; note the miss on it so the user isn't left silently fileless.
        logger.warning("could not open the log file under %s; logging to console only", directory)

    logger.propagate = False

    if config.app.log_transcripts:
        # Explicit, deliberately loud: the user has opted into logging content
        # that is normally kept out of logs for privacy.
        logger.warning(
            "app.log_transcripts is enabled: transcript text will be written "
            "to logs. Disable it to keep transcripts out of logs."
        )

    return logger


def get_logger() -> logging.Logger:
    """Return the application logger (assumes :func:`configure_logging` ran)."""
    return logging.getLogger(LOGGER_NAME)
