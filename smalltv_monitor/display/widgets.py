"""Widget model for the /v1/display protocol (docs/WIDGET-PROTOCOL.md).

Widgets are plain dataclasses that serialise to the protocol's JSON. They carry no drawing logic:
the device renders them, scripts/preview.py approximates that on the PC.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from .icons import BUILTIN, ICON_HASH

SCREEN_W = SCREEN_H = 240
MAX_STRING = 31
MAX_SEGMENTS = 4
MAX_BARS = 32
MAX_SPARK = 120

# Glyphs compiled into the device fonts (space is always drawable)
_ASCII = {chr(c) for c in range(0x20, 0x7F)}
FONT_CHARSETS: dict[int, frozenset[str]] = {
    0: frozenset(_ASCII | {"°"}),  # NotoSansBold 15
    1: frozenset(_ASCII),  # NotoSansBold 36
    2: frozenset(_ASCII | {"°"}),  # NotoSansMono-Bold 20
}
LABEL_FONT = 0

_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")

# A value: number, string, None (no data), (value, colour) override, or stacked segments
Segment = tuple[float, str]
Value = float | int | str | None | tuple[float | int | str, str] | Sequence[Segment]


class LayoutError(ValueError):
    """The screen violates the protocol; the device would reject it."""


Rect = tuple[int, int, int, int]


def _check_colour(colour: str | None, where: str) -> None:
    if colour is not None and not _COLOUR.match(colour):
        raise LayoutError(f"{where}: colour {colour!r} is not #rrggbb")


def _check_text(text: str | None, font: int, where: str) -> None:
    if text is None:
        return
    if len(text) > MAX_STRING:
        raise LayoutError(f"{where}: {text!r} is longer than {MAX_STRING} characters")
    missing = set(text) - FONT_CHARSETS[font]
    if missing:
        raise LayoutError(f"{where}: font {font} has no glyph for {''.join(sorted(missing))!r}")


def _encode_value(value: Any, where: str) -> Any:
    """Normalise a value to its JSON form and validate it."""
    if value is None or isinstance(value, (int, float, str)):
        return value
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], str):
        _check_colour(value[1], where)
        return [value[0], value[1]]
    segments = list(value)
    if len(segments) > MAX_SEGMENTS:
        raise LayoutError(f"{where}: {len(segments)} segments, max {MAX_SEGMENTS}")
    out = []
    for amount, colour in segments:
        _check_colour(colour, where)
        out.append([amount, colour])
    return out


def _round(value: Any, digits: int) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, list):
        return [_round(v, digits) for v in value]
    return value


@dataclass(kw_only=True)
class Widget:
    """Common fields. `id` must be stable across polls; `r` is [x, y, w, h] in pixels."""

    kind: ClassVar[str]
    id: str
    r: Rect
    c: str | None = None
    bg: str | None = None
    tc: str | None = None
    lc: str | None = None

    def fields(self) -> dict[str, Any]:
        """Type-specific JSON fields (without None values)."""
        return {}

    def validate(self) -> None:
        x, y, w, h = self.r
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > SCREEN_W or y + h > SCREEN_H:
            raise LayoutError(f"{self.id}: rect {list(self.r)} is not inside {SCREEN_W}x{SCREEN_H}")
        for name in ("c", "bg", "tc", "lc"):
            _check_colour(getattr(self, name), f"{self.id}.{name}")

    def to_json(self) -> dict[str, Any]:
        self.validate()
        out: dict[str, Any] = {"id": self.id, "t": self.kind, "r": list(self.r)}
        for name in ("c", "bg", "tc", "lc"):
            if (v := getattr(self, name)) is not None:
                out[name] = v
        # Optional fields are omitted when unset; a value is always sent (null = no data, per the protocol)
        out.update({k: v for k, v in self.fields().items() if v is not None or k == "v"})
        return out


@dataclass(kw_only=True)
class Text(Widget):
    kind = "text"
    text: str
    f: int = 0
    a: str = "l"

    def validate(self) -> None:
        super().validate()
        _check_text(self.text, self.f, self.id)

    def fields(self) -> dict[str, Any]:
        return {"text": self.text, "f": self.f, "a": self.a}


@dataclass(kw_only=True)
class ValueText(Widget):
    """Number or short string plus a unit appended verbatim (include a leading space if wanted).

    With `label`, the label is drawn left-aligned (colour `lc`) and the value right-aligned.
    """

    kind = "value"
    v: Value
    unit: str = ""
    f: int = 0
    a: str = "l"
    dp: int = 0
    label: str | None = None

    def validate(self) -> None:
        super().validate()
        _check_text(self.unit, self.f, f"{self.id}.unit")
        _check_text(self.label, self.f, f"{self.id}.label")
        raw = self.v[0] if isinstance(self.v, tuple) else self.v
        if isinstance(raw, str):
            _check_text(raw, self.f, self.id)

    def fields(self) -> dict[str, Any]:
        return {"v": _round(_encode_value(self.v, self.id), self.dp), "unit": self.unit or None, "f": self.f,
                "a": None if self.label else self.a, "dp": self.dp or None, "label": self.label}


@dataclass(kw_only=True)
class Bar(Widget):
    kind = "bar"
    v: Value
    max: float = 100
    dir: str = "h"
    track: str | None = None
    label: str | None = None
    radius: int = 0  # 0 = straight; segmented fills are always drawn square

    def validate(self) -> None:
        super().validate()
        _check_colour(self.track, f"{self.id}.track")
        _check_text(self.label, LABEL_FONT, f"{self.id}.label")

    def fields(self) -> dict[str, Any]:
        return {"v": _round(_encode_value(self.v, self.id), 1), "max": _round(self.max, 1),
                "dir": None if self.dir == "h" else self.dir, "track": self.track, "label": self.label,
                "radius": self.radius or None}


@dataclass(kw_only=True)
class Bars(Widget):
    """Several bars side by side (per-core CPU, histograms)."""

    kind = "bars"
    v: Sequence[Value]
    max: float = 100
    dir: str = "v"
    gap: int = 1
    track: str | None = None

    def validate(self) -> None:
        super().validate()
        _check_colour(self.track, f"{self.id}.track")
        if len(self.v) > MAX_BARS:
            raise LayoutError(f"{self.id}: {len(self.v)} bars, max {MAX_BARS}")

    def fields(self) -> dict[str, Any]:
        return {"v": [_round(_encode_value(b, self.id), 1) for b in self.v], "max": _round(self.max, 1),
                "dir": self.dir, "gap": self.gap, "track": self.track}


@dataclass(kw_only=True)
class Ring(Widget):
    kind = "ring"
    v: Value
    max: float = 100
    f: int = 1
    track: str | None = None
    label: str | None = None

    def validate(self) -> None:
        super().validate()
        _check_colour(self.track, f"{self.id}.track")
        _check_text(self.label, LABEL_FONT, f"{self.id}.label")

    def fields(self) -> dict[str, Any]:
        return {"v": _round(_encode_value(self.v, self.id), 1), "max": _round(self.max, 1), "f": self.f,
                "track": self.track, "label": self.label}


@dataclass(kw_only=True)
class Spark(Widget):
    kind = "spark"
    v: Sequence[float] | None
    max: float = 100
    min: float = 0
    fill: str | None = None
    grad: Sequence[Segment] | None = None  # height gradient stops (value, colour), ascending
    fa: int | None = None  # gradient fill opacity in percent (device default 35)

    def validate(self) -> None:
        super().validate()
        _check_colour(self.fill, f"{self.id}.fill")
        if self.v is not None and len(self.v) > MAX_SPARK:
            raise LayoutError(f"{self.id}: {len(self.v)} samples, max {MAX_SPARK}")
        if self.grad is not None:
            values = [v for v, _ in self.grad]
            if not 2 <= len(values) <= 8 or any(b <= a for a, b in zip(values, values[1:])):
                raise LayoutError(f"{self.id}: grad needs 2..8 strictly ascending stops")
            for _, colour in self.grad:
                _check_colour(colour, f"{self.id}.grad")
        if self.fa is not None and not 0 <= self.fa <= 100:
            raise LayoutError(f"{self.id}: fa must be 0..100")

    def fields(self) -> dict[str, Any]:
        samples = None if self.v is None else [round(s) for s in self.v]
        grad = None if self.grad is None else [[v, c] for v, c in self.grad]
        return {"v": samples, "max": self.max, "min": self.min or None, "fill": self.fill, "grad": grad,
                "fa": self.fa}


@dataclass(kw_only=True)
class Icon(Widget):
    """A PC-served icon (/v1/icon/<name>, cached by the device per content hash) or a firmware built-in."""

    kind = "icon"
    v: str

    def validate(self) -> None:
        super().validate()
        if self.v not in BUILTIN and self.v not in ICON_HASH:
            raise LayoutError(f"{self.id}: unknown icon {self.v!r}")

    def fields(self) -> dict[str, Any]:
        return {"v": self.v, "h": ICON_HASH.get(self.v)}


@dataclass(kw_only=True)
class Panel(Widget):
    kind = "panel"
    radius: int = 6

    def fields(self) -> dict[str, Any]:
        return {"radius": self.radius}


@dataclass
class ScreenSettings:
    bg: str = "#101418"
    bri: int = 80
    poll_ms: int = 1000
    offline_bri: int = 5
    offline_dim_s: int = 300

    def to_json(self) -> dict[str, Any]:
        _check_colour(self.bg, "screen.bg")
        if not 0 <= self.bri <= 100 or not 0 <= self.offline_bri <= 100:
            raise LayoutError("screen: brightness must be 0..100")
        return {"bg": self.bg, "bri": self.bri, "poll_ms": self.poll_ms, "offline_bri": self.offline_bri,
                "offline_dim_s": self.offline_dim_s}
