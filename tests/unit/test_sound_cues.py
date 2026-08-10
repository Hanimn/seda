"""Unit tests for sound cues (#145) — notifier mapping and player dispatch.

No real audio device: the player's subprocess/winsound boundaries are faked.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from seda.config import NotificationsConfig
from seda.notifications import NotificationEvent, SoundNotifier
from seda.notifications import player as player_module


class _SpyPlayer:
    def __init__(self) -> None:
        self.played: list[Path] = []
        self.fail: Exception | None = None

    def __call__(self, path: Path) -> None:
        if self.fail is not None:
            raise self.fail
        self.played.append(path)


def _notifier(spy: _SpyPlayer, **overrides: object) -> SoundNotifier:
    cfg = NotificationsConfig(**overrides)  # type: ignore[arg-type]
    return SoundNotifier(cfg, play=spy)


class TestEventMapping:
    def test_cycle_events_play_start_stop_success(self) -> None:
        spy = _SpyPlayer()
        n = _notifier(spy)
        n.notify(NotificationEvent.RECORDING)
        n.notify(NotificationEvent.TRANSCRIBING)
        n.notify(NotificationEvent.SUCCESS)
        names = [p.name for p in spy.played]
        assert names == ["start.wav", "stop.wav", "success.wav"]

    def test_error_event_plays_error_cue(self) -> None:
        spy = _SpyPlayer()
        _notifier(spy).notify(NotificationEvent.ERROR)
        assert [p.name for p in spy.played] == ["error.wav"]

    def test_unmapped_events_are_silent(self) -> None:
        spy = _SpyPlayer()
        n = _notifier(spy)
        for event in (
            NotificationEvent.READY,
            NotificationEvent.BUSY,
            NotificationEvent.CANCELLED,
            NotificationEvent.PASTING,
            NotificationEvent.CLEANING,
        ):
            n.notify(event)
        assert spy.played == []

    def test_disabled_plays_nothing(self) -> None:
        spy = _SpyPlayer()
        n = _notifier(spy, sound_enabled=False)
        n.notify(NotificationEvent.RECORDING)
        n.notify(NotificationEvent.SUCCESS)
        assert spy.played == []

    def test_custom_sound_path_overrides_bundled(self, tmp_path: Path) -> None:
        spy = _SpyPlayer()
        custom = tmp_path / "my-start.wav"
        custom.write_bytes(b"RIFF")
        n = _notifier(spy, recording_start_sound=str(custom))
        n.notify(NotificationEvent.RECORDING)
        assert spy.played == [custom]

    def test_player_failure_is_swallowed(self) -> None:
        spy = _SpyPlayer()
        spy.fail = RuntimeError("no audio device")
        n = _notifier(spy)
        # Must not raise — a cue failure never breaks dictation.
        n.notify(NotificationEvent.RECORDING)

    def test_missing_custom_file_falls_back_to_silence(self, tmp_path: Path) -> None:
        spy = _SpyPlayer()
        n = _notifier(spy, recording_start_sound=str(tmp_path / "nope.wav"))
        n.notify(NotificationEvent.RECORDING)
        assert spy.played == []


class TestPlayerDispatch:
    def test_unknown_platform_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(player_module.sys, "platform", "plan9")
        player_module.play_sound(Path("/tmp/x.wav"))  # must not raise

    def test_missing_player_binary_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(player_module.sys, "platform", "linux")
        monkeypatch.setattr(player_module.shutil, "which", lambda _name: None)
        player_module.play_sound(Path("/tmp/x.wav"))  # must not raise

    def test_linux_uses_paplay_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        class _FakePopen:
            def __init__(self, args: list[str], **_kw: object) -> None:
                calls.append(args)

        monkeypatch.setattr(player_module.sys, "platform", "linux")
        monkeypatch.setattr(
            player_module.shutil,
            "which",
            lambda name: "/usr/bin/paplay" if name == "paplay" else None,
        )
        monkeypatch.setattr(player_module.subprocess, "Popen", _FakePopen)
        player_module.play_sound(Path("/tmp/x.wav"))
        assert calls == [["/usr/bin/paplay", "/tmp/x.wav"]]
