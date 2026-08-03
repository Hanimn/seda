"""Tests for logging configuration (see IMPLEMENTATION_PLAN.md §21)."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from seda.config import Config, load_config_from_dict
from seda.logging_config import (
    LOGGER_NAME,
    configure_logging,
    default_log_path,
    get_logger,
)


def test_configure_logging_sets_level(tmp_path: Path) -> None:
    logger = configure_logging(
        load_config_from_dict({"app": {"log_level": "DEBUG"}}), log_dir=tmp_path
    )
    assert logger.level == logging.DEBUG
    assert logger.name == LOGGER_NAME


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    config = Config()
    configure_logging(config, log_dir=tmp_path)
    first = len(logging.getLogger(LOGGER_NAME).handlers)
    configure_logging(config, log_dir=tmp_path)
    second = len(logging.getLogger(LOGGER_NAME).handlers)
    # Re-configuring must not stack duplicate handlers. Console + rotating file.
    assert first == second == 2


def test_transcript_logging_emits_startup_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # configure_logging replaces the logger's handlers with its own
    # stderr StreamHandler, so capture the process stderr rather than using
    # caplog (whose handler would be removed by configure_logging).
    config = load_config_from_dict({"app": {"log_transcripts": True}})
    configure_logging(config, log_dir=tmp_path)
    captured = capsys.readouterr()
    assert "log_transcripts" in captured.err


def test_get_logger_returns_named_logger() -> None:
    assert get_logger().name == LOGGER_NAME


# --- Durable file logging (#85) --------------------------------------------


def test_configure_logging_attaches_a_rotating_file_handler(tmp_path: Path) -> None:
    """Both a console and a rotating file handler are installed (#85)."""
    configure_logging(Config(), log_dir=tmp_path)
    handlers = logging.getLogger(LOGGER_NAME).handlers
    file_handlers = [h for h in handlers if isinstance(h, RotatingFileHandler)]
    stream_handlers = [
        h
        for h in handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
    ]
    assert len(file_handlers) == 1, "exactly one rotating file handler"
    assert len(stream_handlers) == 1, "the console handler is kept alongside the file"


def test_file_handler_writes_to_seda_log_under_log_dir(tmp_path: Path) -> None:
    """The file lands at <log_dir>/seda.log and a logged line reaches it (#85)."""
    logger = configure_logging(
        load_config_from_dict({"app": {"log_level": "INFO"}}), log_dir=tmp_path
    )
    logger.info("hud transitioned to LISTENING")
    log_file = tmp_path / "seda.log"
    assert log_file.exists(), "the rotating file handler creates <log_dir>/seda.log"
    assert "hud transitioned to LISTENING" in log_file.read_text()


def test_file_handler_rotates_with_bounded_backups(tmp_path: Path) -> None:
    """The handler is a size-bounded rotator with a finite backup count (#85)."""
    configure_logging(Config(), log_dir=tmp_path)
    fh = next(
        h for h in logging.getLogger(LOGGER_NAME).handlers if isinstance(h, RotatingFileHandler)
    )
    assert fh.maxBytes > 0, "rotation is size-bounded (not an unbounded file)"
    assert fh.backupCount > 0, "a finite number of rotated backups is kept"


def test_configure_logging_creates_missing_log_dir(tmp_path: Path) -> None:
    """A not-yet-existing log dir is created rather than failing (#85)."""
    nested = tmp_path / "does" / "not" / "exist" / "yet"
    assert not nested.exists()
    logger = configure_logging(Config(), log_dir=nested)
    logger.info("x")
    assert (nested / "seda.log").exists()


def test_unwritable_log_dir_fails_open_to_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the file handler cannot be created, logging still works via console (#85).

    A logging *sink* problem must never break the app — the console handler is
    always installed, and a failure to attach the file handler is swallowed but
    surfaced with a warning so the user isn't left silently fileless.
    """

    def _boom(*_a: object, **_k: object) -> RotatingFileHandler:
        # A non-OSError on purpose: the sink must be FULLY fail-open, not just
        # for OSError (Windows pathlib can raise NotImplementedError, etc.).
        raise NotImplementedError("odd platform")

    monkeypatch.setattr("seda.logging_config._OwnerOnlyRotatingFileHandler", _boom)
    logger = configure_logging(Config(), log_dir=tmp_path)  # must not raise
    handlers = logging.getLogger(LOGGER_NAME).handlers
    # Console survives; no file handler attached.
    assert any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in handlers
    )
    assert not any(isinstance(h, RotatingFileHandler) for h in handlers)
    logger.info("still logging")  # must not raise
    # The user is warned they are fileless, not left silent.
    assert "logging to console only" in capsys.readouterr().err


def test_default_log_path_is_under_platform_log_dir() -> None:
    """default_log_path() mirrors default_config_path(): platform log dir / seda.log (#85)."""
    p = default_log_path()
    assert p.name == "seda.log"
    # The parent is the platformdirs user-log dir for 'seda'; assert the app segment.
    assert "seda" in p.parent.as_posix().lower()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file-mode semantics")
def test_log_file_is_owner_only_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The log file is *born* owner-only (0600), even under a permissive umask (#85).

    It can contain transcript text when app.log_transcripts is enabled, so it
    must not be world/group-readable on a shared host. Setting a 0o022 umask
    (which would otherwise create the file 0644) pins that the mode comes from
    the opener at creation, not from a post-hoc chmod (which would leave a
    world-readable TOCTOU window).
    """
    import os
    import stat

    old = os.umask(0o022)
    try:
        configure_logging(Config(), log_dir=tmp_path)
    finally:
        os.umask(old)
    mode = stat.S_IMODE((tmp_path / "seda.log").stat().st_mode)
    assert mode == 0o600, f"log file must be born 0600, got {oct(mode)}"


def test_reconfigure_closes_old_file_handlers_no_fd_leak(tmp_path: Path) -> None:
    """Repeated configure_logging closes the old file handler — no leaked fds (#85).

    A RotatingFileHandler holds an OPEN file; removing without closing would
    orphan a file descriptor on every reconfigure (run/transcribe each call it).
    Grab a strong ref to the attached stream BEFORE reconfiguring and assert it
    is closed after — hunting for a GC-orphaned handler is vacuous because
    refcounting finalizes the detached handler before gc.collect() can see it.
    """
    configure_logging(Config(), log_dir=tmp_path)
    old_fh = next(
        h for h in logging.getLogger(LOGGER_NAME).handlers if isinstance(h, RotatingFileHandler)
    )
    old_stream = old_fh.stream  # strong ref to the OPEN file object
    assert old_stream is not None and not old_stream.closed

    configure_logging(Config(), log_dir=tmp_path)  # reconfigure

    # The previously-attached handler's stream must have been closed on removal,
    # not left dangling — this fails if configure_logging drops handler.close().
    assert old_stream.closed, "the old file handler's stream must be closed on reconfigure"
    live = [
        h for h in logging.getLogger(LOGGER_NAME).handlers if isinstance(h, RotatingFileHandler)
    ]
    assert len(live) == 1, "exactly one live file handler remains"


def test_file_and_console_share_formatter_and_level(tmp_path: Path) -> None:
    """The file logs exactly what the console logs: same formatter, no own level (#85)."""
    configure_logging(Config(), log_dir=tmp_path)
    handlers = logging.getLogger(LOGGER_NAME).handlers
    file_h = next(h for h in handlers if isinstance(h, RotatingFileHandler))
    console_h = next(
        h
        for h in handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
    )
    # Same Formatter instance -> identical format; neither filters independently.
    assert file_h.formatter is console_h.formatter
    assert file_h.level == console_h.level == logging.NOTSET


def test_file_line_is_formatted_not_bare_message(tmp_path: Path) -> None:
    """A written file line carries the shared level+name prefix, not just the raw message (#85)."""
    logger = configure_logging(
        load_config_from_dict({"app": {"log_level": "INFO"}}), log_dir=tmp_path
    )
    logger.info("hud transitioned to LISTENING")
    line = (tmp_path / "seda.log").read_text()
    assert "INFO seda: hud transitioned to LISTENING" in line
