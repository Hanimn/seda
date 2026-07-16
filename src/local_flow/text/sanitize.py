"""Control-character sanitization for raw transcripts (IMPLEMENTATION_PLAN.md §14)."""

from __future__ import annotations

import re

# C0 characters that are stripped (not null — that is rejected; not tab/LF/CR — those are handled).
_STRIP_C0 = re.compile(r"[\x01-\x08\x0b\x0c\x0e-\x1f]")

# C1 range U+0080–U+009F
_STRIP_C1 = re.compile(r"[\x80-\x9f]")


class InvalidTranscriptError(ValueError):
    """Raised when a transcript contains characters that must be rejected outright."""


def sanitize(text: str) -> str:
    """Sanitize *text* in-place and return the cleaned string.

    Steps applied in order:
    1. Reject null bytes (U+0000) — they indicate corrupt input.
    2. Normalize line endings: CRLF and bare CR → LF.
    3. Strip unexpected C0 control characters (all C0 except HT, LF, CR).
    4. Strip C1 control characters (U+0080–U+009F).

    Escape sequences are NOT interpreted — a literal backslash-n stays as-is;
    ANSI escape sequences lose their ESC byte but the remaining printable bytes
    are preserved.
    """
    if "\x00" in text:
        raise InvalidTranscriptError("Transcript contains a null byte (U+0000); input rejected.")

    # Normalize line endings before stripping CR so \r\n becomes \n not \n\n.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    text = _STRIP_C0.sub("", text)
    text = _STRIP_C1.sub("", text)

    return text
