"""Typed exception hierarchy (see IMPLEMENTATION_PLAN.md §23).

All application errors derive from :class:`LocalFlowError`. Expected,
user-facing errors carry a concise message suitable for printing without a
stack trace; tracebacks are shown only under a ``--debug`` flag by the caller.

Only the exceptions needed so far are defined; the remaining §23 names are
added as the phases that raise them land.
"""

from __future__ import annotations


class LocalFlowError(Exception):
    """Base class for all Local Flow errors.

    The string form is a concise, user-safe message. It must never embed
    transcript text, clipboard contents, or secrets.
    """


class ConfigurationError(LocalFlowError):
    """Configuration is invalid or could not be loaded."""


class AudioError(LocalFlowError):
    """An audio file could not be read or decoded."""


class EmptyAudioError(AudioError):
    """A recording contains no usable audio (see §23 recovery policy).

    A subclass of :class:`AudioError` so existing ``except AudioError`` sites
    still catch it, while callers that need the §23 "empty audio → notify and
    return to idle" behavior can distinguish it.
    """


class ModelUnavailableError(LocalFlowError):
    """The requested transcription model is not available.

    Raised, in particular, when offline mode forbids a download and the model
    is not present locally.
    """


class TranscriptionError(LocalFlowError):
    """Transcription failed for a reason other than a missing model."""


class HotkeyError(LocalFlowError):
    """Hotkey registration or listener failure."""


class InvalidTransitionError(LocalFlowError):
    """Attempted an illegal state-machine transition."""
