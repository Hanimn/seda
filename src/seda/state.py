"""Application state machine (IMPLEMENTATION_PLAN.md §8).

The state machine guards every state transition with a lock so hotkey
callbacks, the audio-processing worker, and the main thread can all call
``transition()`` safely without external synchronisation.
"""

from __future__ import annotations

import threading
from enum import StrEnum

from seda.errors import InvalidTransitionError


class AppState(StrEnum):
    """Every possible state of the push-to-talk controller."""

    STARTING = "STARTING"
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    PROCESSING_AUDIO = "PROCESSING_AUDIO"
    TRANSCRIBING = "TRANSCRIBING"
    CLEANING = "CLEANING"
    PASTING = "PASTING"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"
    STOPPING = "STOPPING"


# Valid (from, to) transition pairs per §8.  Any pair not in this set is
# illegal; ``StateMachine.transition()`` raises ``InvalidTransitionError``.
VALID_TRANSITIONS: frozenset[tuple[AppState, AppState]] = frozenset(
    [
        (AppState.STARTING, AppState.IDLE),
        (AppState.IDLE, AppState.RECORDING),
        (AppState.RECORDING, AppState.PROCESSING_AUDIO),
        (AppState.RECORDING, AppState.CANCELLED),
        (AppState.PROCESSING_AUDIO, AppState.TRANSCRIBING),
        (AppState.PROCESSING_AUDIO, AppState.CANCELLED),
        (AppState.TRANSCRIBING, AppState.CLEANING),
        (AppState.TRANSCRIBING, AppState.PASTING),
        (AppState.TRANSCRIBING, AppState.CANCELLED),
        (AppState.CLEANING, AppState.PASTING),
        (AppState.PASTING, AppState.IDLE),
        (AppState.CANCELLED, AppState.IDLE),
        (AppState.ERROR, AppState.IDLE),
        (AppState.ERROR, AppState.STOPPING),
    ]
    # Any non-ERROR state → ERROR is always valid.
    + [(s, AppState.ERROR) for s in AppState if s is not AppState.ERROR]
    # Any state → STOPPING is valid (shutdown can be triggered from anywhere).
    + [(s, AppState.STOPPING) for s in AppState if s is not AppState.STOPPING]
)


class StateMachine:
    """Thread-safe state machine for the push-to-talk controller."""

    def __init__(self, initial: AppState = AppState.STARTING) -> None:
        self._state = initial
        self._lock = threading.Lock()

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    def transition(self, to: AppState) -> None:
        """Atomically move to *to*, or raise :exc:`InvalidTransitionError`."""
        with self._lock:
            from_ = self._state
            if (from_, to) not in VALID_TRANSITIONS:
                raise InvalidTransitionError(f"invalid transition: {from_} -> {to}")
            self._state = to
