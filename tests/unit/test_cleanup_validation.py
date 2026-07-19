"""Unit tests for cleanup output validation (IMPLEMENTATION_PLAN.md §15, §25).

Exercises the full §25 fake-provider matrix: valid edits, empty output,
assistant prefaces, apparent answers, missing/duplicated/extra/reordered
placeholders, over-expansion, malformed/whitespace-only output, and unicode.
Every invalid response must be rejected so the controller falls back to the
deterministic transcript.
"""

from __future__ import annotations

import pytest

from seda.cleanup.validation import (
    ValidationReason,
    validate_cleanup,
    validate_placeholders,
)
from seda.text.technical_tokens import protect

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _protect(text: str):  # type: ignore[no-untyped-def]
    """Protect *text* and return (protected_text, registry)."""
    return protect(text)


# ---------------------------------------------------------------------------
# Accepts valid output
# ---------------------------------------------------------------------------


class TestAccepts:
    def test_unchanged_protected_text_is_ok(self) -> None:
        protected, registry = _protect("check src/auth/middleware.ts now")
        assert validate_cleanup(protected, protected, registry) is ValidationReason.OK

    def test_light_edit_is_ok(self) -> None:
        protected, registry = _protect("um so check src/app.ts please")
        # A plausible cleanup: dropped "um so", kept the placeholder.
        cleaned = protected.replace("um so ", "")
        assert validate_cleanup(cleaned, protected, registry) is ValidationReason.OK

    def test_no_placeholders_plain_prose_ok(self) -> None:
        protected, registry = _protect("please fix the bug")
        assert validate_cleanup("please fix the bug", protected, registry) is ValidationReason.OK

    def test_unicode_output_ok(self) -> None:
        protected, registry = _protect("cafe here")
        assert validate_cleanup("café here", protected, registry) is ValidationReason.OK


# ---------------------------------------------------------------------------
# Emptiness / malformed
# ---------------------------------------------------------------------------


class TestEmptyAndMalformed:
    def test_empty_output_for_nonempty_input_rejected(self) -> None:
        protected, registry = _protect("fix the bug")
        assert validate_cleanup("", protected, registry) is ValidationReason.EMPTY_OUTPUT

    def test_whitespace_only_rejected(self) -> None:
        protected, registry = _protect("fix the bug")
        assert validate_cleanup("   \n\t ", protected, registry) is ValidationReason.WHITESPACE


# ---------------------------------------------------------------------------
# Placeholder integrity (incl. extra-new, which restore() does NOT catch)
# ---------------------------------------------------------------------------


class TestPlaceholderIntegrity:
    def test_missing_placeholder_rejected(self) -> None:
        protected, registry = _protect("check src/auth/middleware.ts now")
        import re

        stripped = re.sub(r"__LF_[A-Z0-9]+_\d{4}__", "", protected)
        assert (
            validate_cleanup(stripped, protected, registry) is ValidationReason.PLACEHOLDER_MISSING
        )

    def test_duplicated_placeholder_rejected(self) -> None:
        protected, registry = _protect("check src/auth/middleware.ts now")
        import re

        ph = re.search(r"__LF_[A-Z0-9]+_\d{4}__", protected)
        assert ph is not None
        dup = protected + " " + ph.group()
        assert validate_cleanup(dup, protected, registry) is ValidationReason.PLACEHOLDER_DUP

    def test_extra_new_placeholder_rejected(self) -> None:
        # An invented placeholder with the same prefix but an unseen index.
        protected, registry = _protect("check src/app.ts now")
        fabricated = f"__LF_{registry.prefix}_9999__"
        tampered = protected + " " + fabricated
        assert validate_cleanup(tampered, protected, registry) is ValidationReason.PLACEHOLDER_EXTRA

    def test_reordered_placeholders_rejected(self) -> None:
        protected, registry = _protect("check src/a.ts and src/b.ts")
        import re

        phs = re.findall(r"__LF_[A-Z0-9]+_\d{4}__", protected)
        if len(phs) < 2:
            pytest.skip("need at least 2 placeholders")
        p0, p1 = phs[0], phs[1]
        swapped = protected.replace(p0, "TEMP").replace(p1, p0).replace("TEMP", p1)
        assert (
            validate_cleanup(swapped, protected, registry) is ValidationReason.PLACEHOLDER_REORDER
        )

    def test_validate_placeholders_ok_when_intact(self) -> None:
        protected, registry = _protect("check src/app.ts now")
        assert validate_placeholders(protected, registry) is ValidationReason.OK


# ---------------------------------------------------------------------------
# Length heuristic
# ---------------------------------------------------------------------------


class TestLength:
    def test_over_expansion_rejected(self) -> None:
        protected, registry = _protect("short input")
        # max(len*1.75, len+200); len is small so the +200 bound dominates.
        too_long = "x" * (len(protected) + 500)
        assert validate_cleanup(too_long, protected, registry) is ValidationReason.TOO_LONG

    def test_modest_expansion_ok(self) -> None:
        protected, registry = _protect("fix the bug")
        # Within +200 chars.
        ok = protected + " with more detail here"
        assert validate_cleanup(ok, protected, registry) is ValidationReason.OK


# ---------------------------------------------------------------------------
# Assistant preface / apparent answer
# ---------------------------------------------------------------------------


class TestPreface:
    @pytest.mark.parametrize(
        "prefaced",
        [
            "Sure, here is the cleaned text",
            "Here is the cleaned version",
            "Here's what you said",
            "Certainly! fix the bug",
            "I can help with that",
            "sure, fix the bug",  # case-insensitive
        ],
    )
    def test_preface_rejected(self, prefaced: str) -> None:
        protected, registry = _protect("fix the bug")
        assert validate_cleanup(prefaced, protected, registry) is ValidationReason.PREFACE

    def test_non_preface_not_flagged(self) -> None:
        protected, registry = _protect("here the build fails")
        # "here the" is not an assistant preface.
        assert validate_cleanup("here the build fails", protected, registry) is ValidationReason.OK


# ---------------------------------------------------------------------------
# Apparent answer (distinct from a preface, §15/§25)
# ---------------------------------------------------------------------------


class TestApparentAnswer:
    @pytest.mark.parametrize(
        "answer",
        [
            "To fix this, edit the config file",
            "You should restart the server",
            "You need to run the migration",
            "The solution is to add an index",
            "I recommend using a connection pool",
            "First, open the settings",
            "```python\nprint('hi')\n```",
        ],
    )
    def test_apparent_answer_rejected(self, answer: str) -> None:
        protected, registry = _protect("how do I fix the slow query")
        assert validate_cleanup(answer, protected, registry) is ValidationReason.APPARENT_ANSWER

    def test_cleaned_question_not_flagged_as_answer(self) -> None:
        # A cleaned dictation that is itself a question must pass.
        protected, registry = _protect("how do I fix the slow query")
        assert (
            validate_cleanup("How do I fix the slow query?", protected, registry)
            is ValidationReason.OK
        )


# ---------------------------------------------------------------------------
# Reason names are sanitized (never contain content)
# ---------------------------------------------------------------------------


class TestReasonSanitized:
    def test_reason_values_are_short_codes(self) -> None:
        for reason in ValidationReason:
            # A sanitized reason is an uppercase snake-case code with no spaces.
            assert reason.value == reason.value.upper()
            assert " " not in reason.value
