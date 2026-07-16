"""Technical-token protection for transcript pipeline (IMPLEMENTATION_PLAN.md §14).

Before LLM cleanup, replace technical tokens (paths, identifiers, URLs, etc.)
with opaque placeholders.  After cleanup, restore them exactly.

The registry tracks the ordered sequence of placeholders so missing, duplicated,
or reordered placeholders can be detected on restore.
"""

from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass, field


class ProtectionError(ValueError):
    """Raised when placeholder integrity is violated on restore."""


# ---------------------------------------------------------------------------
# Placeholder format:  __LF_<PREFIX>_<NNNN>__
# PREFIX is a random 6-char uppercase alphanumeric string per protect() call.
# NNNN is the zero-padded sequential index.
# ---------------------------------------------------------------------------

_PH_PATTERN = re.compile(r"__LF_([A-Z0-9]+)_(\d{4})__")


@dataclass
class TokenRegistry:
    """Maps placeholders to their original values and records insertion order."""

    prefix: str
    _mapping: dict[str, str] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self._order)

    def add(self, original: str) -> str:
        """Store *original* and return its placeholder."""
        idx = len(self._order)
        placeholder = f"__LF_{self.prefix}_{idx:04d}__"
        self._mapping[placeholder] = original
        self._order.append(placeholder)
        return placeholder

    def get(self, placeholder: str) -> str:
        """Return the original value for *placeholder*."""
        return self._mapping[placeholder]

    def placeholders_in_order(self) -> list[str]:
        return list(self._order)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def protect(text: str) -> tuple[str, TokenRegistry]:
    """Replace technical tokens in *text* with opaque placeholders.

    Returns ``(protected_text, registry)``.  Call :func:`restore` with the
    same registry to undo the substitutions.

    All patterns are scanned first; their matches are sorted by start position
    (longest match wins for overlapping spans) so placeholders are assigned in
    left-to-right text order.
    """
    registry = TokenRegistry(prefix=_random_prefix())
    if not text:
        return text, registry

    # Collect all non-overlapping matches across all patterns, sorted by position.
    # When two matches overlap, keep the one that starts earlier; if tied, keep longer.
    matches: list[tuple[int, int, str]] = []  # (start, end, matched_text)
    for pattern, _name in _PATTERNS:
        for m in pattern.finditer(text):
            matches.append((m.start(), m.end(), m.group()))

    # Sort: earlier start first, longer match first on tie.
    matches.sort(key=lambda t: (t[0], -(t[1] - t[0])))

    # Remove overlapping matches (keep the first/longer one).
    non_overlapping: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, tok in matches:
        if start >= last_end:
            non_overlapping.append((start, end, tok))
            last_end = end

    if not non_overlapping:
        return text, registry

    # Build result by substituting each match with a placeholder.
    parts: list[str] = []
    cursor = 0
    for start, end, tok in non_overlapping:
        parts.append(text[cursor:start])
        parts.append(registry.add(tok))
        cursor = end
    parts.append(text[cursor:])

    return "".join(parts), registry


def restore(text: str, registry: TokenRegistry) -> str:
    """Restore placeholders in *text* back to their original values.

    Raises :class:`ProtectionError` if any placeholder is missing, duplicated,
    or appears out of order.
    """
    if registry.count == 0:
        return text

    found = _PH_PATTERN.findall(text)
    # found is a list of (prefix, index) tuples
    found_placeholders = [f"__LF_{p}_{i}__" for p, i in found]

    expected = registry.placeholders_in_order()

    # Check for missing placeholders.
    expected_set = set(expected)
    found_set = set(found_placeholders)
    missing = expected_set - found_set
    if missing:
        raise ProtectionError(
            f"Placeholder(s) missing from text after cleanup: {missing}"
        )

    # Check for duplicates.
    if len(found_placeholders) != len(found_set):
        seen2: set[str] = set()
        dupes = {ph for ph in found_placeholders if ph in seen2 or seen2.add(ph)}  # type: ignore[func-returns-value]
        raise ProtectionError(
            f"duplicate placeholder(s) found after cleanup: {dupes}"
        )

    # Check order (only the expected placeholders, ignoring extras from other sources).
    found_in_expected = [ph for ph in found_placeholders if ph in expected_set]
    if found_in_expected != expected:
        raise ProtectionError(
            "Placeholder sequence order was changed after cleanup "
            f"(expected {expected}, found {found_in_expected})"
        )

    result = text
    for placeholder in expected:
        result = result.replace(placeholder, registry.get(placeholder))
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _random_prefix(length: int = 6) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


