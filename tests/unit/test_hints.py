"""Unit tests for the transcription initial-prompt builder (see §13)."""

from __future__ import annotations

from local_flow.transcription.hints import build_initial_prompt


def test_empty_vocabulary_yields_empty_prompt() -> None:
    assert build_initial_prompt([]) == ""


def test_vocabulary_terms_are_included() -> None:
    prompt = build_initial_prompt(["Claude Code", "TypeScript"])
    assert "Claude Code" in prompt
    assert "TypeScript" in prompt


def test_explicit_prompt_takes_precedence() -> None:
    prompt = build_initial_prompt(["ignored"], explicit="use this verbatim")
    assert prompt == "use this verbatim"


def test_prompt_is_length_bounded() -> None:
    # A vocabulary far exceeding the cap must not produce an unbounded prompt.
    huge = [f"term{i}" for i in range(10_000)]
    prompt = build_initial_prompt(huge)
    assert len(prompt) <= 1024
    # And it must never end mid-term (we drop whole terms to fit).
    assert prompt.endswith(".")


def test_blank_terms_are_skipped() -> None:
    assert build_initial_prompt(["", "  ", "real"]) == "Vocabulary: real."
