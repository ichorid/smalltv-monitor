"""Slack's notification state, read from its tray icon (no Slack API, no network).

Slack (Electron) publishes its tray icon as a StatusNotifierItem on the session bus; Chromium writes
the current image to `<IconThemePath>/<IconName>.png`. Slack's three tray variants (resources
slack-taskbar-{rest,unread,highlight}) differ only in the badge: none, blue dot (unread), red dot
(mention / DM). The red dot is what the display reports.

Discovery uses `gdbus` (glib, preinstalled on Ubuntu) every `discover_every` seconds; each poll only
re-reads the icon file when it changed. PNG decoding is a small stdlib implementation to keep the
service's runtime dependencies at psutil + nvidia-ml-py.
"""

from __future__ import annotations

import logging
import re
import struct
import subprocess
import time
import zlib
from pathlib import Path
from typing import Any

from .collectors import Collector, SourceUnavailable

log = logging.getLogger(__name__)

_GDBUS_TIMEOUT = 3.0


def decode_png_rgba(data: bytes) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    """(width, height, pixels) for 8-bit RGB/RGBA non-interlaced PNGs, which is what Chromium writes."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, width = 8, bytearray(), 0
    height = depth = colour_type = interlace = 0
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, colour_type, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    if depth != 8 or colour_type not in (2, 6) or interlace:
        raise ValueError(f"unsupported PNG (depth {depth}, colour type {colour_type}, interlace {interlace})")
    bpp = 4 if colour_type == 6 else 3
    raw = zlib.decompress(bytes(idat))
    stride = width * bpp
    prev = bytearray(stride)
    pixels: list[tuple[int, int, int, int]] = []
    for y in range(height):
        filt = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if filt == 1:
                line[i] = (line[i] + a) & 0xFF
            elif filt == 2:
                line[i] = (line[i] + b) & 0xFF
            elif filt == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
            elif filt == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        prev = line
        for x in range(width):
            px = line[x * bpp : x * bpp + bpp]
            pixels.append((px[0], px[1], px[2], px[3] if bpp == 4 else 255))
    return width, height, pixels


def classify_badge(pixels: list[tuple[int, int, int, int]], min_pixels: int = 4) -> str:
    """"mention" (red/pink dot), "unread" (blue dot) or "none", by counting badge-coloured pixels."""
    red = blue = 0
    for r, g, b, a in pixels:
        if a < 128:
            continue
        if r > 170 and g < 110 and r - g > 100:
            red += 1
        elif b > 180 and r < 90 and g > 120:
            blue += 1
    if red >= min_pixels:
        return "mention"
    if blue >= min_pixels:
        return "unread"
    return "none"


def _gdbus(*args: str) -> str:
    out = subprocess.run(["gdbus", "call", "--session", *args], capture_output=True, text=True,
                         timeout=_GDBUS_TIMEOUT, check=True)
    return out.stdout


def _sni_property(service: str, path: str, prop: str) -> str:
    out = _gdbus("--dest", service, "--object-path", path, "--method", "org.freedesktop.DBus.Properties.Get",
                 "org.kde.StatusNotifierItem", prop)
    match = re.search(r"<'(.*)'>", out)
    return match.group(1) if match else ""


def find_slack_icon_dir() -> Path | None:
    """IconThemePath of Slack's tray item, or None when Slack has no tray icon."""
    out = _gdbus("--dest", "org.kde.StatusNotifierWatcher", "--object-path", "/StatusNotifierWatcher", "--method",
                 "org.freedesktop.DBus.Properties.Get", "org.kde.StatusNotifierWatcher",
                 "RegisteredStatusNotifierItems")
    for item in re.findall(r"'([^']+)'", out):
        service, _, path = item.partition("@")
        try:
            if _sni_property(service, path or "/StatusNotifierItem", "Id").startswith("Slack"):
                theme = _sni_property(service, path or "/StatusNotifierItem", "IconThemePath")
                return Path(theme) if theme else None
        except (OSError, subprocess.SubprocessError):
            continue
    return None


class SlackCollector(Collector):
    name = "slack"

    def __init__(self, interval: float, discover_every: float = 60.0) -> None:
        self.interval = interval
        self.discover_every = discover_every
        self._dir: Path | None = None
        self._discovered_at = 0.0
        self._cached: tuple[str, float, str] | None = None  # (file name, mtime, state)

    def collect(self) -> dict[str, Any]:
        icon = self._current_icon()
        if icon is None:
            raise SourceUnavailable("no Slack tray icon (Slack not running?)")
        stat = icon.stat()
        if self._cached is None or self._cached[:2] != (icon.name, stat.st_mtime):
            _, _, pixels = decode_png_rgba(icon.read_bytes())
            self._cached = (icon.name, stat.st_mtime, classify_badge(pixels))
        return {"state": self._cached[2], "icon": icon.name}

    def _current_icon(self) -> Path | None:
        now = time.monotonic()
        if self._dir is None or not self._dir.is_dir() or now - self._discovered_at > self.discover_every:
            self._discovered_at = now
            try:
                self._dir = find_slack_icon_dir()
            except (OSError, subprocess.SubprocessError) as exc:
                raise SourceUnavailable(f"D-Bus: {exc}") from exc
        if self._dir is None:
            return None
        icons = sorted(self._dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        return icons[0] if icons else None
