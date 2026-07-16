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


class ClipboardError(LocalFlowError):
    """The clipboard could not be read or written."""


class PasteError(LocalFlowError):
    """A simulated paste keystroke could not be delivered.

    When raised during insertion the transcript is left on the clipboard and
    the prior clipboard is *not* restored (see §16 "Paste failure").
    """


class CleanupError(LocalFlowError):
    """The optional LLM cleanup provider was unavailable or failed.

    Covers transport failures, timeouts, and connection errors talking to the
    local cleanup endpoint. Cleanup is fail-open: callers catch this and fall
    back to the deterministic transcript (see §15 "Fail open").

    Note: rejected *output* (bad placeholders, prefaces, over-expansion) is not
    an exception — :mod:`local_flow.cleanup.validation` reports it as a
    content-free ``ValidationReason`` so the reason can be logged without
    leaking transcript or model text.
    """


class InvalidTransitionError(LocalFlowError):
    """Attempted an illegal state-machine transition."""
