"""Icons served to the device at /v1/icon/<name> (wire format in docs/WIDGET-PROTOCOL.md)."""

from __future__ import annotations

from .icon_data import ICONS

BUILTIN = frozenset({"dot", "warn", "check"})  # drawn natively by the firmware


def fnv1a32(data: bytes) -> int:
    h = 2166136261
    for byte in data:
        h = ((h ^ byte) * 16777619) & 0xFFFFFFFF
    return h


ICON_HASH: dict[str, int] = {name: fnv1a32(data) for name, data in ICONS.items()}


def _nibbles(data: bytes, count: int) -> list[int]:
    out: list[int] = []
    for byte in data:
        out += [byte >> 4, byte & 0x0F]
    return out[:count]


def decode_rgba(payload: bytes, colour: tuple[int, int, int]) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    """(width, height, RGBA pixels) for either wire format, e.g. for the PC preview. `colour` tints mono icons."""
    if payload[:2] != b"SI" or payload[2] not in (1, 2):
        raise ValueError("not an icon payload")
    width, height = payload[3], payload[4]
    if payload[2] == 1:
        return width, height, [(*colour, a * 17) for a in _nibbles(payload[5:], width * height)]
    count = payload[5]
    palette = []
    for i in range(count):
        hi, lo, alpha = payload[6 + 3 * i : 9 + 3 * i]
        v = (hi << 8) | lo
        palette.append((((v >> 11) & 0x1F) * 255 // 31, ((v >> 5) & 0x3F) * 255 // 63, (v & 0x1F) * 255 // 31, alpha))
    indices = _nibbles(payload[6 + 3 * count :], width * height)
    return width, height, [palette[i] for i in indices]
