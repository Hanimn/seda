"""Conservative filler-word removal (IMPLEMENTATION_PLAN.md §14).

Rules:
- Disabled in ``literal`` mode.
- Disabled by default in ``standard`` mode (pass ``force=True`` to override).
- Enabled in ``polished`` mode.
- Never removes "like" (carries meaning too often).
- Only removes standalone discourse fillers, not words inside identifiers or
  protected placeholder tokens.
- Cleans up double-spaces left after removal.
"""

from __future__ import annotations

import re
from typing import Literal

# Ordered: multi-word phrases before single words so they are matched first.
_FILLER_PHRASES: list[str] = [
    "you know",
    "I mean",
    "kind of",
    "sort of",
    "basically",
    "actually",
    "um",
    "uh",
    "erm",
]

# Build a single regex that matches any filler phrase at a whole-word boundary.
# We deliberately exclude "like" per spec.
_FILLER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(p) for p in _FILLER_PHRASES) + r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

# Placeholder tokens — never touch content inside these.
_PLACEHOLDER_RE = re.compile(r"__LF_[A-Z0-9]+_\d{4}__")


def remove_fillers(
    text: str,
    mode: Literal["literal", "standard", "polished"] = "standard",
    *,
    force: bool = False,
) -> str:
    """Remove standalone discourse fillers from *text*.

    Parameters
    ----------
    text:
        Input transcript (after command substitution and token protection).
    mode:
        Pipeline mode.  Removal is active in ``polished`` mode, or when
        ``force=True`` in any mode except ``literal``.
    force:
        Enable removal in ``standard`` mode without changing the global mode.
    """
    if mode == "literal":
        return text
    if mode != "polished" and not force:
        return text

    result = _remove_standalone_fillers(text)
    # Collapse multiple spaces (but preserve newlines).
    result = re.sub(r"[ \t]{2,}", " ", result)
    return result.strip()


# ---------------------------------------------------------------------------
# Core removal logic
# ---------------------------------------------------------------------------


def _remove_standalone_fillers(text: str) -> str:
    """Remove filler phrases that are not inside placeholders or identifiers."""
    # Split into placeholder spans and non-placeholder spans.
    # Only process non-placeholder spans.
    parts: list[str] = []
    cursor = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        # Process text before this placeholder.
        parts.append(_filter_fillers(text[cursor : m.start()]))
        # Keep the placeholder verbatim.
        parts.append(m.group())
        cursor = m.end()
    parts.append(_filter_fillers(text[cursor:]))
    return "".join(parts)


def _filter_fillers(segment: str) -> str:
    """Remove filler phrases from a plain-text segment (no placeholders)."""
    return _FILLER_PATTERN.sub("", segment)
