"""Device and compute-type auto-selection (see IMPLEMENTATION_PLAN.md §13).

Kept free of any faster-whisper / CUDA imports so it is unit-testable without
hardware: hardware detection is injected as a callable. The resolved device
and compute type are meant to be logged — never the transcript.
"""

from __future__ import annotations

from collections.abc import Callable

# Compute-type preference order per §13, chosen by resolved device.
_CUDA_COMPUTE_PREFERENCE = ("float16", "int8_float16")
_CPU_COMPUTE_PREFERENCE = ("int8",)


def _cuda_unavailable() -> bool:
    """Default CUDA probe: report unavailable when the check can't be made.

    faster-whisper (CTranslate2) is an optional extra, so importing it may
    fail; treat any failure as "no CUDA" and fall back to CPU.
    """
    try:
        from ctranslate2 import get_cuda_device_count
    except ImportError:
        return True
    try:
        return bool(get_cuda_device_count() <= 0)
    except Exception:
        return True


def resolve_device(
    configured: str,
    *,
    cuda_available: Callable[[], bool] | None = None,
) -> str:
    """Resolve ``transcription.device`` to a concrete "cuda" or "cpu".

    ``configured`` is one of "auto", "cpu", "cuda". "auto" picks CUDA when
    available, else CPU. An explicit "cuda"/"cpu" is honored as-is (the caller
    surfaces a hard failure later if an explicit "cuda" can't initialize).
    """
    if configured == "cpu":
        return "cpu"
    if configured == "cuda":
        return "cuda"
    # "auto"
    probe = cuda_available if cuda_available is not None else (lambda: not _cuda_unavailable())
    return "cuda" if probe() else "cpu"


def resolve_compute_type(configured: str, device: str) -> str:
    """Resolve ``transcription.compute_type`` given the resolved ``device``.

    An explicit value is honored. "auto" picks the first preferred type for
    the device; if the backend rejects it at load time, the backend is
    responsible for falling back safely.
    """
    if configured != "auto":
        return configured
    preference = _CUDA_COMPUTE_PREFERENCE if device == "cuda" else _CPU_COMPUTE_PREFERENCE
    return preference[0]


def compute_type_fallbacks(device: str) -> tuple[str, ...]:
    """Ordered compute types to try for ``device`` when "auto" is configured."""
    return _CUDA_COMPUTE_PREFERENCE if device == "cuda" else _CPU_COMPUTE_PREFERENCE
