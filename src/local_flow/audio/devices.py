"""Audio input device enumeration and selection (see IMPLEMENTATION_PLAN.md §12).

``sounddevice`` is imported lazily so that ``--help`` and config-only commands
do not pay the PortAudio initialisation cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from local_flow.errors import AudioError

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class DeviceInfo:
    """Metadata for a single audio input device."""

    index: int
    name: str
    input_channels: int
    default_sample_rate: float
    is_default: bool


class DeviceError(AudioError):
    """An audio device could not be found or is ambiguous."""


def list_devices() -> list[DeviceInfo]:
    """Return all available audio input devices via ``sounddevice``.

    Raises :class:`AudioError` if PortAudio / sounddevice is unavailable.
    """
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:
        raise AudioError(f"sounddevice is not available: {exc}") from exc

    try:
        raw = sd.query_devices()
        default_input_idx = sd.default.device[0]
    except Exception as exc:  # noqa: BLE001
        raise AudioError(f"could not query audio devices: {exc}") from exc

    # query_devices() may return a single dict when there is exactly one device.
    if isinstance(raw, dict):
        raw = [raw]

    devices: list[DeviceInfo] = []
    for idx, dev in enumerate(raw):
        if dev.get("max_input_channels", 0) < 1:
            continue
        devices.append(
            DeviceInfo(
                index=idx,
                name=dev["name"],
                input_channels=dev["max_input_channels"],
                default_sample_rate=dev["default_samplerate"],
                is_default=(idx == default_input_idx),
            )
        )
    return devices


def resolve_device(spec: str | int | None) -> DeviceInfo | None:
    """Resolve a device *spec* to a :class:`DeviceInfo`.

    *spec* may be:

    - ``None`` — return ``None`` (caller uses the system default).
    - An ``int`` (or a string that parses as one) — select by index.
    - A string — exact name match first, then unambiguous partial match.

    Raises :class:`DeviceError` on a missing or ambiguous partial match.
    """
    if spec is None:
        return None

    devices = list_devices()

    # Numeric index.
    if isinstance(spec, int) or (isinstance(spec, str) and spec.lstrip("-").isdigit()):
        idx = int(spec)
        for dev in devices:
            if dev.index == idx:
                return dev
        raise DeviceError(f"no input device with index {idx}")

    # Exact name match.
    for dev in devices:
        if dev.name == spec:
            return dev

    # Case-insensitive partial match.
    spec_lower = spec.lower()
    matches = [dev for dev in devices if spec_lower in dev.name.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(f"'{d.name}'" for d in matches)
        raise DeviceError(f"device spec '{spec}' is ambiguous — matches: {names}")
    raise DeviceError(f"no input device matching '{spec}'")
