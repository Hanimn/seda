"""Cleanup provider interface and metrics (IMPLEMENTATION_PLAN.md §10, §15).

An optional, local LLM cleanup stage that turns dictated speech into cleaner
prose *without answering it*. It is disabled by default, bypassed entirely in
literal mode, and fail-open: any error or rejected output falls back to the
deterministic transcript (see :mod:`local_flow.cleanup.validation`).

The :class:`CleanupProvider` protocol is the seam between the controller and a
concrete provider (Ollama, or the no-op / fake used in tests), mirroring the
:class:`~local_flow.transcription.base.TranscriptionBackend` pattern.

Only *aggregate* metrics are ever recorded (§15 "Optional comparison
safeguard", §21): character counts, edit ratio, placeholder count, and the
validation outcome — never the transcript, the prompt, or the response body.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class CleanupProvider(Protocol):
    """A pluggable prose-cleanup provider (§10)."""

    def is_available(self) -> bool:
        """Return whether the provider can currently service a request.

        Best-effort and side-effect-free (e.g. a cheap reachability check); a
        provider that returns ``True`` may still fail in :meth:`clean`.
        """
        ...

    def clean(self, transcript: str, mode: str, vocabulary: list[str]) -> str:
        """Return a cleaned version of *transcript*.

        *transcript* still contains opaque technical-token placeholders, which
        must be preserved exactly. *mode* is ``"standard"`` or ``"polished"``
        (never ``"literal"`` — the caller bypasses cleanup in literal mode).
        *vocabulary* is the custom-term list, offered to the model as context.

        Raises :class:`~local_flow.errors.CleanupError` on transport/timeout
        failure.
        """
        ...


@dataclass(frozen=True)
class CleanupMetrics:
    """Aggregate, content-free metrics for one cleanup attempt (§15).

    Safe to log: holds counts and a ratio plus the sanitized validation-result
    name, never any transcript, prompt, or response text.
    """

    input_chars: int
    output_chars: int
    edit_ratio: float
    placeholder_count: int
    validation: str  # ValidationReason.value


@dataclass
class CleanupCounters:
    """Running, content-free cleanup outcome counters (§28).

    Bundled so the three sibling counts that always move and report together
    live in one place rather than as loose attributes on the controller.
    """

    succeeded: int = 0
    failed: int = 0
    validation_failures: int = 0


class NoopCleanupProvider:
    """A provider that returns the transcript unchanged.

    Selected by ``cleanup.provider = "noop"``. Useful as an explicit "cleanup
    enabled but do nothing" setting and as a trivial always-available provider.
    """

    def is_available(self) -> bool:
        return True

    def clean(self, transcript: str, mode: str, vocabulary: list[str]) -> str:
        return transcript
