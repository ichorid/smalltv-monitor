#!/usr/bin/env python3
"""Render a /v1/display screen to PNG, following the protocol's rendering conventions.

    .venv/bin/python scripts/preview.py --demo -o /tmp/screen.png            # sample data incl. GPU
    .venv/bin/python scripts/preview.py --url http://127.0.0.1:8765/v1/display -o live.png
    .venv/bin/python scripts/preview.py --file screen.json -o screen.png --zoom 3

An approximation for design work: fonts come from the same Noto TTFs as the device's VLW fonts,
anti-aliasing from 4x supersampling. The device is the reference. Needs requirements-dev.txt (Pillow).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smalltv_monitor.display.icons import ICONS, decode_rgba  # noqa: E402

SS = 4  # supersampling factor
FONT_FILES = {
    0: ("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf", 15),
    1: ("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf", 36),
    2: ("/usr/share/fonts/truetype/noto/NotoSansMono-Bold.ttf", 20),
}
DEFAULTS = {"tc": "#ffffff", "lc": "#8b949e", "track": "#1c2128"}
_fonts: dict[int, ImageFont.FreeTypeFont] = {}


def font(index: int) -> ImageFont.FreeTypeFont:
    if index not in _fonts:
        path, size = FONT_FILES[index]
        _fonts[index] = ImageFont.truetype(path, size * SS)
    return _fonts[index]


def rgb(colour: str) -> tuple[int, int, int]:
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)


def split_value(w: dict[str, Any]) -> tuple[Any, str | None]:
    """(raw value, colour override) for scalar values."""
    v = w.get("v")
    if isinstance(v, list) and len(v) == 2 and isinstance(v[1], str) and not isinstance(v[0], list):
        return v[0], v[1]
    return v, None


def segments(w: dict[str, Any]) -> list[tuple[float, str]]:
    v = w.get("v")
    if v is None or isinstance(v, str):
        return []
    if isinstance(v, (int, float)):
        return [(v, w.get("c", "#ffffff"))]
    if len(v) == 2 and isinstance(v[1], str) and not isinstance(v[0], list):
        return [(v[0], v[1])]
    return [(amount, colour) for amount, colour in v]


def fmt(value: Any, dp: int) -> str:
    if value is None:
        return "--"
    if isinstance(value, (int, float)):
        return f"{value:.{dp}f}"
    return str(value)


def text(d: ImageDraw.ImageDraw, xy: tuple[float, float], s: str, f: int, colour: str, anchor: str) -> None:
    d.text((xy[0] * SS, xy[1] * SS), s, font=font(f), fill=rgb(colour), anchor=anchor)


def arc(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, t: int, a0: float, a1: float, colour: str) -> None:
    if a1 - a0 < 0.5:
        return
    box = [(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS]
    for start, end in ((a0, min(a1, 360)), (max(a0, 360) - 360, a1 - 360)):
        if end > start:
            d.arc(box, start, end, fill=rgb(colour), width=t * SS)


def rrect(d: ImageDraw.ImageDraw, x0: float, y0: float, x1: float, y1: float, radius: float, colour: str) -> None:
    if x1 - x0 < 0.5 or y1 - y0 < 0.5:
        return
    d.rounded_rectangle([x0 * SS, y0 * SS, x1 * SS - 1, y1 * SS - 1], radius=radius * SS, fill=rgb(colour))


def draw_ring(d: ImageDraw.ImageDraw, w: dict[str, Any], width: int, height: int) -> None:
    side = min(width, height)
    cx, cy = width / 2, height / 2
    r = side / 2 - 1
    t = max(6, round(side / 10))
    arc_r = r - t / 2  # PIL draws the stroke inside the box; centre it on the ring
    arc(d, cx, cy, arc_r + t / 2, t, 135, 405, w.get("track", DEFAULTS["track"]))
    angle, total, maximum = 135.0, 0.0, w.get("max", 100) or 100
    for amount, colour in segments(w):
        span = 270 * max(0.0, min(amount, maximum - total)) / maximum
        arc(d, cx, cy, arc_r + t / 2, t, angle, angle + span, colour)
        angle += span
        total += amount
    number = "--" if w.get("v") is None else f"{round(total):d}"
    text(d, (cx, cy), number, w.get("f", 1), w.get("tc", DEFAULTS["tc"]), "mm")
    if w.get("label"):
        text(d, (cx, cy + 0.72 * r), w["label"], 0, w.get("lc", DEFAULTS["lc"]), "mm")


def fill_extents(segs: list[tuple[float, str]], maximum: float, length: float) -> list[tuple[float, float, str]]:
    out, pos, total = [], 0.0, 0.0
    for amount, colour in segs:
        amount = max(0.0, min(amount, maximum - total))
        span = length * amount / maximum
        out.append((pos, pos + span, colour))
        pos += span
        total += amount
    return out


def draw_bar(d: ImageDraw.ImageDraw, w: dict[str, Any], width: int, height: int) -> None:
    maximum = w.get("max", 100) or 100
    track = w.get("track", DEFAULTS["track"])
    label, lc = w.get("label"), w.get("lc", DEFAULTS["lc"])
    radius = w.get("radius", 0)
    segs = segments(w)
    fill_radius = radius if len(segs) == 1 else 0  # two or more segments are always square
    if w.get("dir", "h") == "h":
        x0 = 48 if label else 0
        if label:
            text(d, (0, height / 2), label, 0, lc, "lm")
        rrect(d, x0, 0, width, height, radius, track)
        for start, end, colour in fill_extents(segs, maximum, width - x0):
            rrect(d, x0 + start, 0, x0 + end, height, fill_radius, colour)
    else:
        y1 = height - 16 if label else height
        if label:
            text(d, (width / 2, height - 8), label, 0, lc, "mm")
        rrect(d, 0, 0, width, y1, radius, track)
        for start, end, colour in fill_extents(segs, maximum, y1):
            rrect(d, 0, y1 - end, width, y1 - start, fill_radius, colour)


def draw_bars(d: ImageDraw.ImageDraw, w: dict[str, Any], width: int, height: int) -> None:
    values = w.get("v") or []
    if not values:
        return
    gap, maximum = w.get("gap", 1), w.get("max", 100) or 100
    track, colour = w.get("track", DEFAULTS["track"]), w.get("c", "#ffffff")
    vertical = w.get("dir", "v") == "v"
    extent = width if vertical else height
    thick = (extent - gap * (len(values) - 1)) // len(values)
    for i, value in enumerate(values):
        pos = i * (thick + gap)
        segs = segments({"v": value, "c": colour})
        if vertical:
            rrect(d, pos, 0, pos + thick, height, 0, track)
            for start, end, c in fill_extents(segs, maximum, height):
                rrect(d, pos, height - end, pos + thick, height - start, 0, c)
        else:
            rrect(d, 0, pos, width, pos + thick, 0, track)
            for start, end, c in fill_extents(segs, maximum, width):
                rrect(d, start, pos, end, pos + thick, 0, c)


def draw_spark(d: ImageDraw.ImageDraw, w: dict[str, Any], width: int, height: int) -> None:
    samples = w.get("v") or []
    if len(samples) < 2:
        return
    lo, hi = w.get("min", 0), w.get("max", max(samples))  # device default: largest sample
    span = (hi - lo) or 1
    step = (width - 1) / (len(samples) - 1)
    pts = [(i * step * SS, (height - (max(lo, min(hi, s)) - lo) / span * (height - 1)) * SS) for i, s in enumerate(samples)]
    area = [(0, height * SS), *pts, ((width - 1) * SS, height * SS)]
    if not w.get("grad"):
        if w.get("fill"):
            d.polygon(area, fill=rgb(w["fill"]))
        d.line(pts, fill=rgb(w.get("c", "#ffffff")), width=2 * SS, joint="curve")
        return
    # Height gradient: every pixel row takes the colour of the value it represents (btop style)
    from smalltv_monitor.display.dashboard import gradient_colour

    stops = tuple((v, c) for v, c in w["grad"])
    rows = Image.new("RGB", (width * SS, height * SS))
    rd = ImageDraw.Draw(rows)
    for y in range(height * SS):
        value = lo + (height - y / SS) / (height - 1) * span if height > 1 else hi
        rd.line([(0, y), (width * SS, y)], fill=rgb(gradient_colour(stops, value)))
    fill_mask, line_mask = Image.new("L", rows.size), Image.new("L", rows.size)
    ImageDraw.Draw(fill_mask).polygon(area, fill=round(255 * w.get("fa", 35) / 100))
    ImageDraw.Draw(line_mask).line(pts, fill=255, width=2 * SS, joint="curve")
    d._image.paste(rows, (0, 0), fill_mask)
    d._image.paste(rows, (0, 0), line_mask)


def draw_text_like(d: ImageDraw.ImageDraw, w: dict[str, Any], width: int, height: int) -> None:
    if w["t"] == "text":
        s, colour = w.get("v") or w.get("text", ""), w.get("c", "#ffffff")
    else:
        raw, override = split_value(w)
        s, colour = fmt(raw, w.get("dp", 0)) + ("" if raw is None else w.get("unit", "")), override or w.get("c", "#ffffff")
    align = "r" if w.get("label") else w.get("a", "l")
    if w.get("label"):
        text(d, (0, height / 2), w["label"], w.get("f", 0), w.get("lc", DEFAULTS["lc"]), "lm")
        s_full = f'{w["label"]}  {s}'
    else:
        s_full = s
    needed = font(w.get("f", 0)).getlength(s_full) / SS
    if needed > width:
        print(f"warning: {w['id']}: {s_full!r} needs {needed:.0f} px, rect is {width} px", file=sys.stderr)
    x = {"l": 0, "c": width / 2, "r": width}[align]
    text(d, (x, height / 2), s, w.get("f", 0), colour, {"l": "lm", "c": "mm", "r": "rm"}[align])


def draw_icon(d: ImageDraw.ImageDraw, w: dict[str, Any], width: int, height: int) -> None:
    name, colour = w.get("v"), w.get("c", "#ffffff")
    if name in ICONS:  # PC-served bitmap, centred and unscaled (at SS x for the supersampled layer)
        iw, ih, pixels = decode_rgba(ICONS[name], rgb(colour))
        img = Image.new("RGBA", (iw, ih))
        img.putdata(pixels)
        img = img.resize((iw * SS, ih * SS), Image.NEAREST)
        d._image.paste(img, (int((width - iw) / 2 * SS), int((height - ih) / 2 * SS)), img)
        return
    side, fill = min(width, height), rgb(colour)
    cx, cy, r = width / 2 * SS, height / 2 * SS, side / 2 * SS * 0.8
    if name == "dot":
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)
    elif name == "warn":
        d.polygon([(cx, cy - r), (cx + r, cy + r), (cx - r, cy + r)], fill=fill)
    elif name == "check":
        d.line([(cx - r, cy), (cx - r / 3, cy + r * 0.7), (cx + r, cy - r * 0.7)], fill=fill, width=max(2, side // 6) * SS)


def draw_panel(d: ImageDraw.ImageDraw, w: dict[str, Any], width: int, height: int) -> None:
    rrect(d, 0, 0, width, height, w.get("radius", 6), w.get("c", "#1c2128"))


DRAW = {"ring": draw_ring, "bar": draw_bar, "bars": draw_bars, "spark": draw_spark, "text": draw_text_like,
        "value": draw_text_like, "icon": draw_icon, "panel": draw_panel}


def render(doc: dict[str, Any]) -> Image.Image:
    screen_bg = doc.get("screen", {}).get("bg", "#000000")
    out = Image.new("RGB", (240, 240), rgb(screen_bg))
    for w in doc.get("w", []):
        x, y, width, height = w["r"]
        layer = Image.new("RGB", (width * SS, height * SS), rgb(w.get("bg", screen_bg)))
        DRAW[w["t"]](ImageDraw.Draw(layer), w, width, height)  # each widget is clipped to its rect
        out.paste(layer.resize((width, height), Image.LANCZOS), (x, y))
    return out


def demo_screen() -> dict[str, Any]:
    """The default dashboard composed from fixed sample data (GPU included)."""
    from smalltv_monitor.display.dashboard import DashboardComposer
    from smalltv_monitor.display.history import History
    from smalltv_monitor.display.policy import NightSchedule
    from smalltv_monitor.store import SnapshotStore

    store, history = SnapshotStore(), History()
    history.track("cpu", "cpu", lambda d: d["percent"])
    store.add_listener(history.on_sample)
    for i in range(60):
        store.put("cpu", {"percent": 30 + 25 * math.sin(i / 7) ** 2, "user": 0, "system": 0, "per_core": []}, 60)
    store.put("cpu", {"percent": 41.0, "user": 33.5, "system": 7.5, "count": 16, "load1": 4.2,
                      "per_core": [12, 35, 96, 8, 41, 22, 67, 5, 30, 18, 91, 14, 52, 9, 26, 44]}, 60)
    store.put("memory", {"used_gib": 28.6, "cache_gib": 22.3, "total_gib": 61.6, "percent": 46.4,
                         "htop": {"used_gib": 28.6, "shared_gib": 1.2, "buffers_gib": 2.3, "cache_gib": 18.8}}, 60)
    store.put("gpu", {"util_pct": 72, "mem_used_mib": 9800, "mem_total_mib": 16384, "temp_c": 71,
                      "power_w": 250.0, "power_limit_w": 300.0, "name": "NVIDIA GPU"}, 60)
    store.put("disk", {"mounts": [
        {"path": "/", "used_gib": 88.4, "free_gib": 9.4, "total_gib": 97.9, "percent": 90.4},
        {"path": "/home", "used_gib": 934.0, "free_gib": 49.0, "total_gib": 983.2, "percent": 95.0}]}, 60)
    store.put("limits", {"cache_age_s": 20, "harnesses": {
        "claude": {"windows": {"5h": {"used": 64.0, "resets_at": "2026-10-24T15:10:00+02:00"},
                               "weekly": {"used": 91.0, "resets_at": "2026-10-27T09:00:00+01:00"}},
                   "stale": True, "error": "HTTP 429"},
        "codex": {"windows": {"5h": {"used": 23.0, "resets_at": "2026-10-24T18:20:00+02:00"},
                              "weekly": {"used": 74.0, "resets_at": "2026-10-30T18:00:00+01:00"}},
                  "stale": False, "error": None},
        "cursor": {"windows": {"cycle": {"used": 75.8, "resets_at": "2026-10-26T16:42:07+00:00"}},
                   "stale": False, "error": None}}}, 60)
    store.put("slack", {"state": "mention", "icon": "status_icon_1.png"}, 60)
    composer = DashboardComposer(store, history, NightSchedule(), title="workstation")
    return composer.compose(datetime(2026, 10, 24, 14, 30).astimezone()).to_json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url")
    source.add_argument("--file", type=Path)
    source.add_argument("--demo", action="store_true")
    parser.add_argument("-o", "--out", type=Path, required=True)
    parser.add_argument("--zoom", type=int, default=2, help="nearest-neighbour upscale for viewing (default 2)")
    args = parser.parse_args()

    if args.url:
        with urllib.request.urlopen(args.url, timeout=5) as resp:
            raw = resp.read()
    elif args.file:
        raw = args.file.read_bytes()
    else:
        raw = json.dumps(demo_screen(), separators=(",", ":"), ensure_ascii=False).encode()
    doc = json.loads(raw)
    image = render(doc)
    if args.zoom > 1:
        image = image.resize((240 * args.zoom, 240 * args.zoom), Image.NEAREST)
    image.save(args.out)
    print(f"{args.out}: {len(doc.get('w', []))} widgets, {len(raw)} bytes JSON, brightness {doc['screen'].get('bri')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
