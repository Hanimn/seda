"""Build a bounded transcription initial prompt (see IMPLEMENTATION_PLAN.md §13).

The initial prompt biases the model toward the user's vocabulary (project
terms, coding words). It is built **only** from the configured custom
vocabulary — never from clipboard or terminal contents — and is length-bounded
so it can't grow without limit.
"""

from __future__ import annotations

# Keep the prompt short: an over-long initial prompt wastes context and can
# skew decoding. This caps the assembled string, not the vocabulary list.
_MAX_PROMPT_CHARS = 1024


def build_initial_prompt(custom_vocabulary: list[str], *, explicit: str = "") -> str:
    """Return an initial prompt, or empty string when there's nothing to add.

    An ``explicit`` initial prompt from config takes precedence. Otherwise the
    custom vocabulary terms are joined into a single hint sentence, truncated
    to :data:`_MAX_PROMPT_CHARS`.
    """
    if explicit:
        return explicit[:_MAX_PROMPT_CHARS]

    terms = [t.strip() for t in custom_vocabulary if t and t.strip()]
    if not terms:
        return ""

    prompt = "Vocabulary: " + ", ".join(terms) + "."
    if len(prompt) <= _MAX_PROMPT_CHARS:
        return prompt

    # Drop trailing terms until it fits, so we never emit a partial term.
    kept: list[str] = []
    running = len("Vocabulary: .")
    for term in terms:
        addition = len(term) + 2  # ", " separator
        if running + addition > _MAX_PROMPT_CHARS:
            break
        kept.append(term)
        running += addition
    if not kept:
        return ""
    return "Vocabulary: " + ", ".join(kept) + "."
