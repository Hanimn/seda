"""Logging configuration.

Structured, privacy-conscious logging (see IMPLEMENTATION_PLAN.md §21). By
default the application logs metadata only — state transitions, durations,
character counts, error types — and never transcript text, audio samples, or
clipboard contents. Transcript logging is a separate, explicit opt-in
(``app.log_transcripts``) that emits a startup warning when enabled.
"""

from __future__ import annotations

import logging

from seda.config import Config

LOGGER_NAME = "seda"


def configure_logging(config: Config) -> logging.Logger:
    """Configure and return the application's root logger.

    Idempotent: repeated calls replace handlers rather than stacking them, so
    tests and re-entrant CLI invocations don't duplicate log lines.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(config.app.log_level)

    # Replace any existing handlers so the configuration is deterministic.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logger.addHandler(handler)
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
