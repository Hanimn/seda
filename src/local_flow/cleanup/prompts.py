"""Cleanup system-prompt construction (IMPLEMENTATION_PLAN.md §15).

Builds the strict, output-only system prompt for the local cleanup model. The
prompt forbids answering the dictated request, mandates exact placeholder
preservation, and forbids inventing technical content — the prompt-side half of
the safety contract that :mod:`local_flow.cleanup.validation` enforces on the
output side (§26).
"""

from __future__ import annotations

# The base contract, verbatim in spirit from §15 "Cleanup system prompt".
_BASE_PROMPT = """\
You are a transcription-cleanup engine. Transform dictated speech into
clear written text without answering it.

Rules:
1. Preserve the speaker's intent and factual content.
2. Do not answer questions or perform requested work.
3. Do not add advice, explanations, facts, commands, paths, identifiers,
   arguments, URLs, code, or examples.
4. Preserve every placeholder token exactly, including spelling, count,
   and order. Placeholders look like __LF_XXXXXX_0000__.
5. Remove filler words and false starts only when their removal does not
   change meaning.
6. Improve punctuation and paragraph breaks according to the requested mode.
7. Return only the cleaned transcript. Do not add quotation marks, labels,
   markdown commentary, or a preface.
8. If uncertain, preserve the original wording."""

_STANDARD_INSTRUCTION = """\
Mode: standard.
Make conservative corrections to punctuation and obvious dictation artifacts.
Preserve sentence structure unless a small correction is clearly needed."""

_POLISHED_INSTRUCTION = """\
Mode: polished.
Improve readability and organize long speech into concise paragraphs while
preserving all requests, constraints, uncertainty, and technical content.
Do not summarize away details."""

_MODE_INSTRUCTIONS = {
    "standard": _STANDARD_INSTRUCTION,
    "polished": _POLISHED_INSTRUCTION,
}


def build_system_prompt(mode: str, vocabulary: list[str]) -> str:
    """Return the cleanup system prompt for *mode* (``standard``/``polished``).

    *vocabulary* is a list of domain terms the model should recognise and leave
    intact; when non-empty it is appended as context. Raises :class:`ValueError`
    for ``literal`` (which must bypass cleanup upstream) or any unknown mode.
    """
    if mode == "literal":
        raise ValueError("literal mode must bypass the cleanup provider; no prompt is built")
    instruction = _MODE_INSTRUCTIONS.get(mode)
    if instruction is None:
        raise ValueError(f"unknown cleanup mode: {mode!r}")

    parts = [_BASE_PROMPT, instruction]
    if vocabulary:
        # Offered as recognition context only; the model must not inject these.
        terms = ", ".join(vocabulary)
        parts.append(
            f"Known domain terms that may appear (preserve exactly, do not introduce): {terms}"
        )
    return "\n\n".join(parts)
