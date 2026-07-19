"""Deterministic text-processing pipeline (IMPLEMENTATION_PLAN.md §14).

Pipeline stages (in order):
    1. Control-character sanitization
    2. Beginning-of-transcript mode/cancel command detection
    3. Spoken punctuation/path command substitution
    4. Technical-token protection
    5. Optional conservative filler removal
    6. Final whitespace normalization
    7. Restore protected technical tokens

The optional LLM cleanup stage (Phase 6) runs at step 6: the controller hands
the protected text (:attr:`PipelineResult.protected_text`, placeholders still
in place) to a cleanup provider, then calls :func:`finalize_after_cleanup` to
run steps 7 (restore) and the final normalization. When no cleanup runs,
:func:`process_transcript` calls :func:`finalize_after_cleanup` itself so
:attr:`PipelineResult.text` is the ready-to-paste deterministic result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from seda.text.commands import apply_commands
from seda.text.fillers import remove_fillers
from seda.text.sanitize import sanitize
from seda.text.technical_tokens import TokenRegistry, protect, restore

Mode = Literal["literal", "standard", "polished"]

# ---------------------------------------------------------------------------
# Beginning-of-transcript control commands
# ---------------------------------------------------------------------------

# Commands that switch the processing mode for this dictation only.
_MODE_COMMANDS: dict[str, Mode] = {
    "literal mode": "literal",
    "standard mode": "standard",
    "polished mode": "polished",
}
# Commands that discard the entire dictation.
_CANCEL_COMMANDS: frozenset[str] = frozenset({"scratch that", "cancel dictation"})

# Maximum number of words at the start of the transcript to scan for commands.
_BOT_WINDOW = 4


def _detect_bot_command(
    text: str,
) -> tuple[Mode | None, bool, str]:
    """Detect and strip a beginning-of-transcript control command.

    Returns ``(mode_override, cancelled, remaining_text)``.
    ``mode_override`` is ``None`` when no mode command was found.
    ``cancelled`` is ``True`` when a cancel command was found.
    """
    # Strip leading whitespace/punctuation before looking.
    stripped = text.lstrip()

    # We only look within the first few words.
    words = stripped.split()
    if not words:
        return None, False, text

    window = " ".join(words[:_BOT_WINDOW]).lower()

    for phrase, mode in _MODE_COMMANDS.items():
        if window.startswith(phrase):
            # Consume the phrase + any trailing space.
            n_words = len(phrase.split())
            rest = " ".join(words[n_words:])
            return mode, False, rest

    for phrase in _CANCEL_COMMANDS:
        if window.startswith(phrase):
            return None, True, ""

    return None, False, text


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """Output of :func:`process_transcript`."""

    text: str
    cancelled: bool = False
    mode_override: Mode | None = None
    # The mode that was actually used for processing (override takes priority).
    effective_mode: Mode = "standard"
    # Number of spoken commands that were replaced.
    commands_applied: int = 0
    # The token registry from technical-token protection (for Phase 6 handoff).
    token_registry: TokenRegistry | None = None
    # The pre-restore text with technical tokens still replaced by opaque
    # placeholders. An optional LLM cleanup provider (Phase 6) operates on THIS
    # (placeholders must survive cleanup, §14), then the controller calls
    # :func:`finalize_after_cleanup` to restore tokens and normalize. Empty for
    # cancelled/empty transcripts.
    protected_text: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def process_transcript(
    text: str,
    mode: Mode = "standard",
    *,
    spoken_commands_enabled: bool = True,
) -> PipelineResult:
    """Run the full deterministic pipeline on *text*.

    Parameters
    ----------
    text:
        Raw transcript from the transcription backend.
    mode:
        Base processing mode.  A beginning-of-transcript command can override
        this for the current dictation.
    spoken_commands_enabled:
        When ``False``, skip the spoken-command substitution stage.
    """
    # --- Stage 1: sanitize ---
    text = sanitize(text)

    if not text.strip():
        return PipelineResult(text=text.strip(), effective_mode=mode)

    # --- Stage 2: beginning-of-transcript commands ---
    mode_override, cancelled, text = _detect_bot_command(text)

    if cancelled:
        return PipelineResult(
            text="",
            cancelled=True,
            effective_mode=mode,
        )

    effective_mode: Mode = mode_override if mode_override is not None else mode

    # --- Stage 3: spoken commands ---
    commands_applied = 0
    if spoken_commands_enabled:
        cmd_result = apply_commands(text, mode=effective_mode)
        text = cmd_result.text
        commands_applied = cmd_result.commands_applied

    # --- Stage 4: technical-token protection ---
    protected_text, registry = protect(text)

    # --- Stage 5: filler removal ---
    processed = remove_fillers(protected_text, mode=effective_mode)

    # --- Stage 6: optional LLM cleanup ---
    # Not run here. The controller may hand ``processed`` (which still contains
    # opaque placeholders) to a cleanup provider and then call
    # :func:`finalize_after_cleanup`. When no cleanup runs, we finalize the
    # deterministic result immediately so ``text`` is the ready-to-paste string.

    # --- Stage 7: restore tokens + final normalization ---
    final = finalize_after_cleanup(processed, registry)

    return PipelineResult(
        text=final,
        cancelled=False,
        mode_override=mode_override,
        effective_mode=effective_mode,
        commands_applied=commands_applied,
        token_registry=registry,
        protected_text=processed,
    )


def finalize_after_cleanup(protected_text: str, registry: TokenRegistry | None) -> str:
    """Restore protected technical tokens and apply final normalization.

    This is the single restore site for the pipeline. It is called by
    :func:`process_transcript` for the no-cleanup path, and by the controller
    after an optional LLM cleanup stage has run on the protected text.

    ``protected_text`` must still contain every placeholder from ``registry``,
    unchanged and in order — :func:`~seda.text.technical_tokens.restore`
    raises :class:`~seda.text.technical_tokens.ProtectionError` otherwise,
    which the controller treats as a cleanup-validation failure and falls back
    to the deterministic transcript. When ``registry`` is ``None`` (nothing was
    protected) the text is normalized as-is.
    """
    if registry is None:
        return _normalize(protected_text)
    restored = restore(protected_text, registry)
    return _normalize(restored)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Final whitespace normalization."""
    # Collapse multiple ordinary spaces (preserve newlines).
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Strip leading/trailing spaces and tabs (not newlines — they are intentional).
    text = text.strip(" \t")
    return text
