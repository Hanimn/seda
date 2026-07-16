"""Unit tests for the full deterministic text pipeline (IMPLEMENTATION_PLAN.md §14)."""

from __future__ import annotations

import pytest

from local_flow.text.pipeline import (
    PipelineResult,
    finalize_after_cleanup,
    process_transcript,
)


# ---------------------------------------------------------------------------
# Control-character sanitization is applied first
# ---------------------------------------------------------------------------

class TestSanitizationApplied:
    def test_crlf_normalized(self) -> None:
        result = process_transcript("line one\r\nline two", mode="standard")
        assert "\r" not in result.text

    def test_null_byte_raises(self) -> None:
        from local_flow.text.sanitize import InvalidTranscriptError
        with pytest.raises(InvalidTranscriptError):
            process_transcript("bad\x00input", mode="standard")

    def test_c0_stripped(self) -> None:
        result = process_transcript("before\x07after", mode="standard")
        assert "\x07" not in result.text
        assert "beforeafter" in result.text


# ---------------------------------------------------------------------------
# Beginning-of-transcript mode commands
# ---------------------------------------------------------------------------

class TestModeCommands:
    def test_literal_mode_detected_and_stripped(self) -> None:
        result = process_transcript("literal mode fix the bug", mode="standard")
        assert result.mode_override == "literal"
        assert "literal mode" not in result.text
        assert "fix the bug" in result.text

    def test_standard_mode_detected_and_stripped(self) -> None:
        result = process_transcript("standard mode explain this", mode="literal")
        assert result.mode_override == "standard"
        assert "standard mode" not in result.text

    def test_polished_mode_detected_and_stripped(self) -> None:
        result = process_transcript("polished mode write a summary", mode="standard")
        assert result.mode_override == "polished"
        assert "polished mode" not in result.text

    def test_cancel_returns_none(self) -> None:
        result = process_transcript("scratch that never mind", mode="standard")
        assert result.cancelled is True

    def test_cancel_dictation_returns_none(self) -> None:
        result = process_transcript("cancel dictation whatever", mode="standard")
        assert result.cancelled is True

    def test_mode_command_only_at_start(self) -> None:
        # "literal mode" in the middle of a sentence must not trigger override.
        result = process_transcript(
            "please use literal mode for this", mode="standard"
        )
        assert result.mode_override is None
        assert "literal mode" in result.text

    def test_no_mode_command(self) -> None:
        result = process_transcript("fix the bug", mode="standard")
        assert result.mode_override is None
        assert result.cancelled is False


# ---------------------------------------------------------------------------
# Spoken commands applied
# ---------------------------------------------------------------------------

class TestSpokenCommandsApplied:
    def test_path_command(self) -> None:
        result = process_transcript(
            "look at src slash auth slash middleware dot ts", mode="standard"
        )
        assert "src/auth/middleware.ts" in result.text

    def test_new_line_command(self) -> None:
        result = process_transcript("first line new line second line", mode="standard")
        assert "\n" in result.text

    def test_symbol_prefix_forces_replacement(self) -> None:
        result = process_transcript("symbol open brace key symbol colon val symbol close brace", mode="standard")
        assert "{key:val}" in result.text

    def test_commands_disabled_when_flag_off(self) -> None:
        result = process_transcript(
            "auth slash middleware", mode="standard", spoken_commands_enabled=False
        )
        assert "slash" in result.text
        assert "/" not in result.text


# ---------------------------------------------------------------------------
# Technical-token protection
# ---------------------------------------------------------------------------

class TestTokenProtection:
    def test_protected_tokens_survive_pipeline(self) -> None:
        result = process_transcript(
            "check src/auth/middleware.ts for bugs", mode="standard"
        )
        assert "src/auth/middleware.ts" in result.text

    def test_placeholders_not_in_final_output(self) -> None:
        import re
        result = process_transcript(
            "check DATABASE_URL and refreshToken", mode="standard"
        )
        assert not re.search(r"__LF_[A-Z0-9]+_\d{4}__", result.text)


# ---------------------------------------------------------------------------
# Filler removal
# ---------------------------------------------------------------------------

class TestFillerRemoval:
    def test_fillers_removed_in_polished_mode(self) -> None:
        result = process_transcript(
            "um basically we should fix this", mode="polished"
        )
        assert "um" not in result.text
        assert "basically" not in result.text
        assert "fix this" in result.text

    def test_fillers_kept_in_standard_mode(self) -> None:
        result = process_transcript(
            "um basically we should fix this", mode="standard"
        )
        assert "um" in result.text

    def test_fillers_kept_in_literal_mode(self) -> None:
        result = process_transcript(
            "um basically fix this", mode="literal"
        )
        assert "um" in result.text