# ---------------------------------------------------------------------------
# Recognition patterns (applied in order; more specific patterns first)
# ---------------------------------------------------------------------------

# Fenced code block  ```...```  (multi-line)
_PAT_FENCED_CODE = re.compile(r"```[\s\S]*?```", re.MULTILINE)

# Inline code  `...`
_PAT_INLINE_CODE = re.compile(r"`[^`\n]+`")

# URLs  http(s)://...
_PAT_URL = re.compile(
    r"https?://[^\s\"'<>\]\[)]+",
    re.IGNORECASE,
)

# Email addresses
_PAT_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# File paths: relative (src/auth/middleware.ts, ../config/x.toml) or absolute (/etc/hosts)
_PAT_PATH = re.compile(
    r"(?:\.{1,2}/|/)[A-Za-z0-9_./\-]+(?:\.[A-Za-z0-9]+)+"
    r"|"
    r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_./\-]+(?:\.[A-Za-z0-9]+)*"
)

# Filenames with extensions (e.g. middleware.ts, config.toml, README.md)
# Must have at least one dot with a known/plausible extension.
_PAT_FILENAME = re.compile(
    r"\b[A-Za-z0-9_\-]+\.[A-Za-z][A-Za-z0-9]{0,9}\b"
)

# Semantic versions  v2.1.4  v1.0.0-beta.1
_PAT_SEMVER = re.compile(
    r"\bv\d+\.\d+\.\d+(?:[.\-][A-Za-z0-9.]+)?\b"
)

# IP address with optional port  127.0.0.1:11434
_PAT_IP_PORT = re.compile(
    r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"
)

# Git hashes: 7-40 hex chars that look like a hash (not a common word)
_PAT_GIT_HASH = re.compile(r"\b[0-9a-f]{7,40}\b")

# CLI flags:  --no-verify  -v  --dry-run
_PAT_CLI_FLAG = re.compile(r"--?[A-Za-z][A-Za-z0-9\-]*")

# SCREAMING_SNAKE_CASE environment variables (≥2 uppercase letters + underscore)
_PAT_ENV_VAR = re.compile(r"\b[A-Z][A-Z0-9_]*_[A-Z0-9_]+\b")

# camelCase  (starts lower, has at least one uppercase)
_PAT_CAMEL = re.compile(r"\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+\b")

# PascalCase  (starts upper, has at least two uppercase or mixed)
_PAT_PASCAL = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b")

# snake_case  (contains underscore between word chars)
_PAT_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

# kebab-case  (contains hyphen between word chars, looks like an identifier)
_PAT_KEBAB = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)+\b")

# Ordered list: most specific / longest patterns first to avoid partial matches.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_PAT_FENCED_CODE,  "fenced_code"),
    (_PAT_INLINE_CODE,  "inline_code"),
    (_PAT_URL,          "url"),
    (_PAT_EMAIL,        "email"),
    (_PAT_IP_PORT,      "ip_port"),
    (_PAT_SEMVER,       "semver"),
    (_PAT_PATH,         "path"),
    (_PAT_FILENAME,     "filename"),
    (_PAT_GIT_HASH,     "git_hash"),
    (_PAT_CLI_FLAG,     "cli_flag"),
    (_PAT_ENV_VAR,      "env_var"),
    (_PAT_CAMEL,        "camel"),
    (_PAT_PASCAL,       "pascal"),
    (_PAT_SNAKE,        "snake"),
    (_PAT_KEBAB,        "kebab"),
]
