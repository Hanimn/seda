"""Platform sound playback for notification cues (#145).

Fire-and-forget: each cue spawns the platform's player and never waits on it,
so a slow or missing audio stack can never block a dictation cycle. Every
failure resolves to a silent no-op — sound is a nicety, never a hard
dependency. All local: bundled WAVs or a user-supplied file path; no network.

Platform dispatch:
- macOS: ``afplay`` (always present)
- Windows: stdlib ``winsound.PlaySound`` (async)
- Linux: ``paplay`` (PulseAudio/PipeWire) then ``aplay`` (ALSA), whichever exists
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def play_sound(path: Path) -> None:
    """Play *path* (a WAV file) on the best available platform player.

    Fully fail-open: unknown platform, missing player binary, or a playback
    error is swallowed (logged at DEBUG). Never raises.
    """
    try:
        _play(path)
    except Exception:  # noqa: BLE001 - a cue failure must never break dictation
        logger.debug("sound cue failed for %s", path, exc_info=True)


def _play(path: Path) -> None:
    plat = sys.platform
    if plat == "darwin":
        subprocess.Popen(
            ["/usr/bin/afplay", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    if plat.startswith("win"):
        # Windows-only stdlib module; imported dynamically so type-checking on
        # POSIX never sees it.
        winsound: Any = __import__("winsound")
        winsound.PlaySound(
            str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        )
        return
    # Linux and other POSIX desktops: first available player wins.
    for name in ("paplay", "aplay"):
        binary = shutil.which(name)
        if binary:
            subprocess.Popen(
                [binary, str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