# ---------------------------------------------------------------------------
# Final normalization
# ---------------------------------------------------------------------------

class TestFinalNormalization:
    def test_no_leading_trailing_spaces(self) -> None:
        result = process_transcript("  hello world  ", mode="standard")
        assert result.text == result.text.strip()

    def test_double_spaces_collapsed(self) -> None:
        result = process_transcript("hello  world", mode="standard")
        assert "  " not in result.text

    def test_empty_input_handled(self) -> None:
        result = process_transcript("", mode="standard")
        assert result.text == ""

    def test_whitespace_only_input(self) -> None:
        result = process_transcript("   ", mode="standard")
        assert result.text.strip() == ""


# ---------------------------------------------------------------------------
# Literal mode bypasses spoken-command ambiguous replacements and fillers
# ---------------------------------------------------------------------------

class TestLiteralModeBehavior:
    def test_bare_ambiguous_commands_not_replaced_in_literal(self) -> None:
        # Literal mode preserves bare ambiguous words ("apply only explicitly
        # enabled spoken-symbol commands", §3).
        result = process_transcript("use slash here", mode="literal")
        assert "slash" in result.text
        assert "/" not in result.text

    def test_symbol_prefix_still_works_in_literal(self) -> None:
        result = process_transcript("symbol slash", mode="literal")
        assert "/" in result.text

    def test_fillers_not_removed_in_literal(self) -> None:
        result = process_transcript("um fix this", mode="literal")
        assert "um" in result.text


# ---------------------------------------------------------------------------
# PipelineResult metadata
# ---------------------------------------------------------------------------

class TestPipelineResult:
    def test_returns_pipeline_result(self) -> None:
        result = process_transcript("hello world", mode="standard")
        assert isinstance(result, PipelineResult)

    def test_effective_mode_reflects_override(self) -> None:
        result = process_transcript("polished mode fix the bug", mode="standard")
        assert result.effective_mode == "polished"

    def test_effective_mode_is_base_mode_when_no_override(self) -> None:
        result = process_transcript("fix the bug", mode="standard")
        assert result.effective_mode == "standard"


# ---------------------------------------------------------------------------
# The finalize_after_cleanup seam (Phase 6 handoff)
# ---------------------------------------------------------------------------


class TestFinalizeAfterCleanup:
    """process_transcript exposes protected (pre-restore) text; a cleanup
    provider operates on it, then finalize_after_cleanup restores + normalizes."""

    def test_protected_text_still_contains_placeholders(self) -> None:
        import re

        result = process_transcript(
            "check src/auth/middleware.ts for bugs", mode="standard"
        )
        # The protected text handed to a cleanup provider must still carry the
        # opaque placeholders (the technical token is protected, not visible).
        assert re.search(r"__LF_[A-Z0-9]+_\d{4}__", result.protected_text)
        assert "src/auth/middleware.ts" not in result.protected_text

    def test_finalize_reproduces_pipeline_text(self) -> None:
        # Finalizing the untouched protected text must reproduce the same final
        # text process_transcript already returned (backward-compat contract).
        result = process_transcript(
            "check src/auth/middleware.ts for bugs", mode="standard"
        )
        assert (
            finalize_after_cleanup(result.protected_text, result.token_registry)
            == result.text
        )

    def test_finalize_restores_cleaned_text(self) -> None:
        result = process_transcript("look at README.md now", mode="standard")
        # A "cleaned" version that keeps the placeholder intact restores fine.
        cleaned = result.protected_text  # provider returned it unchanged
        final = finalize_after_cleanup(cleaned, result.token_registry)
        assert "README.md" in final

    def test_finalize_rejects_reordered_placeholders(self) -> None:
        from local_flow.text.technical_tokens import ProtectionError

        result = process_transcript("check src/a.ts and src/b.ts", mode="standard")
        import re

        phs = re.findall(r"__LF_[A-Z0-9]+_\d{4}__", result.protected_text)
        if len(phs) < 2:
            pytest.skip("need at least 2 placeholders for reorder test")
        p0, p1 = phs[0], phs[1]
        swapped = (
            result.protected_text.replace(p0, "TEMP")
            .replace(p1, p0)
            .replace("TEMP", p1)
        )
        with pytest.raises(ProtectionError):
            finalize_after_cleanup(swapped, result.token_registry)

    def test_finalize_with_no_registry_just_normalizes(self) -> None:
        # No registry (nothing protected) → normalize the text as-is.
        assert finalize_after_cleanup("hello   world  ", None) == "hello world"

    def test_protected_text_empty_for_cancelled(self) -> None:
        result = process_transcript("scratch that never mind", mode="standard")
        assert result.cancelled is True
        assert result.protected_text == ""
