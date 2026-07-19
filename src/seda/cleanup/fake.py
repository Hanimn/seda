"""In-memory fake cleanup provider for tests (IMPLEMENTATION_PLAN.md §25).

Programmable to return canned output, cycle through a sequence of outputs, be
unavailable, or raise — so unit tests can drive the whole §25 validation matrix
(valid edit, empty, preface, apparent answer, placeholder tampering,
over-expansion, timeout, malformed) without a real Ollama endpoint or network.
"""

from __future__ import annotations

from seda.errors import CleanupError


class FakeCleanupProvider:
    """A structural :class:`~seda.cleanup.base.CleanupProvider` double.

    Parameters
    ----------
    output:
        Fixed string returned by :meth:`clean` (ignoring the input).
    outputs:
        A sequence of outputs returned in order across successive calls; takes
        precedence over ``output``.
    echo:
        When ``True`` and neither ``output`` nor ``outputs`` is set, return the
        transcript unchanged (a "valid, no-op cleanup").
    available:
        Value returned by :meth:`is_available`.
    raise_error:
        When set, :meth:`clean` raises this instead of returning (simulates a
        transport/timeout failure).
    """

    def __init__(
        self,
        *,
        output: str | None = None,
        outputs: list[str] | None = None,
        echo: bool = True,
        available: bool = True,
        raise_error: CleanupError | None = None,
    ) -> None:
        self._output = output
        self._outputs = outputs
        self._echo = echo
        self._available = available
        self._raise_error = raise_error
        self.calls: list[tuple[str, str, list[str]]] = []

    def is_available(self) -> bool:
        return self._available

    def clean(self, transcript: str, mode: str, vocabulary: list[str]) -> str:
        self.calls.append((transcript, mode, list(vocabulary)))
        if self._raise_error is not None:
            raise self._raise_error
        if self._outputs is not None:
            idx = min(len(self.calls) - 1, len(self._outputs) - 1)
            return self._outputs[idx]
        if self._output is not None:
            return self._output
        if self._echo:
            return transcript
        return ""
