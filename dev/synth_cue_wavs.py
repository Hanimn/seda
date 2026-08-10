"""One-off: synthesize the bundled notification cue WAVs (#145)."""

import os
import wave

import numpy as np

OUT = "/opt/data/projects/seda/src/seda/notifications/sounds"
os.makedirs(OUT, exist_ok=True)
SR = 16000


def _envelope(n: int) -> np.ndarray:
    edge = int(SR * 0.008)
    env = np.ones(n, dtype=np.float32)
    env[:edge] = 0.5 - 0.5 * np.cos(np.pi * np.arange(edge) / edge)
    env[-edge:] = 0.5 - 0.5 * np.cos(np.pi * np.arange(edge, 0, -1) / edge)
    return env


def tone(freqs: list[float], dur_ms: int, gap_ms: int = 0) -> np.ndarray:
    seg = int(SR * dur_ms / 1000)
    gap = np.zeros(int(SR * gap_ms / 1000), dtype=np.float32)
    t = np.arange(seg) / SR
    parts = []
    for f in freqs:
        parts.append(np.sin(2 * np.pi * f * t).astype(np.float32) * 0.35 * _envelope(seg))
        parts.append(gap)
    return np.concatenate(parts)


def chirp(f0: float, f1: float, dur_ms: int) -> np.ndarray:
    seg = int(SR * dur_ms / 1000)
    t = np.arange(seg) / SR
    phase = 2 * np.pi * (f0 * t + (f1 - f0) / (2 * t[-1]) * t**2)
    return (np.sin(phase).astype(np.float32) * 0.35) * _envelope(seg)


def write(name: str, samples: np.ndarray) -> None:
    int16 = (samples.clip(-1, 1) * 32767).astype(np.int16)
    p = os.path.join(OUT, f"{name}.wav")
    with wave.open(p, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(int16.tobytes())
    print(name, f"{len(int16) / SR:.3f}s", os.path.getsize(p), "bytes")


write("start", chirp(660, 880, 70))
write("stop", chirp(880, 660, 70))
write("success", tone([880, 1174], 45, gap_ms=25))
write("error", tone([220], 120))
