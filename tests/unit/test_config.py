"""Unit tests for configuration loading, validation, and serialization.

Covers the IMPLEMENTATION_PLAN.md §25 configuration checklist: defaults,
overrides, invalid modes/sample rates/durations/hotkeys, invalid and
non-loopback cleanup URLs, ``auto_submit = true`` rejection, effective-config
generation, and privacy-safe serialization.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from local_flow.config import (
    Config,
    ConfigError,
    default_config_path,
    load_config,
    load_config_from_dict,
    render_toml,
)


def test_default_config_is_valid_and_conservative() -> None:
    config = Config()
    # Privacy/safety defaults must be off (see §3, §21).
    assert config.paste.auto_submit is False
    assert config.cleanup.enabled is False
    assert config.app.log_transcripts is False
    assert config.app.retain_debug_audio is False
    assert config.cleanup.allow_remote_endpoint is False


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "does-not-exist.toml")
    assert config == Config()


def test_user_overrides_are_applied() -> None:
    config = load_config_from_dict({"app": {"mode": "literal", "log_level": "DEBUG"}})
    assert config.app.mode == "literal"
    assert config.app.log_level == "DEBUG"
    # Unspecified fields keep their defaults.
    assert config.audio.sample_rate == 16000


def test_invalid_mode_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config_from_dict({"app": {"mode": "turbo"}})
    assert "app.mode" in str(exc.value)


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ConfigError):
        load_config_from_dict({"app": {"log_level": "LOUD"}})


@pytest.mark.parametrize("rate", [12345, 0, 99999])
def test_invalid_sample_rate_is_rejected(rate: int) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config_from_dict({"audio": {"sample_rate": rate}})
    assert "sample_rate" in str(exc.value)


def test_valid_sample_rate_is_accepted() -> None:
    config = load_config_from_dict({"audio": {"sample_rate": 48000}})
    assert config.audio.sample_rate == 48000


def test_minimum_duration_must_be_below_maximum() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config_from_dict(
            {
                "audio": {
                    "minimum_duration_ms": 200_000,
                    "maximum_duration_seconds": 10,
                }
            }
        )
    assert "minimum_duration_ms" in str(exc.value)


def test_negative_duration_is_rejected() -> None:
    with pytest.raises(ConfigError):
        load_config_from_dict({"audio": {"maximum_duration_seconds": -1}})


@pytest.mark.parametrize(
    "hotkey",
    [
        "",
        "<ctrl>+",
        "<ctrl>+notakey",
        "<>+a",
        "ctrl+alt+space",  # bare 'ctrl' is neither single-char nor bracketed
    ],
)
def test_invalid_hotkey_is_rejected(hotkey: str) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config_from_dict({"hotkeys": {"push_to_talk": hotkey}})
    assert "hotkeys.push_to_talk" in str(exc.value)


@pytest.mark.parametrize(
    "hotkey",
    [
        "<ctrl>+<alt>+space",
        "<esc>",
        "<ctrl>+<alt>+m",
        "<cmd>+<shift>+d",
        "tab",
    ],
)
def test_valid_hotkey_is_accepted(hotkey: str) -> None:
    config = load_config_from_dict({"hotkeys": {"push_to_talk": hotkey}})
    assert config.hotkeys.push_to_talk == hotkey


def test_auto_submit_true_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config_from_dict({"paste": {"auto_submit": True}})
    message = str(exc.value)
    assert "auto_submit" in message
    assert "false" in message


def test_non_loopback_cleanup_url_is_rejected_by_default() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config_from_dict({"cleanup": {"ollama": {"base_url": "http://example.com:11434"}}})
    assert "loopback" in str(exc.value)


def test_non_loopback_cleanup_url_allowed_with_explicit_opt_in() -> None:
    config = load_config_from_dict(
        {
            "cleanup": {
                "allow_remote_endpoint": True,
                "ollama": {"base_url": "http://example.com:11434"},
            }
        }
    )
    assert config.cleanup.ollama.base_url == "http://example.com:11434"


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434"],
)
def test_loopback_cleanup_urls_are_accepted(url: str) -> None:
    config = load_config_from_dict({"cleanup": {"ollama": {"base_url": url}}})
    assert config.cleanup.ollama.base_url == url


def test_non_http_cleanup_url_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config_from_dict({"cleanup": {"ollama": {"base_url": "ftp://127.0.0.1"}}})
    assert "http" in str(exc.value)


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config_from_dict({"app": {"notafield": 1}})
    assert "notafield" in str(exc.value)


def test_effective_config_hides_custom_vocabulary() -> None:
    config = load_config_from_dict({"text": {"custom_vocabulary": ["secret-repo", "Private.ts"]}})
    effective = config.effective()
    vocab = effective["text"]["custom_vocabulary"]
    assert "secret-repo" not in str(vocab)
    assert "2 term(s)" in vocab


def test_effective_config_hides_debug_audio_directory() -> None:
    config = load_config_from_dict({"app": {"debug_audio_directory": "/home/alice/private/audio"}})
    effective = config.effective()
    assert "alice" not in str(effective["app"]["debug_audio_directory"])


def test_debug_audio_directory_not_validated_when_retention_disabled() -> None:
    # A non-writable/nonexistent path is fine as long as retention is off.
    config = load_config_from_dict(
        {"app": {"retain_debug_audio": False, "debug_audio_directory": "/no/such/dir"}}
    )
    assert config.app.debug_audio_directory == "/no/such/dir"


def test_non_directory_debug_audio_path_is_rejected_when_enabled(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config_from_dict(
            {
                "app": {
                    "retain_debug_audio": True,
                    "debug_audio_directory": str(not_a_dir),
                }
            }
        )
    assert "not a directory" in str(exc.value)


def test_non_writable_debug_audio_dir_is_rejected_when_enabled(tmp_path: Path) -> None:
    import os
    import stat

    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x, no write
    try:
        # Skip if the environment ignores mode bits (e.g. running as root).
        if os.access(readonly, os.W_OK):
            pytest.skip("filesystem/user ignores write permission bits")
        with pytest.raises(ConfigError) as exc:
            load_config_from_dict(
                {
                    "app": {
                        "retain_debug_audio": True,
                        "debug_audio_directory": str(readonly),
                    }
                }
            )
        assert "not writable" in str(exc.value)
    finally:
        readonly.chmod(stat.S_IRWXU)  # restore so tmp cleanup can remove it


def test_effective_config_is_json_serializable() -> None:
    import json

    config = Config()
    # Should not raise.
    json.dumps(config.effective())


def test_invalid_toml_reports_readable_error(tmp_path: Path) -> None:
    bad = tmp_path / "config.toml"
    bad.write_text("this is = = not toml", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(bad)
    assert "not valid TOML" in str(exc.value)


def test_render_toml_round_trips() -> None:
    original = load_config_from_dict({"app": {"mode": "polished"}, "audio": {"sample_rate": 24000}})
    text = render_toml(original)
    # The rendered document must parse and re-validate to the same config.
    reparsed = load_config_from_dict(tomllib.loads(text))
    assert reparsed == original


def test_render_toml_escapes_strings() -> None:
    config = load_config_from_dict({"transcription": {"initial_prompt": 'say "hi"\\there'}})
    text = render_toml(config)
    reparsed = load_config_from_dict(tomllib.loads(text))
    assert reparsed.transcription.initial_prompt == 'say "hi"\\there'


def test_default_config_path_ends_with_expected_name() -> None:
    path = default_config_path()
    assert path.name == "config.toml"
    assert "local-flow" in str(path)
