"""Unit tests for device/compute-type selection (see §13, §25)."""

from __future__ import annotations

from seda.transcription.selection import (
    compute_type_fallbacks,
    resolve_compute_type,
    resolve_device,
)


def test_auto_selects_cuda_when_available() -> None:
    assert resolve_device("auto", cuda_available=lambda: True) == "cuda"


def test_auto_falls_back_to_cpu_when_cuda_unavailable() -> None:
    assert resolve_device("auto", cuda_available=lambda: False) == "cpu"


def test_explicit_cpu_is_honored_even_if_cuda_present() -> None:
    assert resolve_device("cpu", cuda_available=lambda: True) == "cpu"


def test_explicit_cuda_is_honored() -> None:
    assert resolve_device("cuda", cuda_available=lambda: False) == "cuda"


def test_compute_type_auto_prefers_float16_on_cuda() -> None:
    assert resolve_compute_type("auto", "cuda") == "float16"


def test_compute_type_auto_prefers_int8_on_cpu() -> None:
    assert resolve_compute_type("auto", "cpu") == "int8"


def test_explicit_compute_type_is_honored() -> None:
    assert resolve_compute_type("int8_float16", "cuda") == "int8_float16"


def test_cuda_fallback_order_starts_with_float16() -> None:
    fallbacks = compute_type_fallbacks("cuda")
    assert fallbacks[0] == "float16"
    assert "int8_float16" in fallbacks


def test_cpu_fallback_is_int8() -> None:
    assert compute_type_fallbacks("cpu") == ("int8",)
