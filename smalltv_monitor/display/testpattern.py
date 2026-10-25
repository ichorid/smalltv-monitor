"""Panel diagnostics (set `screen = "testpattern"` in the config): pure red, green and blue blocks."""

from __future__ import annotations

from .screen import Screen
from .widgets import Panel, ScreenSettings, Text

BLOCKS = (("RED", "#ff0000"), ("GREEN", "#00ff00"), ("BLUE", "#0000ff"))


def test_pattern() -> Screen:
    widgets = []
    for i, (name, colour) in enumerate(BLOCKS):
        y = 8 + i * 76
        widgets.append(Panel(id=f"p{i}", r=(8, y, 120, 68), c=colour, radius=0))
        widgets.append(Text(id=f"t{i}", r=(136, y, 96, 68), text=name, f=1, c="#ffffff", a="l"))
    return Screen(ScreenSettings(bg="#000000", bri=80), widgets)
