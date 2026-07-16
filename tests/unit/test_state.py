"""Unit tests for the application state machine (IMPLEMENTATION_PLAN.md §8)."""

from __future__ import annotations

import threading

import pytest

from local_flow.errors import InvalidTransitionError
from local_flow.state import VALID_TRANSITIONS, AppState, StateMachine


class TestAppState:
    def test_all_required_states_exist(self) -> None:
        names = {s.value for s in AppState}
        assert names == {
            "STARTING",
            "IDLE",
            "RECORDING",
            "PROCESSING_AUDIO",
            "TRANSCRIBING",
            "CLEANING",
            "PASTING",
            "CANCELLED",
            "ERROR",
            "STOPPING",
        }


class TestValidTransitionsTable:
    def test_required_primary_transitions_present(self) -> None:
        required = {
            (AppState.STARTING, AppState.IDLE),
            (AppState.IDLE, AppState.RECORDING),
            (AppState.RECORDING, AppState.PROCESSING_AUDIO),
            (AppState.RECORDING, AppState.CANCELLED),
            (AppState.PROCESSING_AUDIO, AppState.TRANSCRIBING),
            (AppState.PROCESSING_AUDIO, AppState.CANCELLED),
            (AppState.TRANSCRIBING, AppState.CANCELLED),
            (AppState.TRANSCRIBING, AppState.CLEANING),
            (AppState.TRANSCRIBING, AppState.PASTING),
            (AppState.CLEANING, AppState.PASTING),
            (AppState.PASTING, AppState.IDLE),
            (AppState.CANCELLED, AppState.IDLE),
        }
        assert required <= VALID_TRANSITIONS

    def test_any_state_to_error(self) -> None:
        for state in AppState:
            if state is not AppState.ERROR:
                assert (state, AppState.ERROR) in VALID_TRANSITIONS

    def test_error_to_idle_and_stopping(self) -> None:
        assert (AppState.ERROR, AppState.IDLE) in VALID_TRANSITIONS
        assert (AppState.ERROR, AppState.STOPPING) in VALID_TRANSITIONS


class TestStateMachineTransitions:
    def test_initial_state_is_starting(self) -> None:
        sm = StateMachine()
        assert sm.state is AppState.STARTING

    def test_valid_transition_succeeds(self) -> None:
        sm = StateMachine()
        sm.transition(AppState.IDLE)
        assert sm.state is AppState.IDLE

    def test_chain_of_valid_transitions(self) -> None:
        sm = StateMachine()
        sm.transition(AppState.IDLE)
        sm.transition(AppState.RECORDING)
        sm.transition(AppState.PROCESSING_AUDIO)
        sm.transition(AppState.TRANSCRIBING)
        sm.transition(AppState.PASTING)
        sm.transition(AppState.IDLE)
        assert sm.state is AppState.IDLE

    def test_invalid_transition_raises(self) -> None:
        sm = StateMachine()
        # STARTING -> RECORDING is not a valid transition
        with pytest.raises(InvalidTransitionError, match="STARTING"):
            sm.transition(AppState.RECORDING)

    def test_invalid_transition_does_not_change_state(self) -> None:
        sm = StateMachine()
        with pytest.raises(InvalidTransitionError):
            sm.transition(AppState.RECORDING)
        assert sm.state is AppState.STARTING

    def test_all_invalid_pairs_raise(self) -> None:
        for from_state in AppState:
            for to_state in AppState:
                if (from_state, to_state) not in VALID_TRANSITIONS:
                    sm = StateMachine(initial=from_state)
                    with pytest.raises(InvalidTransitionError):
                        sm.transition(to_state)

    def test_transition_to_error_from_any_non_error_state(self) -> None:
        for state in AppState:
            if state is not AppState.ERROR:
                sm = StateMachine(initial=state)
                sm.transition(AppState.ERROR)
                assert sm.state is AppState.ERROR

    def test_concurrent_transitions_do_not_corrupt_state(self) -> None:
        """Two threads race to transition; exactly one should win."""
        sm = StateMachine(initial=AppState.IDLE)
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def try_record() -> None:
            barrier.wait()
            try:
                sm.transition(AppState.RECORDING)
            except InvalidTransitionError as exc:
                errors.append(exc)

        t1 = threading.Thread(target=try_record)
        t2 = threading.Thread(target=try_record)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one should have won the transition.
        assert sm.state is AppState.RECORDING
        assert len(errors) == 1
