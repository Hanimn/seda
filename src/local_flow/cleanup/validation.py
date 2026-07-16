"""Cleanup output validation (IMPLEMENTATION_PLAN.md §15, §26).

Before accepting an LLM's cleaned output we validate it against a strict reject
list (§15 "Cleanup validation"). The checks are deliberately conservative — the
system prompt and low temperature are the first line of defence, and this is the
second; §26 stresses that none of it makes cleanup "perfectly safe", so literal
mode (which bypasses cleanup entirely) remains the safest mode.

Validation returns a :class:`ValidationReason` — a *sanitized* code, never the
offending content — so the controller can log the outcome and fall back to the
deterministic transcript without leaking transcript or model text (§21, §36).

Placeholder integrity is the load-bearing check. Note that
:func:`local_flow.text.technical_tokens.restore` catches missing/duplicated/
reordered placeholders but tolerates *extra* unknown ones; here we additionally
reject any placeholder the registry never issued (an LLM inventing tokens), so a
fabricated placeholder can never reach restore.
"""

from __future__ import annotations

import re
from enum import StrEnum

from local_flow.text.technical_tokens import PH_PATTERN, TokenRegistry, placeholder_for

# Assistant prefaces that signal the model broke the output-only contract
# (§15). Matched case-insensitively at the very start of the output. ASCII —
# real model output uses straight quotes/casing, not the doc's typographic ones.
_PREFACES: tuple[str, ...] = (
    "sure",
    "here is",
    "here's",
    "here are",
    "certainly",
    "i can help",
    "i'd be happy",
    "of course",
)

# Openings that suggest the model answered/acted on the dictation rather than
# cleaning it (§15 "Output appears to answer the prompt"). Best-effort only —
# §26 is explicit that no heuristic makes cleanup "perfectly safe"; literal mode
# remains the safe path. Matched case-insensitively at the start of the output.
_ANSWER_OPENERS: tuple[str, ...] = (
    "the answer is",
    "to do this",
    "to fix",
    "you should",
    "you can",
    "you need to",
    "i would recommend",
    "i recommend",
    "the solution is",
    "the best way",
    "first,",
    "step 1",
    "```",  # a code block is an answer/action, never a cleaned transcript
)

# §15 length rule: reject output substantially longer than input.
_LENGTH_MULTIPLIER = 1.75
_LENGTH_MARGIN = 200


class ValidationReason(StrEnum):
    """Why cleanup output was accepted or rejected — a content-free code."""

    OK = "OK"
    EMPTY_OUTPUT = "EMPTY_OUTPUT"
    WHITESPACE = "WHITESPACE"
    PLACEHOLDER_MISSING = "PLACEHOLDER_MISSING"
    PLACEHOLDER_DUP = "PLACEHOLDER_DUP"
    PLACEHOLDER_REORDER = "PLACEHOLDER_REORDER"
    PLACEHOLDER_EXTRA = "PLACEHOLDER_EXTRA"
    TOO_LONG = "TOO_LONG"
    PREFACE = "PREFACE"
    APPARENT_ANSWER = "APPARENT_ANSWER"


def validate_placeholders(text: str, registry: TokenRegistry | None) -> ValidationReason:
    """Check that *text* preserves exactly the placeholders in *registry*.

    Rejects missing, duplicated, reordered, or *extra* (never-issued)
    placeholders. Returns :attr:`ValidationReason.OK` when intact (including the
    no-registry / no-placeholder case).
    """
    if registry is None or registry.count == 0:
        # Nothing was protected; but the model must not have invented tokens.
        if PH_PATTERN.search(text):
            return ValidationReason.PLACEHOLDER_EXTRA
        return ValidationReason.OK

    expected = registry.placeholders_in_order()
    expected_set = set(expected)
    found = [placeholder_for(p, int(i)) for p, i in PH_PATTERN.findall(text)]
    found_set = set(found)

    # Extra: any found placeholder the registry never issued.
    if found_set - expected_set:
        return ValidationReason.PLACEHOLDER_EXTRA
    # Missing: an expected placeholder absent from the output.
    if expected_set - found_set:
        return ValidationReason.PLACEHOLDER_MISSING
    # Duplicate: an expected placeholder appearing more than once.
    if len(found) != len(found_set):
        return ValidationReason.PLACEHOLDER_DUP
    # Reorder: same set, wrong order.
    if found != expected:
        return ValidationReason.PLACEHOLDER_REORDER
    return ValidationReason.OK


def validate_cleanup(
    output: str, protected_input: str, registry: TokenRegistry | None
) -> ValidationReason:
    """Validate cleanup *output* against *protected_input* (§15 reject list).

    Returns :attr:`ValidationReason.OK` to accept, or a specific reason to
    reject. The caller maps any non-OK result to a fail-open fallback and logs
    only the reason code, never the content.
    """
    # Emptiness / whitespace-only (only meaningful for non-empty input).
    if protected_input.strip():
        if not output:
            return ValidationReason.EMPTY_OUTPUT
        if not output.strip():
            return ValidationReason.WHITESPACE

    # Placeholder integrity.
    placeholder_reason = validate_placeholders(output, registry)
    if placeholder_reason is not ValidationReason.OK:
        return placeholder_reason

    # Length: reject output substantially longer than input.
    max_len = max(len(protected_input) * _LENGTH_MULTIPLIER, len(protected_input) + _LENGTH_MARGIN)
    if len(output) > max_len:
        return ValidationReason.TOO_LONG

    # Assistant preface / apparent answer at the start.
    if _has_preface(output):
        return ValidationReason.PREFACE
    if _looks_like_answer(output):
        return ValidationReason.APPARENT_ANSWER

    return ValidationReason.OK


def _has_preface(output: str) -> bool:
    """True when *output* opens with a known assistant preface (§15)."""
    # Strip leading non-word punctuation/quotes the model might prepend.
    head = output.lstrip(" \t\n\"'`*>-").lower()
    return any(head.startswith(preface) for preface in _PREFACES)


def _looks_like_answer(output: str) -> bool:
    """True when *output* looks like an answer/action rather than cleaned prose.

    Best-effort (§15 "Output appears to answer the prompt", §26 "not perfectly
    safe"): flags outputs that open with an instructional/solution phrase or a
    code fence. A cleaned transcript preserves the speaker's own wording, so
    these openers are strong signals the model performed the request instead.
    """
    head = output.lstrip(" \t\n\"'*>-").lower()
    return any(head.startswith(opener) for opener in _ANSWER_OPENERS)


def edit_ratio(input_text: str, output_text: str) -> float:
    """A cheap content-free edit magnitude for observability (§15).

    Character-length delta relative to input length; 0.0 for identical-length
    output, growing with divergence. Not a similarity metric — just an aggregate
    signal recorded alongside the other :class:`~local_flow.cleanup.base.CleanupMetrics`.
    """
    if not input_text:
        return 0.0
    return abs(len(output_text) - len(input_text)) / len(input_text)


def placeholder_count(text: str) -> int:
    """Number of technical-token placeholders present in *text*."""
    return len(re.findall(PH_PATTERN, text))
