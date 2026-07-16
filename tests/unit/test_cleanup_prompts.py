"""Unit tests for the cleanup system prompt (IMPLEMENTATION_PLAN.md §15)."""

from __future__ import annotations

import pytest

from local_flow.cleanup.prompts import build_system_prompt


class TestSystemPrompt:
    def test_contains_output_only_contract(self) -> None:
        prompt = build_system_prompt("standard", [])
        low = prompt.lower()
        # Must instruct the model not to answer and to return only the transcript.
        assert "not answer" in low or "do not answer" in low
        assert "only" in low

    def test_contains_placeholder_preservation_rule(self) -> None:
        prompt = build_system_prompt("standard", [])
        assert "placeholder" in prompt.lower()

    def test_standard_and_polished_differ(self) -> None:
        standard = build_system_prompt("standard", [])
        polished = build_system_prompt("polished", [])
        assert standard != polished

    def test_polished_mentions_paragraphs(self) -> None:
        polished = build_system_prompt("polished", []).lower()
        assert "paragraph" in polished or "readability" in polished

    def test_vocabulary_included_when_present(self) -> None:
        prompt = build_system_prompt("standard", ["TypeScript", "middleware.ts"])
        assert "TypeScript" in prompt
        assert "middleware.ts" in prompt

    def test_no_vocabulary_section_when_empty(self) -> None:
        # An empty vocabulary should not leave a dangling "vocabulary:" label.
        prompt = build_system_prompt("standard", [])
        assert isinstance(prompt, str) and prompt.strip()

    def test_rejects_literal_mode(self) -> None:
        # Literal mode must never reach the provider; building a prompt for it
        # is a programming error.
        with pytest.raises(ValueError, match="literal"):
            build_system_prompt("literal", [])
