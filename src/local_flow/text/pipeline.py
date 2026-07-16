"""Deterministic text-processing pipeline (IMPLEMENTATION_PLAN.md §14).

Pipeline stages (in order):
    1. Control-character sanitization
    2. Beginning-of-transcript mode/cancel command detection
    3. Spoken punctuation/path command substitution
    4. Technical-token protection
    5. Optional conservative filler removal
    6. Final whitespace normalization
    7. Restore protected technical tokens

The optional LLM cleanup stage (Phase 6) runs between steps 5 and 6 and is
not implemented here — this module hands off a ``PipelineResult`` that the
controller can pass to a cleanup provider before calling
:func:`finalize_after_cleanup`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from local_flow.text.commands import apply_commands
from local_flow.text.fillers import remove_fillers
from local_flow.text.sanitize import sanitize
from local_flow.text.technical_tokens import TokenRegistry, protect, restore

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

    # --- Stage 6 placeholder: LLM cleanup would go here ---

    # --- Stage 7: restore tokens + final normalization ---
    restored = restore(processed, registry)
    final = _normalize(restored)

    return PipelineResult(
        text=final,
        cancelled=False,
        mode_override=mode_override,
        effective_mode=effective_mode,
        commands_applied=commands_applied,
        token_registry=registry,
    )


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
