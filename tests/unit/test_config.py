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

from seda.config import (
    Config,
    ConfigError,
    apply_settings_edits,
    default_config_path,
    load_config,
    load_config_from_dict,
    render_toml,
    save_config,
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


def test_empty_per_platform_hotkey_is_rejected() -> None:
    # The per-platform default fields must be real hotkeys — only the
    # override field (push_to_talk / toggle_mode) may be empty.
    with pytest.raises(ConfigError) as exc:
        load_config_from_dict({"hotkeys": {"push_to_talk_macos": ""}})
    assert "hotkeys.push_to_talk_macos" in str(exc.value)


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


def test_paste_defaults_are_safe() -> None:
    # Default multiline policy preserves text (§16 "Preserve multiline text by
    # default"); platform shortcuts have sensible defaults.
    paste = Config().paste
    assert paste.multiline_policy == "preserve"
    assert paste.shortcut_macos == "cmd+v"
    assert paste.shortcut_windows == "ctrl+v"
    assert paste.shortcut_linux_gui == "ctrl+v"
    assert paste.shortcut_linux_terminal == "ctrl+shift+v"


@pytest.mark.parametrize("policy", ["preserve", "flatten", "copy_only"])
def test_valid_multiline_policy_accepted(policy: str) -> None:
    config = load_config_from_dict({"paste": {"multiline_policy": policy}})
    assert config.paste.multiline_policy == policy


def test_invalid_multiline_policy_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config_from_dict({"paste": {"multiline_policy": "explode"}})
    assert "paste.multiline_policy" in str(exc.value)


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


class TestSelectPushToTalk:
    """Platform-aware push-to-talk selection (issue #9)."""

    def test_macos_default_avoids_ctrl_alt_space_and_cmd_space(self) -> None:
        from seda.config import HotkeysConfig, select_push_to_talk

        key = select_push_to_talk(HotkeysConfig(), platform="darwin")
        # Must not be the Windows/Linux chord (collides with input switcher)
        # and must not be Spotlight's Cmd+Space.
        assert key != "<ctrl>+<alt>+space"
        assert key.replace(" ", "").lower() not in ("<cmd>+space", "cmd+space")
        # And it must be a valid hotkey (validation would already have run).
        assert key

    def test_windows_default(self) -> None:
        from seda.config import HotkeysConfig, select_push_to_talk

        assert select_push_to_talk(HotkeysConfig(), platform="win32") == "<ctrl>+<alt>+space"

    def test_linux_default(self) -> None:
        from seda.config import HotkeysConfig, select_push_to_talk

        assert select_push_to_talk(HotkeysConfig(), platform="linux") == "<ctrl>+<alt>+space"

    def test_explicit_push_to_talk_overrides_platform_default(self) -> None:
        from seda.config import HotkeysConfig, select_push_to_talk

        cfg = HotkeysConfig(push_to_talk="<f8>")
        # An explicit user value wins on every platform.
        assert select_push_to_talk(cfg, platform="darwin") == "<f8>"
        assert select_push_to_talk(cfg, platform="win32") == "<f8>"

    def test_per_platform_field_respected(self) -> None:
        from seda.config import HotkeysConfig, select_push_to_talk

        cfg = HotkeysConfig(push_to_talk_macos="<cmd>+<shift>+d")
        assert select_push_to_talk(cfg, platform="darwin") == "<cmd>+<shift>+d"

    def test_toggle_mode_macos_default_avoids_ctrl_alt(self) -> None:
        # The toggle-mode per-platform fields are spec-required data (issue #9);
        # even without a runtime consumer yet, the macOS default must not reuse
        # the Windows/Linux chord.
        from seda.config import HotkeysConfig

        cfg = HotkeysConfig()
        assert cfg.toggle_mode_macos != "<ctrl>+<alt>+m"
        assert cfg.toggle_mode_windows == "<ctrl>+<alt>+m"
        assert cfg.toggle_mode_linux == "<ctrl>+<alt>+m"


class TestSelectOverlayEnabled:
    """Overlay enable resolution: flag > explicit config > platform (ADR-0004)."""

    def test_default_none_is_on_for_macos(self) -> None:
        from seda.config import OverlayConfig, select_overlay_enabled

        assert select_overlay_enabled(OverlayConfig(), platform="darwin") is True

    def test_default_none_is_off_for_others(self) -> None:
        from seda.config import OverlayConfig, select_overlay_enabled

        assert select_overlay_enabled(OverlayConfig(), platform="win32") is False
        assert select_overlay_enabled(OverlayConfig(), platform="linux") is False

    def test_no_overlay_flag_forces_off_even_on_macos(self) -> None:
        from seda.config import OverlayConfig, select_overlay_enabled

        # Flag wins over everything, including an explicit enabled=True.
        assert (
            select_overlay_enabled(OverlayConfig(enabled=True), no_overlay=True, platform="darwin")
            is False
        )

    def test_explicit_true_wins_on_every_platform(self) -> None:
        from seda.config import OverlayConfig, select_overlay_enabled

        cfg = OverlayConfig(enabled=True)
        assert select_overlay_enabled(cfg, platform="darwin") is True
        # Note: on non-macOS this only *requests* the overlay; the GUI host then
        # fails open (no AppKit). Resolution here still returns True (ADR-0004).
        assert select_overlay_enabled(cfg, platform="linux") is True

    def test_explicit_false_wins_on_macos(self) -> None:
        from seda.config import OverlayConfig, select_overlay_enabled

        assert select_overlay_enabled(OverlayConfig(enabled=False), platform="darwin") is False


class TestOverlayConfig:
    def test_default_enabled_is_none(self) -> None:
        from seda.config import OverlayConfig

        assert OverlayConfig().enabled is None

    def test_overlay_section_loads_from_dict(self) -> None:
        config = load_config_from_dict({"overlay": {"enabled": True}})
        assert config.overlay.enabled is True

    def test_overlay_defaults_when_absent(self) -> None:
        config = load_config_from_dict({})
        assert config.overlay.enabled is None

    def test_overlay_rejects_unknown_key(self) -> None:
        # _Section uses extra="forbid" — a typo must be caught.
        with pytest.raises(ConfigError):
            load_config_from_dict({"overlay": {"enabledd": True}})


def test_empty_push_to_talk_override_is_allowed() -> None:
    # Empty override means "use the platform default" — must not be rejected.
    config = load_config_from_dict({"hotkeys": {"push_to_talk": ""}})
    assert config.hotkeys.push_to_talk == ""


def test_application_overrides_accepted() -> None:
    config = load_config_from_dict(
        {
            "paste": {
                "application_overrides": [
                    {"application": "iTerm2", "shortcut": "cmd+v"},
                    {"application": "Windows Terminal", "shortcut": "ctrl+shift+v"},
                ]
            }
        }
    )
    overrides = config.paste.application_overrides
    assert len(overrides) == 2
    assert overrides[0].application == "iTerm2"
    assert overrides[0].shortcut == "cmd+v"
    assert overrides[1].application == "Windows Terminal"
    assert overrides[1].shortcut == "ctrl+shift+v"


def test_application_overrides_default_empty() -> None:
    config = load_config_from_dict({})
    assert config.paste.application_overrides == []


def test_application_overrides_render_as_toml() -> None:
    from seda.config import render_toml

    config = load_config_from_dict(
        {
            "paste": {
                "application_overrides": [
                    {"application": "iTerm2", "shortcut": "cmd+v"},
                ]
            }
        }
    )
    toml = render_toml(config)
    assert "iTerm2" in toml
    assert "cmd+v" in toml


def test_application_overrides_round_trip_through_toml() -> None:
    """Rendered TOML can be re-parsed and produces identical config."""
    import tomllib

    from seda.config import render_toml

    original = load_config_from_dict(
        {
            "paste": {
                "application_overrides": [
                    {"application": "iTerm2", "shortcut": "cmd+v"},
                    {"application": "Windows Terminal", "shortcut": "ctrl+shift+v"},
                ]
            }
        }
    )
    rendered = render_toml(original)
    # The rendered TOML must be valid and parse back to the same values.
    reparsed_data = tomllib.loads(rendered)
    reparsed = load_config_from_dict(reparsed_data)
    assert reparsed.paste.application_overrides == original.paste.application_overrides


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
    assert "seda" in str(path)


# --- migration notice (Local Flow → Seda rename) ---------------------------


def _patch_config_dirs(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Map user_config_path(<app>) to ``root/<app>`` for deterministic tests."""
    import seda.config as config_module

    def _fake(app_name: str, appauthor: bool = False) -> Path:  # noqa: FBT001,FBT002
        return root / app_name

    monkeypatch.setattr(config_module, "user_config_path", _fake)


def test_migration_notice_when_old_present_and_new_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from seda.config import OLD_APP_NAME, migration_notice

    _patch_config_dirs(monkeypatch, tmp_path)
    (tmp_path / OLD_APP_NAME).mkdir()  # old config exists, new does not

    notice = migration_notice()
    assert notice is not None
    assert OLD_APP_NAME in notice
    assert "seda" in notice


def test_no_notice_when_new_config_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from seda.config import APP_NAME, OLD_APP_NAME, migration_notice

    _patch_config_dirs(monkeypatch, tmp_path)
    (tmp_path / OLD_APP_NAME).mkdir()
    (tmp_path / APP_NAME).mkdir()  # new dir present → nothing to migrate

    assert migration_notice() is None


def test_no_notice_when_neither_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from seda.config import migration_notice

    _patch_config_dirs(monkeypatch, tmp_path)  # neither dir created
    assert migration_notice() is None


def test_migration_notice_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import seda.config as config_module
    from seda.config import migration_notice

    def _boom(app_name: str, appauthor: bool = False) -> Path:  # noqa: FBT001,FBT002
        raise RuntimeError("path resolution exploded")

    monkeypatch.setattr(config_module, "user_config_path", _boom)
    assert migration_notice() is None


# --- Settings-window apply/save (#88) --------------------------------------


def test_apply_settings_edits_updates_only_named_fields() -> None:
    """apply_settings_edits merges dotted-path edits, leaving other fields intact (#88)."""
    current = Config()
    assert current.cleanup.enabled is False
    updated = apply_settings_edits(
        current, {"cleanup.enabled": True, "transcription.model": "base.en"}
    )
    assert updated.cleanup.enabled is True
    assert updated.transcription.model == "base.en"
    # Untouched fields keep their values; the original is not mutated.
    assert updated.app.notify_on_ready == current.app.notify_on_ready
    assert current.cleanup.enabled is False, "the input Config must not be mutated"


def test_apply_settings_edits_rejects_invalid_with_readable_error() -> None:
    """An invalid edit re-validates and raises ConfigError with a readable message (#88)."""
    with pytest.raises(ConfigError) as exc:
        apply_settings_edits(Config(), {"paste.auto_submit": True})
    # The safety rule (§ never auto-submit) surfaces as a clear, in-window message.
    assert "auto_submit" in str(exc.value)


def test_apply_settings_edits_empty_is_a_noop_equal_config() -> None:
    """No edits yields a config equal to the input (#88)."""
    current = load_config_from_dict({"transcription": {"model": "small.en"}})
    assert apply_settings_edits(current, {}) == current


def test_save_config_writes_valid_reloadable_toml(tmp_path: Path) -> None:
    """save_config writes TOML that load_config reads back to the same config (#88)."""
    target = tmp_path / "config.toml"
    cfg = apply_settings_edits(Config(), {"cleanup.enabled": True})
    save_config(cfg, target)
    assert target.exists()
    reloaded = load_config(target)
    assert reloaded.cleanup.enabled is True
    assert reloaded == cfg


def test_apply_settings_edits_preserves_untouched_multiline_flatten() -> None:
    """Editing only cleanup must NOT clobber a 'flatten' multiline_policy (#88 review)."""
    current = load_config_from_dict({"paste": {"multiline_policy": "flatten"}})
    updated = apply_settings_edits(current, {"cleanup.enabled": True})
    assert updated.paste.multiline_policy == "flatten", "flatten must survive an unrelated edit"
    assert updated.cleanup.enabled is True


def test_apply_settings_edits_walks_nested_paths() -> None:
    """A 3-level path descends into nested tables, not a flat key (#88 review)."""
    updated = apply_settings_edits(Config(), {"cleanup.ollama.model": "qwen2.5:7b"})
    assert updated.cleanup.ollama.model == "qwen2.5:7b"
