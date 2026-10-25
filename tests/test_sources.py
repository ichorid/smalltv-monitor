"""Tests for the AI-limits (Alloy cache) and Slack tray-icon sources."""

from __future__ import annotations

import json
import os
import struct
import tempfile
import time
import unittest
import zlib
from pathlib import Path

from smalltv_monitor.agent_limits import AgentLimitsCollector
from smalltv_monitor.collectors import SourceUnavailable
from smalltv_monitor.slack import classify_badge, decode_png_rgba

CACHE = {
    "claude": {"available": False, "error": "HTTP 429", "windows": []},
    "codex": {"available": True, "error": None, "as_of": "2026-10-24T21:52:35Z", "windows": [
        {"key": "primary", "label": "5h", "used_percent": 23.0, "resets_at": "2026-10-25T01:58:27+00:00"},
        {"key": "secondary", "label": "weekly", "used_percent": 28.0, "resets_at": None}]},
    "cursor": {"available": True, "error": None, "windows": [
        {"key": "total", "label": "cycle", "used_percent": 75.8, "resets_at": "2026-10-26T16:42:07+00:00"}]},
}


class AgentLimitsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "limits.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, data: dict, age: float = 0) -> None:
        self.cache.write_text(json.dumps(data))
        stamp = time.time() - age
        os.utime(self.cache, (stamp, stamp))

    def test_reads_alloy_cache(self) -> None:
        self.write(CACHE)
        out = AgentLimitsCollector(30, self.cache, None).collect()["harnesses"]
        self.assertEqual(out["codex"]["windows"]["5h"]["used"], 23.0)
        self.assertEqual(out["codex"]["windows"]["weekly"]["used"], 28.0)
        self.assertEqual(out["cursor"]["windows"]["cycle"]["resets_at"], "2026-10-26T16:42:07+00:00")
        self.assertEqual(out["claude"], {"windows": {}, "as_of": None, "stale": False, "error": "HTTP 429",
                                         "status": None})

    def test_keeps_last_good_windows_as_stale(self) -> None:
        good = {**CACHE, "claude": {"available": True, "windows": [
            {"label": "5h", "used_percent": 64.0}, {"label": "weekly", "used_percent": 91.0}]}}
        collector = AgentLimitsCollector(30, self.cache, None)
        self.write(good)
        self.assertFalse(collector.collect()["harnesses"]["claude"]["stale"])
        self.write(CACHE)  # Claude now answers 429
        claude = collector.collect()["harnesses"]["claude"]
        self.assertTrue(claude["stale"])
        self.assertEqual(claude["error"], "HTTP 429")
        self.assertEqual(claude["windows"]["weekly"]["used"], 91.0)

    def test_missing_cache(self) -> None:
        with self.assertRaises(SourceUnavailable):
            AgentLimitsCollector(30, self.cache, None).collect()

    def test_runs_refresh_command_only_when_stale(self) -> None:
        marker = Path(self.tmp.name) / "ran"
        cmd = ["sh", "-c", f"echo x >> {marker}"]
        self.write(CACHE, age=10)
        AgentLimitsCollector(30, self.cache, cmd, refresh_after=300).collect()
        self.assertFalse(marker.exists())
        self.write(CACHE, age=600)
        collector = AgentLimitsCollector(30, self.cache, cmd, refresh_after=300)
        collector.collect()
        collector.collect()  # rate-limited: at most once per refresh_after
        self.assertEqual(marker.read_text().count("x"), 1)


def make_png(width: int, height: int, pixels: list[tuple[int, int, int, int]]) -> bytes:
    rows = b"".join(b"\x00" + bytes(c for px in pixels[y * width:(y + 1) * width] for c in px) for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def icon(badge: tuple[int, int, int] | None) -> list[tuple[int, int, int, int]]:
    """22x22 white glyph with an optional 6x6 badge in the lower right corner."""
    px = []
    for y in range(22):
        for x in range(22):
            if badge and x >= 15 and y >= 15:
                px.append((*badge, 255))
            elif 4 <= x <= 12 and 4 <= y <= 12:
                px.append((255, 255, 255, 255))
            else:
                px.append((0, 0, 0, 0))
    return px


class SlackTest(unittest.TestCase):
    def test_decode_roundtrip(self) -> None:
        pixels = icon((224, 30, 90))
        width, height, decoded = decode_png_rgba(make_png(22, 22, pixels))
        self.assertEqual((width, height), (22, 22))
        self.assertEqual(decoded, pixels)

    def test_classify(self) -> None:
        self.assertEqual(classify_badge(icon(None)), "none")
        self.assertEqual(classify_badge(icon((0, 216, 248))), "unread")  # Slack's blue dot
        self.assertEqual(classify_badge(icon((224, 30, 90))), "mention")  # Slack's red dot

    def test_rejects_unsupported(self) -> None:
        with self.assertRaises(ValueError):
            decode_png_rgba(b"GIF89a")

    @unittest.skipUnless(__import__("importlib").util.find_spec("PIL"), "Pillow not installed (dev dependency)")
    def test_matches_pillow_for_all_filters(self) -> None:
        import io

        from PIL import Image

        image = Image.new("RGBA", (22, 22))
        image.putdata([((x * 11) % 256, (y * 7) % 256, (x * y) % 256, 255) for y in range(22) for x in range(22)])
        buf = io.BytesIO()
        image.save(buf, "PNG", optimize=True)  # Pillow picks per-row filters
        self.assertEqual(decode_png_rgba(buf.getvalue())[2], list(image.get_flattened_data()))


if __name__ == "__main__":
    unittest.main()
