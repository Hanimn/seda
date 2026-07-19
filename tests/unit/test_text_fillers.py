"""Unit tests for filler-word processing (IMPLEMENTATION_PLAN.md §14)."""

from __future__ import annotations

import pytest

from seda.text.fillers import remove_fillers

# ---------------------------------------------------------------------------
# Mode-based enablement
# ---------------------------------------------------------------------------


class TestModeEnablement:
    def test_disabled_in_literal_mode(self) -> None:
        text = "um I think we should do this"
        assert remove_fillers(text, mode="literal") == text

    def test_disabled_by_default_in_standard_mode(self) -> None:
        text = "um I think we should do this"
        assert remove_fillers(text, mode="standard") == text

    def test_enabled_in_polished_mode(self) -> None:
        text = "um I think we should do this"
        result = remove_fillers(text, mode="polished")
        assert "um" not in result

    def test_enabled_when_forced_in_standard(self) -> None:
        text = "um I think so"
        result = remove_fillers(text, mode="standard", force=True)
        assert "um" not in result


# ---------------------------------------------------------------------------
# Filler candidates removed in polished mode
# ---------------------------------------------------------------------------


class TestFillerCandidates:
    @pytest.mark.parametrize(
        "filler",
        ["um", "uh", "erm"],
    )
    def test_single_word_filler_removed(self, filler: str) -> None:
        text = f"{filler} I think we should refactor this"
        result = remove_fillers(text, mode="polished")
        assert filler not in result.lower()
        assert "refactor" in result

    @pytest.mark.parametrize(
        "filler_phrase",
        ["you know", "I mean", "kind of", "sort of", "basically", "actually"],
    )
    def test_phrase_filler_removed(self, filler_phrase: str) -> None:
        text = f"we should {filler_phrase} fix this bug"
        result = remove_fillers(text, mode="polished")
        assert filler_phrase not in result
        assert "fix this bug" in result

    def test_filler_at_end_removed(self) -> None:
        result = remove_fillers("this is fine you know", mode="polished")
        assert "you know" not in result
        assert "this is fine" in result

    def test_filler_at_start_removed(self) -> None:
        result = remove_fillers("basically we need to do this", mode="polished")
        assert result.lower().startswith("we need") or "basically" not in result


# ---------------------------------------------------------------------------
# Like is never removed
# ---------------------------------------------------------------------------


class TestLikePreserved:
    def test_like_is_never_removed(self) -> None:
        text = "it is like a cache but faster"
        assert remove_fillers(text, mode="polished") == text

    def test_like_preserved_even_with_force(self) -> None:
        text = "it was like nothing I had seen"
        assert remove_fillers(text, mode="polished", force=True) == text


# ---------------------------------------------------------------------------
# Fillers inside technical tokens / protected spans are not removed
# ---------------------------------------------------------------------------


class TestFillerInsideTechnical:
    def test_filler_word_in_identifier_untouched(self) -> None:
        # "actually" is part of a variable name prefix — won't match since
        # "actually" inside an identifier is not a standalone word.
        text = "call getActuallyUsedTokens to check usage"
        result = remove_fillers(text, mode="polished")
        # The word inside the identifier must survive.
        assert "getActuallyUsedTokens" in result

    def test_um_inside_placeholder_untouched(self) -> None:
        # A placeholder token that happens to contain "um" should not be touched.
        text = "look at __LF_TOKEN_0001__ and fix it"
        result = remove_fillers(text, mode="polished")
        assert "__LF_TOKEN_0001__" in result


# ---------------------------------------------------------------------------
# Non-filler uses are preserved
# ---------------------------------------------------------------------------


class TestNonFillerPreserved:
    def test_actually_as_adverb_removed(self) -> None:
        # "actually" as a discourse filler is removed
        result = remove_fillers("actually I think this works", mode="polished")
        assert "actually" not in result

    def test_basically_in_prose_removed(self) -> None:
        result = remove_fillers("this is basically correct", mode="polished")
        assert "basically" not in result

    def test_plain_text_unchanged(self) -> None:
        text = "we need to refactor the authentication module"
        assert remove_fillers(text, mode="polished") == text

    def test_multiple_fillers_all_removed(self) -> None:
        text = "um basically I think uh we should you know fix this"
        result = remove_fillers(text, mode="polished")
        for filler in ("um", "basically", "uh", "you know"):
            assert filler not in result
        assert "fix this" in result

    def test_whitespace_cleaned_up(self) -> None:
        # Removing fillers should not leave double spaces.
        text = "um we should fix this"
        result = remove_fillers(text, mode="polished")
        assert "  " not in result
        assert result == result.strip()
