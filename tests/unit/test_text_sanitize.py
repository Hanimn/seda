"""Unit tests for control-character sanitization (IMPLEMENTATION_PLAN.md §14)."""

from __future__ import annotations

import pytest

from seda.text.sanitize import InvalidTranscriptError, sanitize


class TestNullByteRejection:
    def test_null_byte_raises(self) -> None:
        with pytest.raises(InvalidTranscriptError, match="null byte"):
            sanitize("hello\x00world")

    def test_null_byte_at_start(self) -> None:
        with pytest.raises(InvalidTranscriptError, match="null byte"):
            sanitize("\x00")

    def test_no_null_byte_passes(self) -> None:
        assert sanitize("hello world") == "hello world"


class TestCRLFNormalization:
    def test_crlf_becomes_lf(self) -> None:
        assert sanitize("line one\r\nline two") == "line one\nline two"

    def test_cr_alone_becomes_lf(self) -> None:
        assert sanitize("line one\rline two") == "line one\nline two"

    def test_lf_unchanged(self) -> None:
        assert sanitize("line one\nline two") == "line one\nline two"

    def test_mixed_endings_all_normalized(self) -> None:
        assert sanitize("a\r\nb\rc\nd") == "a\nb\nc\nd"


class TestC0ControlCharacters:
    # C0 range: U+0000–U+001F; \t (0x09), \n (0x0A), \r (0x0D) are allowed.
    @pytest.mark.parametrize(
        "char",
        [
            "\x01",  # SOH
            "\x02",  # STX
            "\x03",  # ETX
            "\x04",  # EOT
            "\x05",  # ENQ
            "\x06",  # ACK
            "\x07",  # BEL
            "\x08",  # BS
            "\x0b",  # VT
            "\x0c",  # FF
            "\x0e",  # SO
            "\x0f",  # SI
            "\x10",  # DLE
            "\x11",  # DC1
            "\x12",  # DC2
            "\x13",  # DC3
            "\x14",  # DC4
            "\x15",  # NAK
            "\x16",  # SYN
            "\x17",  # ETB
            "\x18",  # CAN
            "\x19",  # EM
            "\x1a",  # SUB
            "\x1b",  # ESC
            "\x1c",  # FS
            "\x1d",  # GS
            "\x1e",  # RS
            "\x1f",  # US
        ],
    )
    def test_unexpected_c0_stripped(self, char: str) -> None:
        result = sanitize(f"before{char}after")
        assert result == "beforeafter"
        assert char not in result

    def test_tab_preserved(self) -> None:
        assert sanitize("col1\tcol2") == "col1\tcol2"

    def test_newline_preserved(self) -> None:
        assert sanitize("line1\nline2") == "line1\nline2"


class TestC1ControlCharacters:
    # C1 range: U+0080–U+009F
    @pytest.mark.parametrize(
        "char",
        ["\x80", "\x85", "\x8f", "\x9b", "\x9f"],
    )
    def test_c1_chars_stripped(self, char: str) -> None:
        result = sanitize(f"before{char}after")
        assert result == "beforeafter"
        assert char not in result


class TestEscapeSequences:
    def test_ansi_escape_not_interpreted(self) -> None:
        # ESC char itself is stripped; the bracket and letters remain.
        result = sanitize("\x1b[31mred\x1b[0m")
        assert "\x1b" not in result
        # The non-control characters survive.
        assert "red" in result

    def test_string_backslash_n_not_converted(self) -> None:
        # A literal backslash-n in the transcript must not become a real newline.
        result = sanitize(r"line1\nline2")
        assert result == r"line1\nline2"


class TestNormalTextUnchanged:
    def test_plain_ascii(self) -> None:
        text = "Look at auth/middleware.ts and explain the issue."
        assert sanitize(text) == text

    def test_unicode_text(self) -> None:
        text = "Héllo wörld — café résumé"
        assert sanitize(text) == text

    def test_newlines_and_tabs_preserved(self) -> None:
        text = "line1\n\tindented\nline3"
        assert sanitize(text) == text

    def test_empty_string(self) -> None:
        assert sanitize("") == ""
