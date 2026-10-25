"""A full screen (settings + widgets), validated and serialised within the device's size budget."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

from .widgets import LayoutError, ScreenSettings, Spark, Text, Widget

log = logging.getLogger(__name__)

PROTO = 1
MAX_BYTES = 4096  # device rejects larger responses (docs/GFX-LAB.md: ~2.3x heap while parsing)
MAX_WIDGETS = 32


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


@dataclass
class Screen:
    settings: ScreenSettings
    widgets: Sequence[Widget]

    def validate(self) -> None:
        if len(self.widgets) > MAX_WIDGETS:
            raise LayoutError(f"{len(self.widgets)} widgets, max {MAX_WIDGETS}")
        seen: set[str] = set()
        for i, w in enumerate(self.widgets):
            if w.id in seen:
                raise LayoutError(f"duplicate widget id {w.id!r}")
            seen.add(w.id)
            for other in self.widgets[i + 1 :]:
                if _overlap(w.r, other.r):
                    raise LayoutError(f"widgets {w.id!r} and {other.id!r} overlap")

    def to_json(self) -> dict:
        self.validate()
        return {"proto": PROTO, "screen": self.settings.to_json(), "w": [w.to_json() for w in self.widgets]}

    def to_bytes(self) -> bytes:
        return json.dumps(self.to_json(), separators=(",", ":"), ensure_ascii=False).encode()

    def fit(self, limit: int = MAX_BYTES) -> bytes:
        """Serialise, shortening sparkline histories (keeping the newest samples) until it fits."""
        screen = self
        body = screen.to_bytes()
        while len(body) > limit:
            sparks = [w for w in screen.widgets if isinstance(w, Spark) and _samples(w) > 8]
            if not sparks:
                raise LayoutError(f"screen is {len(body)} bytes, limit {limit}, nothing left to trim")
            longest = max(sparks, key=_samples)
            trimmed = replace(longest, v=_newer_half(longest.v), u=_newer_half(longest.u))
            screen = Screen(screen.settings, [trimmed if w is longest else w for w in screen.widgets])
            body = screen.to_bytes()
            log.debug("trimmed %s to %d samples (%d bytes)", longest.id, _samples(trimmed), len(body))
        return body



def _samples(spark: Spark) -> int:
    return len(spark.v or ()) + len(spark.u or ())


def _newer_half(series: Sequence[float] | None) -> list[float] | None:
    return None if series is None else list(series)[len(series) // 2 :]


def error_screen(message: str, settings: ScreenSettings | None = None) -> Screen:
    """Shown instead of a screen that cannot be served, so the problem is visible on the device."""
    text = "".join(ch if ch.isascii() and ch.isprintable() else "?" for ch in message)[:31]
    return Screen(
        settings or ScreenSettings(),
        [
            Text(id="err_title", r=(8, 90, 224, 24), text="SCREEN ERROR", f=0, c="#f2384c", a="c"),
            Text(id="err_msg", r=(8, 120, 224, 24), text=text, f=0, c="#8b949e", a="c"),
        ],
    )
