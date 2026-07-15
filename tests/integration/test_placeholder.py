"""Integration-test placeholder.

Real integration tests (WAV loading, live backends, Ollama, clipboard) arrive
in later phases and are marked ``integration`` so CI's ``-m "not integration"``
run skips them. This placeholder keeps the marker meaningful from Phase 0.
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
def test_integration_marker_is_registered() -> None:
    # Deselected by `pytest -m "not integration"`; runs only when explicitly
    # requested. Later phases replace this with real hardware/model tests.
    assert True
