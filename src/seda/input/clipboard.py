"""Clipboard abstraction for text insertion (IMPLEMENTATION_PLAN.md §16).

The :class:`ClipboardProvider` protocol is the seam between the text-insertion
orchestrator and the platform clipboard.  It intentionally speaks only *text*:

* ``read_text()`` returns the current clipboard text, or ``None`` when the
  clipboard holds a non-text payload (image, files, rich text).  A ``None``
  result means "I cannot represent this as text" and callers must not treat it
  as an empty string — see the restoration race in :mod:`seda.input.paste`.
* ``write_text()`` replaces the clipboard with plain text.

The MVP restores only text clipboards (§16 "Clipboard ownership limitations");
richer, multi-format preservation is deliberately out of scope and left to a
future native provider behind this same interface.

Two implementations live here:

* :class:`PyperclipClipboard` — the production provider, backed by ``pyperclip``.
* :class:`FakeClipboard` — an in-memory double for tests, able to model a
  non-text clipboard via :meth:`FakeClipboard.set_non_text`.

Clipboard *contents* are never logged (§21); this module only ever moves text
between memory and the OS clipboard.
"""

from __future__ import annotations

from typing import Protocol


class ClipboardProvider(Protocol):
    """Minimal text-only clipboard interface."""

    def read_text(self) -> str | None:
        """Return the clipboard text, or ``None`` if it is not text."""
        ...

    def write_text(self, text: str) -> None:
        """Replace the clipboard contents with ``text``."""
        ...


class PyperclipClipboard:
    """Production clipboard provider backed by ``pyperclip``.

    ``pyperclip`` exposes text only: a non-text clipboard (image/files) comes
    back as an empty string on most platforms, which is indistinguishable from
    a genuinely empty text clipboard.  We therefore treat an empty read as
    text ``""`` and rely on the orchestrator's race check (does the clipboard
    still equal the transcript?) rather than trying to detect non-text here.
    """

    def read_text(self) -> str | None:
        import pyperclip

        try:
            text: str = pyperclip.paste()
            return text
        except Exception:  # noqa: BLE001 - pyperclip raises platform-specific errors
            # An unavailable clipboard is reported as "no text" rather than
            # crashing the pipeline; the caller degrades to leaving the
            # transcript in place.
            return None

    def write_text(self, text: str) -> None:
        import pyperclip

        pyperclip.copy(text)


class FakeClipboard:
    """In-memory clipboard for tests.

    Holds either a text value or a sentinel "non-text" payload.  ``read_text``
    returns ``None`` for the non-text case, mirroring a real clipboard holding
    an image or files.
    """

    def __init__(self, initial: str = "") -> None:
        self._text: str | None = initial

    def read_text(self) -> str | None:
        return self._text

    def write_text(self, text: str) -> None:
        self._text = text

    def set_non_text(self) -> None:
        """Model a clipboard holding a non-text payload (image/files)."""
        self._text = None
