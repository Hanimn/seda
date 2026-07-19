"""Unit tests for technical-token protection (IMPLEMENTATION_PLAN.md §14)."""

from __future__ import annotations

import re

import pytest

from seda.text.technical_tokens import (
    ProtectionError,
    protect,
    restore,
)

# ---------------------------------------------------------------------------
# Basic protect / restore round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_plain_text_unchanged(self) -> None:
        text = "look at this code"
        protected, registry = protect(text)
        assert restore(protected, registry) == text

    def test_empty_string(self) -> None:
        protected, registry = protect("")
        assert restore(protected, registry) == ""

    def test_no_technical_tokens(self) -> None:
        text = "explain why this fails"
        protected, registry = protect(text)
        assert protected == text
        assert registry.count == 0

    def test_restore_returns_exact_original(self) -> None:
        text = "check src/auth/middleware.ts for bugs"
        protected, registry = protect(text)
        assert restore(protected, registry) == text


# ---------------------------------------------------------------------------
# Placeholder format
# ---------------------------------------------------------------------------


class TestPlaceholderFormat:
    def test_placeholder_is_opaque(self) -> None:
        text = "src/auth/middleware.ts"
        protected, registry = protect(text)
        # Placeholder must not contain the original content.
        assert "auth" not in protected
        assert "middleware" not in protected
        assert ".ts" not in protected

    def test_placeholder_matches_expected_pattern(self) -> None:
        text = "src/auth/middleware.ts"
        protected, registry = protect(text)
        # Should contain at least one placeholder token.
        assert re.search(r"__LF_[A-Z0-9]+_\d{4}__", protected)

    def test_per_request_prefix_is_random(self) -> None:
        text = "src/auth/middleware.ts"
        _, r1 = protect(text)
        _, r2 = protect(text)
        # Two separate calls should use different prefixes.
        assert r1.prefix != r2.prefix


# ---------------------------------------------------------------------------
# What gets protected — canonical cases from the spec
# ---------------------------------------------------------------------------


class TestProtectedPatterns:
    @pytest.mark.parametrize(
        "token",
        [
            # Paths
            "src/auth/middleware.ts",
            "../config/settings.toml",
            "/etc/hosts",
            # Filenames with extensions
            "middleware.ts",
            "config.toml",
            "README.md",
            "package.json",
            # URLs
            "https://localhost:3000/api",
            "http://127.0.0.1:11434",
            # Email
            "user@example.com",
            # CLI flags
            "--no-verify",
            "--dry-run",
            "-v",
            # Env vars
            "DATABASE_URL",
            "NODE_ENV",
            # snake_case
            "refresh_token",
            "my_variable",
            # camelCase
            "refreshToken",
            "myVariable",
            # PascalCase
            "AuthMiddleware",
            "UserService",
            # Semantic versions
            "v2.1.4",
            "v1.0.0-beta.1",
            # Git hash (short)
            "a1b2c3d",
            # IP:port
            "127.0.0.1:11434",
            # kebab-case
            "my-component",
            "some-long-kebab-name",
        ],
    )
    def test_token_is_protected(self, token: str) -> None:
        text = f"look at {token} now"
        protected, registry = protect(text)
        assert token not in protected, f"Token {token!r} was not replaced"
        assert registry.count >= 1
        assert restore(protected, registry) == text


# ---------------------------------------------------------------------------
# TokenRegistry integrity checks
# ---------------------------------------------------------------------------


class TestRegistryIntegrity:
    def test_missing_placeholder_detected(self) -> None:
        text = "src/auth/middleware.ts"
        protected, registry = protect(text)
        # Remove all placeholders from the protected text.
        corrupted = re.sub(r"__LF_[A-Z0-9]+_\d{4}__", "", protected)
        with pytest.raises(ProtectionError, match="missing"):
            restore(corrupted, registry)

    def test_duplicated_placeholder_detected(self) -> None:
        text = "src/auth/middleware.ts"
        protected, registry = protect(text)
        placeholder = re.search(r"__LF_[A-Z0-9]+_\d{4}__", protected)
        assert placeholder is not None
        ph = placeholder.group()
        # Duplicate the placeholder.
        corrupted = protected + " " + ph
        with pytest.raises(ProtectionError, match="duplicate"):
            restore(corrupted, registry)

    def test_reordered_placeholders_detected(self) -> None:
        text = "check src/a.ts and src/b.ts"
        protected, registry = protect(text)
        placeholders = re.findall(r"__LF_[A-Z0-9]+_\d{4}__", protected)
        if len(placeholders) < 2:
            pytest.skip("need at least 2 placeholders for reorder test")
        # Swap them.
        p0, p1 = placeholders[0], placeholders[1]
        swapped = protected.replace(p0, "TEMP").replace(p1, p0).replace("TEMP", p1)
        # Reordering is only detectable via the sequence check.
        with pytest.raises(ProtectionError, match="order|sequence|reorder"):
            restore(swapped, registry)

    def test_count_matches_placeholders_in_text(self) -> None:
        text = "check src/a.ts and src/b.ts please"
        protected, registry = protect(text)
        found = re.findall(r"__LF_[A-Z0-9]+_\d{4}__", protected)
        assert registry.count == len(found)


# ---------------------------------------------------------------------------
# Multiple tokens in one string
# ---------------------------------------------------------------------------


class TestMultipleTokens:
    def test_two_paths_protected(self) -> None:
        text = "compare src/a.ts with src/b.ts"
        protected, registry = protect(text)
        assert "src/a.ts" not in protected
        assert "src/b.ts" not in protected
        assert restore(protected, registry) == text

    def test_path_and_env_var(self) -> None:
        text = "set DATABASE_URL to postgres://localhost/db"
        protected, registry = protect(text)
        assert restore(protected, registry) == text

    def test_many_tokens(self) -> None:
        text = "run npm run test in src/app and check NODE_ENV with refreshToken at v2.1.4"
        protected, registry = protect(text)
        assert restore(protected, registry) == text
