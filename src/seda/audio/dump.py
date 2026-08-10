"""Debug-audio retention (``app.retain_debug_audio``, #126).

Writes the captured mono float32 buffer to a timestamped PCM WAV so users can
debug transcription quality against exactly what the model heard. Privacy:
retention is opt-in (off by default), files stay local, and — because they
contain the user's speech — they are created **owner-only (0600) from birth**
via an ``os.open`` opener, mirroring the log-file handler (no TOCTOU chmod
window).
"""

from __future__ import annotations

import os
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

from seda.errors import AudioError

# Owner-only mode for retained audio: it contains the user's dictated speech.
_WAV_FILE_MODE = 0o600


def write_debug_wav(
    directory: Path,
    samples: np.ndarray,
    sample_rate: int,
    *,
    now: datetime | None = None,
) -> Path:
    """Write *samples* (mono float32 in [-1, 1]) as an int16 PCM WAV.

    Returns the path written. Raises :class:`AudioError` for an empty buffer
    or an unwritable target. ``now`` is injectable so tests can pin the
    timestamped filename.
    """
    if samples.size == 0:
        raise AudioError("cannot retain debug audio: recording is empty")

    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")[:-3]
    path = directory / f"seda-{stamp}.wav"

    int16 = (samples.clip(-1.0, 1.0) * 32767).astype(np.int16)
    try:
        # os.open opener → mode 0600 at creation, never wider (umask can only
        # remove bits). POSIX-only; a harmless no-op on Windows.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _WAV_FILE_MODE)
        with os.fdopen(fd, "wb") as fh, wave.open(fh, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(int16.tobytes())
    except OSError as exc:
        raise AudioError(f"could not write debug audio to {path}: {exc}") from exc
    return path
