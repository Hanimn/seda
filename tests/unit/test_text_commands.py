"""Unit tests for the spoken-command engine (IMPLEMENTATION_PLAN.md §14)."""

from __future__ import annotations

import pytest

from local_flow.text.commands import (
    CommandResult,
    apply_commands,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _std(text: str) -> str:
    """Apply commands in standard (contextual) mode."""
    return apply_commands(text, mode="standard").text


def _lit(text: str) -> str:
    """Apply commands in literal mode (all single-word commands active)."""
    return apply_commands(text, mode="literal").text


# ---------------------------------------------------------------------------
# Unambiguous multi-word commands (always replaced in all modes)
# ---------------------------------------------------------------------------

class TestUnambiguousMultiWordCommands:
    @pytest.mark.parametrize(
        "speech, expected",
        [
            # "new line" is a 2-word phrase → whole phrase replaced by \n
            ("new line here", "\nhere"),
            ("newline here", "\nhere"),
            ("new paragraph now", "\n\nnow"),
            # Opening delimiters attach to what follows
            ("open parenthesis x", "(x"),
            ("open paren x", "(x"),
            ("open bracket x", "[x"),
            ("open brace x", "{x"),
            # Closing delimiters attach to what precedes — "x )" not "x)"
            # because "x" is just a word, not technical
            ("close parenthesis x", ") x"),
            ("close paren x", ") x"),
            ("close bracket x", "] x"),
            ("close brace x", "} x"),
            # Multi-word unambiguous
            ("question mark really", "? really"),
            ("exclamation mark wow", "! wow"),
            ("double quote text double quote", '"text"'),
            ("single quote text single quote", "'text'"),
            ("at sign user", "@user"),
            ("triple backtick python", "```python"),
            ("back tick x", "`x"),
        ],
    )
    def test_multi_word_replaced_standard(self, speech: str, expected: str) -> None:
        assert _std(speech) == expected


# ---------------------------------------------------------------------------
# Ambiguous single-word commands — contextual mode
# ---------------------------------------------------------------------------

class TestAmbiguousSingleWordContextual:
    """In standard/contextual mode, ambiguous words replaced only in technical sequences."""

    def test_slash_in_path_replaced(self) -> None:
        # "src" is ≤3 chars → triggers technical context; cascades to "auth slash middleware".
        assert _std("src slash auth slash middleware dot t s") == "src/auth/middleware.ts"

    def test_dot_in_filename_replaced(self) -> None:
        # Short neighbour "ts" triggers technical context.
        assert _std("middleware dot ts") == "middleware.ts"

    def test_slash_in_natural_prose_unchanged(self) -> None:
        # "or" context — should not replace
        result = _std("Use a slash or backslash here")
        # "slash" must not become "/" when surrounded by natural prose words
        assert "slash" in result

    def test_period_in_natural_prose_unchanged(self) -> None:
        result = _std("Use a period of thirty seconds")
        assert "period" in result
        assert result.count(".") == 0 or "period" in result  # no blind replacement

    def test_dot_in_natural_prose_unchanged(self) -> None:
        result = _std("dot the i's and cross the t's")
        assert "dot" in result

    def test_colon_alone_unchanged_standard(self) -> None:
        result = _std("We should use a colon here")
        assert "colon" in result

    def test_comma_alone_unchanged_standard(self) -> None:
        result = _std("Use a comma in the list")
        assert "comma" in result


# ---------------------------------------------------------------------------
# Symbol prefix forces replacement of ambiguous words
# ---------------------------------------------------------------------------

class TestSymbolPrefix:
    def test_symbol_prefix_forces_dot(self) -> None:
        assert _std("symbol dot") == "."

    def test_symbol_prefix_forces_slash(self) -> None:
        assert _std("symbol slash") == "/"

    def test_symbol_prefix_forces_colon(self) -> None:
        assert _std("symbol colon") == ":"

    def test_symbol_prefix_forces_comma(self) -> None:
        assert _std("symbol comma") == ","

    def test_symbol_prefix_forces_open_brace(self) -> None:
        assert _std("symbol open brace user symbol colon true symbol close brace") == "{user:true}"

    def test_symbol_prefix_case_insensitive(self) -> None:
        assert _std("Symbol Dot") == "."

    def test_symbol_prefix_forces_dash(self) -> None:
        assert _std("symbol dash") == "-"

    def test_symbol_prefix_forces_underscore(self) -> None:
        assert _std("symbol underscore") == "_"

    def test_symbol_prefix_forces_equals(self) -> None:
        assert _std("symbol equals") == "="

    def test_symbol_prefix_forces_hash(self) -> None:
        assert _std("symbol hash") == "#"

    def test_symbol_prefix_forces_pipe(self) -> None:
        assert _std("symbol pipe") == "|"

    def test_symbol_prefix_forces_ampersand(self) -> None:
        assert _std("symbol ampersand") == "&"

    def test_symbol_prefix_forces_backtick(self) -> None:
        assert _std("symbol backtick") == "`"

    def test_symbol_prefix_forces_tab(self) -> None:
        assert _std("symbol tab") == "\t"


# ---------------------------------------------------------------------------
# Literal mode — only unambiguous + symbol-prefixed commands active;
# bare ambiguous single words are preserved (§3: "apply only explicitly
# enabled spoken-symbol commands").
# ---------------------------------------------------------------------------

class TestLiteralMode:
    def test_bare_slash_not_replaced(self) -> None:
        assert _lit("Use a slash here") == "Use a slash here"

    def test_bare_period_not_replaced(self) -> None:
        assert _lit("Use a period here") == "Use a period here"

    def test_bare_dot_not_replaced(self) -> None:
        assert _lit("dot the files") == "dot the files"

    def test_bare_colon_not_replaced(self) -> None:
        assert _lit("Use a colon here") == "Use a colon here"

    def test_bare_comma_not_replaced(self) -> None:
        assert _lit("Use a comma here") == "Use a comma here"

    def test_bare_semicolon_not_replaced(self) -> None:
        assert _lit("then semicolon next") == "then semicolon next"

    def test_bare_dash_not_replaced(self) -> None:
        assert _lit("use dash here") == "use dash here"

    def test_bare_hyphen_not_replaced(self) -> None:
        assert _lit("use hyphen here") == "use hyphen here"

    def test_bare_underscore_not_replaced(self) -> None:
        assert _lit("my underscore var") == "my underscore var"

    def test_bare_equals_not_replaced(self) -> None:
        assert _lit("x equals five") == "x equals five"

    def test_bare_plus_not_replaced(self) -> None:
        assert _lit("x plus y") == "x plus y"

    def test_bare_asterisk_not_replaced(self) -> None:
        assert _lit("x asterisk y") == "x asterisk y"

    def test_bare_backslash_not_replaced(self) -> None:
        assert _lit("use backslash here") == "use backslash here"

    # Unambiguous multi-word commands still fire in literal mode.
    def test_unambiguous_new_line_still_replaced(self) -> None:
        assert _lit("first new line second") == "first\nsecond"

    def test_unambiguous_open_paren_still_replaced(self) -> None:
        assert _lit("open parenthesis x") == "(x"

    # Symbol-prefixed commands still fire in literal mode.
    def test_symbol_prefix_still_replaces_dot(self) -> None:
        assert _lit("symbol dot") == "."

    def test_symbol_prefix_still_replaces_slash(self) -> None:
        assert _lit("symbol slash") == "/"

    def test_path_in_technical_context_still_replaced(self) -> None:
        # "ts" is ≤3 chars → technical seed; cascades in literal mode too.
        assert _lit("middleware dot ts") == "middleware.ts"


# ---------------------------------------------------------------------------
# Longest-phrase-wins
# ---------------------------------------------------------------------------

class TestLongestPhraseWins:
    def test_open_parenthesis_wins_over_open(self) -> None:
        # "open parenthesis" must match as one unit, not "open" alone.
        result = _std("open parenthesis x")
        assert result == "(x"

    def test_close_parenthesis_wins_over_close(self) -> None:
        result = _std("close parenthesis x")
        assert result == ") x"

    def test_new_paragraph_wins_over_new_line(self) -> None:
        result = _std("new paragraph now")
        assert result == "\n\nnow"


# ---------------------------------------------------------------------------
# Case insensitivity
# ---------------------------------------------------------------------------

class TestCaseInsensitivity:
    def test_upper_case_command(self) -> None:
        assert _std("Open Parenthesis x") == "(x"

    def test_mixed_case(self) -> None:
        assert _std("NEW LINE here") == "\nhere"

    def test_lower_case_symbol_prefix(self) -> None:
        assert _std("symbol dot") == "."


# ---------------------------------------------------------------------------
# Token boundaries
# ---------------------------------------------------------------------------

class TestTokenBoundaries:
    def test_partial_word_not_matched(self) -> None:
        # "slashable" should not trigger the "slash" command.
        result = _std("slashable path")
        assert "/" not in result
        assert "slashable" in result

    def test_newline_command_not_inside_word(self) -> None:
        # "renew line" must not trigger "new line" inside the word "renew"
        result = _std("renew line contract")
        assert result == "renew line contract"


# ---------------------------------------------------------------------------
# Full path example from spec
# ---------------------------------------------------------------------------

class TestSpecExamples:
    def test_auth_middleware_path(self) -> None:
        # Full path prefix gives "src" (≤3 chars) → technical; cascades through.
        result = _std("src slash auth slash middleware dot t s")
        assert result == "src/auth/middleware.ts"

    def test_auth_middleware_with_symbol_prefix(self) -> None:
        # Using symbol prefix also works without a technical seed word.
        result = _std("auth symbol slash middleware symbol dot ts")
        assert result == "auth/middleware.ts"

    def test_periodize_unchanged(self) -> None:
        result = _std("We should periodize this data")
        assert result == "We should periodize this data"

    def test_period_of_time_unchanged(self) -> None:
        result = _std("Use a period of thirty seconds")
        assert "period" in result

    def test_symbol_brace_expr(self) -> None:
        result = _std("symbol open brace user symbol colon true symbol close brace")
        assert result == "{user:true}"


# ---------------------------------------------------------------------------
# CommandResult metadata
# ---------------------------------------------------------------------------

class TestCommandResult:
    def test_returns_command_result(self) -> None:
        r = apply_commands("new line here", mode="standard")
        assert isinstance(r, CommandResult)
        assert r.text == "\nhere"
        assert r.commands_applied >= 1

    def test_no_commands_applied(self) -> None:
        r = apply_commands("hello world", mode="standard")
        assert r.commands_applied == 0
        assert r.text == "hello world"
