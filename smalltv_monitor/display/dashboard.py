"""The default workstation screen: what to show, where, and in which colour.

All presentation decisions (thresholds, colours, units) live here; the device just draws the result.

Layout (240x240), [x] = 16 px icon:
    clock                         [slack] [agent]
                 5H                WEEK
    [claude] [bar] 36%      [bar]   9%    <- remaining allowance: green >= 50 %, yellow >= 25 %, red
    [codex]  [bar] 77%      [bar]  72%
    [cursor] [bar] 24%     3-2 5-6 2      <- Cursor: billing cycle only; right: time to reset, all rows
    [cpu]    ~~~~~ sparkline ~~~~~  41%   <- deep red (bottom) to yellow (top)
    [mem]    [used|shared|buf|cache] 29/62G   <- htop's memory meter
    [disk]   /       9.4G    /home     99G    <- yellow < 10 % free, red < 5 %
    [gpu]       250/300W    VRAM     9.6G
    (bottom 28 px free; [agent] = AGENT_SLOT, reserved for the agent-input icon)
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ..store import SnapshotStore
from .history import History
from .icons import ICONS
from .policy import NightSchedule
from .screen import Screen
from .widgets import Bar, Icon, ScreenSettings, Spark, Text, ValueText, Widget

BG = "#000000"  # pure black: on this panel even #101418 reads as grey
TRACK = "#30363d"  # bar background: clearly lighter than BG so a bar's full length is visible
FAINT = "#1c2128"
WHITE = "#ffffff"
LIGHT = "#c9d1d9"
MUTED = "#8b949e"
GREEN = "#3fb950"
YELLOW = "#f2cc38"
RED = "#f2384c"
ORANGE = "#fd8c00"
ORANGE_DIM = "#5c3a10"
BLUE = "#3b82f6"
MAGENTA = "#d946ef"
CLAUDE = "#d97757"
# Cold -> hot scale for CPU load (number colour and sparkline height gradient); the teal stop keeps
# blue -> yellow from passing through grey
HEAT = ((0, "#3b82f6"), (25, "#14b8a6"), (50, "#f2cc38"), (75, "#fd8c00"), (100, "#f2384c"))
# CPU sparkline height gradient: deep red at the bottom to yellow at the top
SPARK_GRAD = ((0, "#8b0000"), (100, YELLOW))

SPARK_SAMPLES = 120
ICON_X = 8
X0 = 30  # content starts right of the 18 px icon column
COL1_X, COL2_X = X0, 134  # the two limit columns: bar (56 px) + value (40 px) each
AGENT_SLOT = (214, 2, 18, 18)  # reserved for the "agent requires input" icon (not wired up yet)

LIMIT_ROWS = (("claude", "claude", CLAUDE), ("codex", "codex", WHITE), ("cursor", "cursor", WHITE))


def _ascii(text: str, limit: int = 31) -> str:
    return "".join(ch for ch in text if 0x20 <= ord(ch) < 0x7F)[:limit]


def _usage_colour(percent: float) -> str:
    return RED if percent >= 90 else YELLOW if percent >= 70 else GREEN


def gradient_colour(stops: tuple[tuple[float, str], ...], value: float) -> str:
    """Linear RGB interpolation between colour stops, clamped to the end colours (as the device does)."""
    if value <= stops[0][0]:
        return stops[0][1]
    for (v0, c0), (v1, c1) in zip(stops, stops[1:]):
        if value <= v1:
            t = (value - v0) / (v1 - v0) if v1 > v0 else 1.0
            a, b = (int(c0[i : i + 2], 16) for i in (1, 3, 5)), (int(c1[i : i + 2], 16) for i in (1, 3, 5))
            return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))
    return stops[-1][1]


def _remaining_colour(remaining: float) -> str:
    return RED if remaining < 25 else YELLOW if remaining < 50 else GREEN


def _icon(wid: str, name: str, y: int, colour: str = LIGHT, height: int = 18) -> Widget:
    return Icon(id=wid, r=(ICON_X, y + (height - 18) // 2, 18, 18), v=name, c=colour)


def _free_colour(free: float, total: float) -> str:
    share = free / total if total else 0
    return RED if share < 0.05 else YELLOW if share < 0.10 else WHITE


def _gib(value: float) -> str:
    return f"{value:.1f}" if value < 10 else f"{value:.0f}"


def _until(window: Mapping[str, Any] | None, now: datetime, unit_seconds: int) -> str:
    """Whole units until the window resets, rounded up ("resets within N"); "?" when unknown."""
    resets_at = (window or {}).get("resets_at")
    if not resets_at:
        return "?"
    try:
        when = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    seconds = (when - now.astimezone(UTC)).total_seconds()
    return str(max(0, math.ceil(seconds / unit_seconds)))


def _reset_summary(harnesses: Mapping[str, Any], now: datetime) -> str:
    """ "<5h hours>-<weekly days>" for Claude and Codex, then Cursor's days to cycle reset, e.g. "3-2 5-6 2"."""
    parts = []
    for harness, _, _ in LIMIT_ROWS:
        windows = (harnesses.get(harness) or {}).get("windows") or {}
        if harness == "cursor":
            parts.append(_until(windows.get("cycle"), now, 86400))
        else:
            parts.append(f"{_until(windows.get('5h'), now, 3600)}-{_until(windows.get('weekly'), now, 86400)}")
    return " ".join(parts)


