"""macOS-native clipboard via ``NSPasteboard`` (AppKit/PyObjC), #147.

Extends the text-only :class:`~seda.input.clipboard.ClipboardProvider` seam
with the optional **snapshot/restore** capability: the full pasteboard (all
items, all types — images, files, rich text) is captured before a paste cycle
overwrites it and put back afterwards by ``TextInserter``'s race-checked
restore. This closes the "prior non-text clipboard is lost" known limitation
on macOS, where PyObjC is already a dependency of the GUI path.

All AppKit imports are lazy so this module is importable (and unit-testable)
on any platform; only *calling* the provider needs macOS.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# UTI for plain text. Using the literal avoids importing AppKit just for the
# constant — NSPasteboard methods accept UTI strings directly.
_TEXT_UTI = "public.utf8-plain-text"


@dataclass(frozen=True)
class PasteboardSnapshot:
    """A full pasteboard capture: one entry per item, each a list of
    ``(uti, raw_bytes)`` pairs. Opaque to callers; only :class:`MacOSNativeClipboard`
    interprets it."""

    items: tuple[tuple[tuple[str, bytes], ...], ...]


class MacOSNativeClipboard:
    """Clipboard provider backed by ``NSPasteboard.generalPasteboard``.

    The pasteboard object is injectable so tests can substitute a fake (no
    AppKit on CI). ``_new_pasteboard_item`` / ``_to_data`` are the only
    AppKit-construction boundaries and are patched in tests.
    """

    def __init__(self, pasteboard: Any | None = None) -> None:
        self._pb = pasteboard

    # ------------------------------------------------------------------
    # ClipboardProvider (text seam)
    # ------------------------------------------------------------------

    def read_text(self) -> str | None:
        try:
            text = self._pasteboard().stringForType_(_TEXT_UTI)
        except Exception:  # noqa: BLE001 - mirror PyperclipClipboard's fail-safe
            return None
        return str(text) if text is not None else None

    def write_text(self, text: str) -> None:
        pb = self._pasteboard()
        pb.clearContents()
        pb.setString_forType_(text, _TEXT_UTI)

    # ------------------------------------------------------------------
    # Optional snapshot capability (used by TextInserter, #147)
    # ------------------------------------------------------------------

    def snapshot(self) -> PasteboardSnapshot:
        """Capture every item and type on the pasteboard as raw bytes."""
        items: list[tuple[tuple[str, bytes], ...]] = []
        for item in self._pasteboard().pasteboardItems() or []:
            pairs: list[tuple[str, bytes]] = []
            for uti in item.types() or []:
                data = item.dataForType_(uti)
                if data is not None:
                    pairs.append((str(uti), bytes(data)))
            if pairs:
                items.append(tuple(pairs))
        return PasteboardSnapshot(items=tuple(items))

    def restore(self, snapshot: PasteboardSnapshot) -> None:
        """Put a captured snapshot back, replacing the current contents."""
        pb = self._pasteboard()
        pb.clearContents()
        objects = []
        for pairs in snapshot.items:
            item = self._new_pasteboard_item()
            for uti, raw in pairs:
                item.setData_forType_(self._to_data(raw), uti)
            objects.append(item)
        if objects:
            pb.writeObjects_(objects)

    # ------------------------------------------------------------------
    # AppKit boundaries (lazy; patched in tests)
    # ------------------------------------------------------------------

    def _pasteboard(self) -> Any:
        if self._pb is None:
            from AppKit import NSPasteboard  # macOS-only

            self._pb = NSPasteboard.generalPasteboard()
        return self._pb

    def _new_pasteboard_item(self) -> Any:
        from AppKit import NSPasteboardItem  # macOS-only

        return NSPasteboardItem.alloc().init()

    def _to_data(self, raw: bytes) -> Any:
        from Foundation import NSData  # macOS-only

        return NSData.alloc().initWithBytes_length_(raw, len(raw))
