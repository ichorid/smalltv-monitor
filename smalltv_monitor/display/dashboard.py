"""The default workstation screen: what to show, where, and in which colour.

All presentation decisions (thresholds, colours, units) live here; the device just draws the result.

Layout (240x240), [x] = 16 px icon:
    clock                         [slack] [agent]
                 5H                WEEK
    [claude] [bar] 36%      [bar]   9%    <- remaining allowance: green >= 50 %, yellow >= 25 %, red
    [codex]  [bar] 77%      [bar]  72%
    [cursor] [bar] 24%     3-2 5-6 2      <- Cursor: billing cycle only; right: time to reset, all rows
    [cpu]    ~~ cpu ~~  41%  ~ i/o ~  12  <- CPU: deep red (bottom) to yellow (top); I/O: reads (green)
                                    0.4      drawn over writes (red); MB/s now: write (red) over read (green)
    [mem]    [used|shared|buf|cache] 29/62G   <- htop's memory meter
    [disk]   /       9.4G    /home     99G    <- yellow < 10 % free, red < 5 %
    [gpu]       250/300W    VRAM     9.6G
    (bottom 28 px free; [agent] = AGENT_SLOT, reserved for the agent-input icon)
"""

from __future__ import annotations

import math
import re
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
# CPU load colour, deep red at 0 % to yellow at 100 %: the sparkline's height gradient and the number
CPU_GRAD = ((0, "#8b0000"), (100, YELLOW))

# CPU row: CPU sparkline + number in the left half, disk I/O sparkline in the right half (one sample per px)
CPU_SPARK = (30, 104, 60, 34)
CPU_VALUE = (90, 104, 40, 34)
IO_SPARK = (134, 104, 60, 34)
IO_WRITE = (196, 104, 36, 17)  # current MB/s, write above read, right of the I/O sparkline
IO_READ = (196, 121, 36, 17)
IO_NUMBERS_FW = 3  # renderer firmware -w3: up to 40 widgets (the two numbers make 33)
IO_FLOOR_MB_S = 10.0  # the automatic I/O scale never drops below this, so trickles stay near the bottom
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


def _rate(mb_s: float) -> str:
    """Throughput in MB/s, at most 4 characters: 0.4, 12, 523, 2.4G (GB/s)."""
    if mb_s < 9.95:
        return f"{mb_s:.1f}"
    if mb_s < 999.5:
        return f"{mb_s:.0f}"
    return f"{mb_s / 1000:.1f}G"


def _io_level(mb_s: float, full_scale: float, log: bool) -> float:
    """Height fraction of a throughput against the full scale: log(1 + MB/s) / log(1 + full scale), or linear."""
    if log:
        return min(1.0, math.log1p(max(0.0, mb_s)) / math.log1p(full_scale))
    return min(1.0, mb_s / full_scale)


def _firmware_level(display: Mapping[str, Any] | None) -> int | None:
    """Renderer protocol level from the polling device's `fw` ("...-w3" -> 3); None if unknown."""
    match = re.search(r"-w(\d+)$", (display or {}).get("fw") or "")
    return int(match[1]) if match else None


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


def track_history(history: History) -> None:
    """Record the series this screen's sparklines draw, at the collectors' cadence."""
    history.track("cpu", "cpu", lambda d: d["percent"])
    history.track("io_read", "diskio", lambda d: d["read_mb_s"])
    history.track("io_write", "diskio", lambda d: d["write_mb_s"])


class DashboardComposer:
    def __init__(self, store: SnapshotStore, history: History, schedule: NightSchedule, title: str | None = None,
                 io_max_mb_s: tuple[float, float] = (0.0, 0.0), io_scale: str = "log"):
        self._store = store
        self._io_max_read, self._io_max_write = io_max_mb_s  # full scale per series; 0 = automatic
        self._io_log = io_scale == "log"
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
            *self._io(data.get("diskio"), _firmware_level(data.get("display"))),
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
            Spark(id="cpuh", r=CPU_SPARK, grad=CPU_GRAD, fa=100, max=100,
                  v=self._history.get("cpu", CPU_SPARK[2]) or None),
            ValueText(id="cpuv", r=CPU_VALUE, a="r",
                      v=None if cpu is None else (f"{cpu['percent']:.0f}%", gradient_colour(CPU_GRAD, cpu["percent"]))),
        ]

    def _io(self, diskio: Mapping[str, Any] | None, firmware: int | None) -> list[Widget]:
        """Disk throughput: writes as a solid red area, reads drawn over them as a solid green one; current
        MB/s to the right (write in red above read in green). The numbers are left out for a device whose
        firmware takes fewer than 33 widgets; unknown firmware (preview, tests) gets them."""
        numbers: list[Widget] = []
        if firmware is None or firmware >= IO_NUMBERS_FW:
            write = None if diskio is None else (_rate(diskio["write_mb_s"]), RED)
            read = None if diskio is None else (_rate(diskio["read_mb_s"]), GREEN)
            numbers = [ValueText(id="iow", r=IO_WRITE, a="r", v=write), ValueText(id="ior", r=IO_READ, a="r", v=read)]
        return [self._io_spark(diskio), *numbers]

    def _io_spark(self, diskio: Mapping[str, Any] | None) -> Spark:
        samples = IO_SPARK[2]
        reads, writes = self._history.get("io_read", samples), self._history.get("io_write", samples)
        if diskio is None or not reads:
            return Spark(id="io", r=IO_SPARK, v=None)
        # Each series is drawn against its configured full scale (io_max_read_mb_s / io_max_write_mb_s, e.g. the
        # disk's bandwidth); unset, against the busiest moment shown of either (at least IO_FLOOR_MB_S). On the
        # log scale (default) everyday MB/s stay visible next to GB/s bursts. Samples are sent as pixel rows
        # (0..h-1): no visible loss, and at most two digits each
        rows = IO_SPARK[3] - 1
        auto = max(IO_FLOOR_MB_S, *reads, *writes)
        read_scale, write_scale = self._io_max_read or auto, self._io_max_write or auto
        return Spark(id="io", r=IO_SPARK, max=rows, c=GREEN, fill=GREEN, uc=RED,
                     v=[rows * _io_level(r, read_scale, self._io_log) for r in reads],
                     u=[rows * _io_level(w, write_scale, self._io_log) for w in writes])

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