class DashboardComposer:
    def __init__(self, store: SnapshotStore, history: History, schedule: NightSchedule, title: str | None = None):
        self._store = store
        self._history = history
        self._schedule = schedule
        self._title = title  # unused by this layout; kept for alternative screens

    def fresh(self) -> dict[str, Mapping[str, Any]]:
        _, readings = self._store.snapshot()
        now = time.monotonic()
        return {name: r.data for name, r in readings.items() if r.data is not None and r.is_fresh(now)}

    def compose(self, now: datetime | None = None) -> Screen:
        now = now or datetime.now().astimezone()
        data = self.fresh()
        widgets: list[Widget] = [
            ValueText(id="clock", r=(8, 2, 60, 18), v=now.strftime("%H:%M"), c=MUTED),
            self._slack(data.get("slack")),
            *self._limits(data.get("limits"), now),
            *self._cpu(data.get("cpu")),
            *self._memory(data.get("memory")),
            *self._disks(data.get("disk")),
            *self._gpu(data.get("gpu")),
        ]
        return Screen(ScreenSettings(bg=BG, bri=self._schedule.brightness(now)), widgets)

    # --- sections -------------------------------------------------------------------------------

    @staticmethod
    def _slack(slack: Mapping[str, Any] | None) -> Widget:
        state = (slack or {}).get("state")
        variant = {"none": "slack-rest", "unread": "slack-unread", "mention": "slack-mention"}.get(state or "")
        rect = (192, 2, 18, 18)
        if variant in ICONS:  # Slack's own tray image, with its blue / red dot
            return Icon(id="slack", r=rect, v=variant)
        return Icon(id="slack", r=rect, v="slack", c=FAINT if state is None else MUTED)

    @staticmethod
    def _limit_cells(prefix: str, y: int, window: Mapping[str, Any] | None, x: int) -> list[Widget]:
        """Bar + value showing the *remaining* allowance (100 % = nothing used yet)."""
        left = None if window is None else max(0.0, 100.0 - window["used"])
        # Stale (last good reading, e.g. while the source answers 429) is still coloured: it is the best estimate
        colour = MUTED if left is None else _remaining_colour(left)
        return [
            Bar(id=f"{prefix}b", r=(x, y + 5, 56, 8), track=TRACK, v=None if left is None else (left, colour)),
            ValueText(id=f"{prefix}v", r=(x + 58, y, 40, 18), a="r",
                      v="--" if left is None else (f"{left:.0f}%", colour)),
        ]

    def _limits(self, limits: Mapping[str, Any] | None, now: datetime) -> list[Widget]:
        harnesses = (limits or {}).get("harnesses") or {}
        out: list[Widget] = [
            # Headers centred over their bars (56 px), not over bar + value
            Text(id="h5", r=(COL1_X, 22, 56, 16), text="5H", c=MUTED, a="c"),
            Text(id="hwk", r=(COL2_X, 22, 56, 16), text="WEEK", c=MUTED, a="c"),
        ]
        for row, (harness, icon, colour) in enumerate(LIMIT_ROWS):
            y = 40 + 20 * row
            h = harnesses.get(harness) or {}
            windows = h.get("windows") or {}
            out.append(_icon(f"i{harness}", icon, y, colour))
            if harness == "cursor":  # one billing-cycle window instead of 5h/weekly
                cycle = windows.get("cycle")
                out += self._limit_cells("cur", y, cycle, COL1_X)
                # Time to reset for every row, in row order: "3-2 5-6 2" (5h hours-weekly days, Cursor cycle days)
                out.append(Text(id="curr", r=(COL2_X, y, 98, 18), text=_reset_summary(harnesses, now), c=MUTED,
                                a="r"))
            else:
                key = harness[:3]
                out += self._limit_cells(f"{key}5", y, windows.get("5h"), COL1_X)
                out += self._limit_cells(f"{key}w", y, windows.get("weekly"), COL2_X)
        return out

    def _cpu(self, cpu: Mapping[str, Any] | None) -> list[Widget]:
        return [
            _icon("icpu", "cpu", 104, height=34),
            # Solid heat area, no outline: every pixel's colour is set by its height (fa=100 makes the
            # edge pixels the same colour as their row, so no line is visible)
            Spark(id="cpuh", r=(X0, 104, 160, 34), grad=SPARK_GRAD, fa=100, max=100,
                  v=self._history.get("cpu", SPARK_SAMPLES) or None),
            ValueText(id="cpuv", r=(192, 104, 40, 34), a="r",
                      v=None if cpu is None else (f"{cpu['percent']:.0f}%", gradient_colour(HEAT, cpu["percent"]))),
        ]

    @staticmethod
    def _memory(mem: Mapping[str, Any] | None) -> list[Widget]:
        htop = (mem or {}).get("htop")
        return [
            _icon("imem", "mem", 142),
            Bar(id="mem", r=(X0, 144, 148, 14), track=TRACK,  # straight: segmented bars are always square
                max=mem["total_gib"] if mem else 100,
                v=None if htop is None else [
                    (htop["used_gib"], GREEN), (htop["shared_gib"], MAGENTA),
                    (htop["buffers_gib"], BLUE), (htop["cache_gib"], YELLOW),
                ]),
            ValueText(id="memv", r=(180, 142, 52, 18), a="r", c=WHITE,
                      v=None if htop is None else f"{_gib(htop['used_gib'])}/{_gib(mem['total_gib'])}G"),
        ]

    @staticmethod
    def _disks(disk: Mapping[str, Any] | None) -> list[Widget]:
        mounts = {m["path"]: m for m in (disk or {}).get("mounts") or []}
        out: list[Widget] = [_icon("idisk", "disk", 166)]
        # Each 98 px cell: mount point left-aligned, free space right-aligned ("984G" is ~38 px)
        for key, path, x in (("root", "/", COL1_X), ("home", "/home", COL2_X)):
            m = mounts.get(path)
            value = "--" if m is None else (f"{_gib(m['free_gib'])}G", _free_colour(m["free_gib"], m["total_gib"]))
            out.append(ValueText(id=key, r=(x, 166, 98, 18), label=path, lc=MUTED, c=WHITE, v=value))
        return out

    @staticmethod
    def _gpu(gpu: Mapping[str, Any] | None) -> list[Widget]:
        power: Any = "--"
        vram: Any = "--"
        if gpu is not None:
            limit, watts = gpu.get("power_limit_w"), gpu.get("power_w")
            if watts is not None:
                colour = WHITE if not limit else _usage_colour(100 * watts / limit)
                power = (f"{watts:.0f}/{limit:.0f}W" if limit else f"{watts:.0f}W", colour)
            if gpu.get("mem_total_mib"):
                pct = 100 * gpu["mem_used_mib"] / gpu["mem_total_mib"]
                vram = (f"{_gib(gpu['mem_used_mib'] / 1024)}G", WHITE if pct < 90 else RED)  # total is fixed
        return [
            _icon("igpu", "gpu", 188),
            ValueText(id="gpow", r=(COL1_X, 188, 98, 18), a="r", c=WHITE, v=power),  # "999/999W" ~72 px
            ValueText(id="gvram", r=(COL2_X, 188, 98, 18), label="VRAM", lc=MUTED, c=WHITE, v=vram),
        ]
