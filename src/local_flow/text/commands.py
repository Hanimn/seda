"""Spoken-command engine for deterministic transcript processing (IMPLEMENTATION_PLAN.md §14).

Design:
- Multi-word commands are always replaced (unambiguous).
- Ambiguous single-word commands replaced only when:
    * ``symbol`` prefix is used explicitly, OR
    * literal mode is active, OR
    * the preceding output ends with a technical segment (contextual), OR
    * look-ahead shows it's between non-prose words (path-separator heuristic).
- Longest-phrase-wins: phrases sorted by word count descending.
- Case-insensitive, whole-word matching (whitespace-delimited tokens).
- Spacing: tracked per segment during assembly, not as a post-pass regex.
  Each segment carries a flag indicating whether it should be joined without
  space to the next segment (inline technical symbol behaviour).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Command table  (phrase, replacement, ambiguous)
# ---------------------------------------------------------------------------
_COMMAND_TABLE: list[tuple[str, str, bool]] = [
    # Unambiguous multi-word — always replaced
    ("new paragraph",     "\n\n", False),
    ("new line",          "\n",   False),
    ("newline",           "\n",   False),
    ("open parenthesis",  "(",    False),
    ("close parenthesis", ")",    False),
    ("open paren",        "(",    False),
    ("close paren",       ")",    False),
    ("open bracket",      "[",    False),
    ("close bracket",     "]",    False),
    ("open brace",        "{",    False),
    ("close brace",       "}",    False),
    ("question mark",     "?",    False),
    ("exclamation mark",  "!",    False),
    ("double quote",      '"',    False),
    ("single quote",      "'",    False),
    ("at sign",           "@",    False),
    ("triple backtick",   "```",  False),
    ("back tick",         "`",    False),
    # Ambiguous single-word — context-dependent in standard/polished mode
    ("backtick",   "`",  True),
    ("colon",      ":",  True),
    ("semicolon",  ";",  True),
    ("comma",      ",",  True),
    ("period",     ".",  True),
    ("dot",        ".",  True),
    ("slash",      "/",  True),
    ("backslash",  "\\", True),
    ("underscore", "_",  True),
    ("dash",       "-",  True),
    ("hyphen",     "-",  True),
    ("equals",     "=",  True),
    ("plus",       "+",  True),
    ("asterisk",   "*",  True),
    ("hash",       "#",  True),
    ("pipe",       "|",  True),
    ("ampersand",  "&",  True),
    ("tab",        "\t", True),
]

_COMMANDS: dict[str, tuple[str, bool]] = {
    p: (r, a) for p, r, a in _COMMAND_TABLE
}
# Longest phrase first (by word count, then length for tie-breaking).
_SORTED_PHRASES = sorted(
    _COMMANDS,
    key=lambda p: (len(p.split()), len(p)),
    reverse=True,
)
_SYMBOL_PREFIX = "symbol"

# Commands whose replacement should absorb surrounding spaces (path/code symbols).
# When ``sticky=True`` the symbol attaches directly to adjacent tokens.
_STICKY_REPLS: frozenset[str] = frozenset({
    "/", "\\", ".", "_", "-", ":", "=", "+", "*", "@", "#", "|", "&",
    "(", ")", "[", "]", "{", "}", '"', "'", "`", "```", ";", ",", "?", "!", "\t",
})

# Prose function words that indicate a surrounding word is NOT a path component.
_PROSE_WORDS: frozenset[str] = frozenset(
    "a an the of in on at to for or and but with use using we they it is are "
    "was were be been being have has had do does did will would could should "
    "may might shall must can my our your his her its their this that these "
    "those here there so also just not no yes i you he she".split()
)


@dataclass(frozen=True)
class CommandResult:
    text: str
    commands_applied: int


# ---------------------------------------------------------------------------
# Internal segment type
# ---------------------------------------------------------------------------

@dataclass
class _Seg:
    """One output segment with spacing metadata."""
    text: str
    # True → don't insert a space before the *next* segment.
    no_space_after: bool = False
    # True → don't insert a space before *this* segment.
    no_space_before: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply_commands(
    text: str,
    mode: Literal["standard", "literal", "polished"] = "standard",
) -> CommandResult:
    """Replace spoken commands in *text*."""
    words = text.split()
    segs: list[_Seg] = []
    commands_applied = 0
    i = 0
    prev_technical = False  # last emitted segment was technical/symbolic

    while i < len(words):
        word = words[i]

        # --- Symbol prefix: force the next command phrase ---
        if word.lower() == _SYMBOL_PREFIX:
            matched, n = _match(words, i + 1)
            if matched:
                repl, _ = _COMMANDS[matched]
                seg = _make_symbol_seg(repl, is_in_technical_run=True)
                segs.append(seg)
                commands_applied += 1
                i += 1 + n
                prev_technical = True
                continue
            segs.append(_Seg(word))
            prev_technical = False
            i += 1
            continue

        # --- Try longest command match ---
        matched, n = _match(words, i)
        if matched:
            repl, is_ambiguous = _COMMANDS[matched]
            in_tech = prev_technical or _is_path_separator_context(words, i, n, matched)
            # Literal mode: only replace unambiguous commands and explicitly
            # prefixed (symbol …) ones — bare ambiguous words are preserved
            # ("Apply only explicitly enabled spoken-symbol commands", §3).
            replace = not is_ambiguous or in_tech
            if replace:
                seg = _make_symbol_seg(repl, is_in_technical_run=in_tech)
                segs.append(seg)
                commands_applied += 1
                i += n
                prev_technical = True
                continue
            # Ambiguous, not replacing — emit original words
            for w in words[i : i + n]:
                segs.append(_Seg(w))
            prev_technical = _looks_technical(words[i])
            i += n
            continue

        segs.append(_Seg(word))
        # A word that immediately follows a technical symbol is itself part of
        # the technical run — keep prev_technical True so the next separator
        # (e.g. the second "slash" in "src/auth slash middleware") also fires.
        prev_technical = _looks_technical(word) or (prev_technical and bool(re.match(r"^[A-Za-z0-9_]+$", word)))
        i += 1

    return CommandResult(
        text=_assemble(segs),
        commands_applied=commands_applied,
    )


# ---------------------------------------------------------------------------
# Segment construction
# ---------------------------------------------------------------------------

# Opening delimiters attach to what follows; closing attach to what precedes.
_OPENERS: frozenset[str] = frozenset({"(", "[", "{", "`", "```", "@"})
_CLOSERS: frozenset[str] = frozenset({")", "]", "}"})
# Quote marks attach on both sides (open and close in the same run).
_QUOTES: frozenset[str] = frozenset({'"', "'"})
# Path separators / identifier connectors always attach on both sides in a
# technical run; in prose they get spaces.
_CONNECTORS: frozenset[str] = frozenset({"/", "\\", ".", "_", "-", ":", "=", "+",
                                          "*", "#", "|", "&", ";", ",", "?", "!",
                                          '"', "'"})


def _make_symbol_seg(repl: str, *, is_in_technical_run: bool) -> _Seg:
    """Create a segment for a command replacement with appropriate spacing flags."""
    if repl in ("\n", "\n\n"):
        return _Seg(repl, no_space_after=True, no_space_before=True)
    if repl == "\t":
        return _Seg(repl, no_space_after=False, no_space_before=True)
    if repl in _QUOTES:
        # Quote marks glue to adjacent content on both sides.
        return _Seg(repl, no_space_after=True, no_space_before=True)
    if repl in _OPENERS:
        return _Seg(repl, no_space_after=True, no_space_before=False)
    if repl in _CLOSERS:
        return _Seg(repl, no_space_after=False, no_space_before=True)
    if is_in_technical_run:
        return _Seg(repl, no_space_after=True, no_space_before=True)
    return _Seg(repl, no_space_after=False, no_space_before=False)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _assemble(segs: list[_Seg]) -> str:
    if not segs:
        return ""

    parts: list[str] = [segs[0].text]
    for idx in range(1, len(segs)):
        prev = segs[idx - 1]
        curr = segs[idx]
        if prev.no_space_after or curr.no_space_before:
            parts.append(curr.text)
        else:
            parts.append(" ")
            parts.append(curr.text)

    out = "".join(parts)
    # Collapse multiple ordinary spaces (preserve newlines/tabs).
    out = re.sub(r"[ ]{2,}", " ", out)

    # Post-pass: after a dot used as a file-extension separator, collapse
    # spaces between single-letter tokens (spoken-out extensions like "t s").
    # Apply up to 4 times to handle e.g. "h t m l".
    for _ in range(4):
        new = re.sub(r"(?<=\.)([A-Za-z]{1,4}) ([A-Za-z])(?=[ \n\t]|$)", r"\1\2", out)
        if new == out:
            break
        out = new

    return out.strip(" ")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _looks_technical(tok: str) -> bool:
    """True when *tok* looks like a path component or identifier fragment."""
    if not tok:
        return False
    if len(tok) == 1 and not tok.isalpha() and tok not in " \n\t":
        return True
    return bool(re.search(r"[0-9_./\-\\@]", tok))


def _is_path_separator_context(
    words: list[str], cmd_idx: int, cmd_len: int, phrase: str
) -> bool:
    """True when *phrase* is a path/connector command in a technical sequence.

    Requires at least one neighbour to already look technical (contains a
    digit, underscore, slash, dot, etc.) OR to be a very short token (≤3 chars)
    typically used as a file-extension component.  Pure multi-letter alphabetic
    words on both sides are treated as prose unless they abut another command.
    """
    _CONNECTOR_PHRASES = {
        "slash", "dot", "period", "backslash", "underscore", "dash", "hyphen",
    }
    if phrase not in _CONNECTOR_PHRASES:
        return False

    prev_word = words[cmd_idx - 1] if cmd_idx > 0 else None
    next_idx = cmd_idx + cmd_len
    next_word = words[next_idx] if next_idx < len(words) else None

    if prev_word is None or next_word is None:
        return False

    # Prose function words on either side → prose context.
    if prev_word.lower() in _PROSE_WORDS or next_word.lower() in _PROSE_WORDS:
        return False

    # At least one neighbour must be "clearly technical": contains a digit,
    # underscore, slash, dot, dash, or @ — OR is a short token (≤3 chars) that
    # is NOT a common English word (i.e. likely a file extension or path component
    # like "ts", "py", "js" rather than "my", "or", "at").
    def _clearly_technical(w: str) -> bool:
        if bool(re.search(r"[0-9_./\-\\@]", w)):
            return True
        if len(w) <= 3 and w.lower() not in _PROSE_WORDS:
            return True
        return False

    return _clearly_technical(prev_word) or _clearly_technical(next_word)


def _match(words: list[str], start: int) -> tuple[str | None, int]:
    """Return (phrase, word_count) for the longest matching command at *start*."""
    if start >= len(words):
        return None, 0
    available = len(words) - start
    for phrase in _SORTED_PHRASES:
        pw = phrase.split()
        n = len(pw)
        if n > available:
            continue
        if [w.lower() for w in words[start : start + n]] == pw:
            return phrase, n
    return None, 0
