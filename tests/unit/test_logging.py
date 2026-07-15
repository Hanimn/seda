"""Tests for logging configuration (see IMPLEMENTATION_PLAN.md §21)."""

from __future__ import annotations

import logging

import pytest

from local_flow.config import Config, load_config_from_dict
from local_flow.logging_config import LOGGER_NAME, configure_logging, get_logger


def test_configure_logging_sets_level() -> None:
    logger = configure_logging(load_config_from_dict({"app": {"log_level": "DEBUG"}}))
    assert logger.level == logging.DEBUG
    assert logger.name == LOGGER_NAME


def test_configure_logging_is_idempotent() -> None:
    config = Config()
    configure_logging(config)
    first = len(logging.getLogger(LOGGER_NAME).handlers)
    configure_logging(config)
    second = len(logging.getLogger(LOGGER_NAME).handlers)
    # Re-configuring must not stack duplicate handlers.
    assert first == second == 1


def test_transcript_logging_emits_startup_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # configure_logging replaces the logger's handlers with its own
    # stderr StreamHandler, so capture the process stderr rather than using
    # caplog (whose handler would be removed by configure_logging).
    config = load_config_from_dict({"app": {"log_transcripts": True}})
    configure_logging(config)
    captured = capsys.readouterr()
    assert "log_transcripts" in captured.err


def test_get_logger_returns_named_logger() -> None:
    assert get_logger().name == LOGGER_NAME
