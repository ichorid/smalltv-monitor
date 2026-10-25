"""Tests for the /v1/display side: widget model, screen limits, composer, policy, endpoint."""

from __future__ import annotations

import json
import time
import unittest
from datetime import UTC, datetime, time as dtime

from smalltv_monitor.display.dashboard import DashboardComposer, track_history
from smalltv_monitor.display.history import History
from smalltv_monitor.display.icons import ICON_HASH, ICONS, decode_rgba, fnv1a32
from smalltv_monitor.display.policy import NightSchedule, parse_hhmm
from smalltv_monitor.display.screen import MAX_BYTES, Screen, error_screen
from smalltv_monitor.display.widgets import (
    Bar,
    Bars,
    Icon,
    LayoutError,
    Ring,
    ScreenSettings,
    Spark,
    Text,
    ValueText,
)
from smalltv_monitor.server import Request
from smalltv_monitor.store import SnapshotStore
from smalltv_monitor.views import DisplayView, IconView

CPU = {"percent": 41.0, "user": 33.5, "system": 7.5, "count": 16, "load1": 1.0, "per_core": [10.0] * 16}
MEM = {"used_gib": 26.8, "cache_gib": 40.0, "total_gib": 61.6, "percent": 43.5,
       "htop": {"used_gib": 28.6, "shared_gib": 1.2, "buffers_gib": 2.3, "cache_gib": 18.8}}
GPU = {"util_pct": 72, "mem_used_mib": 15340.0, "mem_total_mib": 16384.0, "temp_c": 85, "power_w": 250.0,
       "power_limit_w": 300.0}
DISK = {"mounts": [{"path": "/", "used_gib": 88.0, "free_gib": 9.4, "total_gib": 97.9, "percent": 90.0},
                   {"path": "/home", "used_gib": 940.0, "free_gib": 40.0, "total_gib": 983.0, "percent": 96.0}]}
LIMITS = {"cache_age_s": 20, "harnesses": {
    "claude": {"windows": {"5h": {"used": 64.0, "resets_at": "2026-01-01T14:10:00+00:00"},
                           "weekly": {"used": 91.0, "resets_at": "2026-01-06T09:00:00Z"}},
               "stale": True, "error": "HTTP 429"},
    "codex": {"windows": {"5h": {"used": 23.0, "resets_at": "2026-01-01T12:30:00+00:00"}, "weekly": {"used": 74.0}},
              "stale": False, "error": None},
    "cursor": {"windows": {"cycle": {"used": 75.8, "resets_at": "2026-01-03T12:00:00+00:00"}}, "stale": False,
               "error": None}}}


def screen_of(*widgets) -> Screen:
    return Screen(ScreenSettings(), list(widgets))


class WidgetTest(unittest.TestCase):
    def test_serialises_protocol_fields(self) -> None:
        ring = Ring(id="cpu", r=(0, 0, 92, 92), label="CPU", track="#1c2128", v=[(33.456, "#fd8c00"), (7.5, "#f2384c")])
        self.assertEqual(ring.to_json(), {"id": "cpu", "t": "ring", "r": [0, 0, 92, 92],
                                          "v": [[33.5, "#fd8c00"], [7.5, "#f2384c"]], "max": 100, "f": 1,
                                          "track": "#1c2128", "label": "CPU"})
        self.assertEqual(ValueText(id="p", r=(0, 0, 50, 20), v=(212.4, "#f2384c"), unit=" W").to_json()["v"], [212, "#f2384c"])
        self.assertNotIn("dir", Bar(id="b", r=(0, 0, 10, 10), v=5).to_json())  # defaults omitted
        self.assertIsNone(Ring(id="g", r=(0, 0, 50, 50), v=None).to_json()["v"])  # explicit null = no data

    def test_rejects_invalid_widgets(self) -> None:
        cases = [
            Text(id="t", r=(200, 0, 50, 10), text="x"),  # off screen
            Text(id="t", r=(0, 0, 10, 10), text="x", c="red"),  # colour format
            Text(id="t", r=(0, 0, 10, 10), text="x" * 32),  # too long
            ValueText(id="v", r=(0, 0, 10, 10), v=21, unit="°C", f=1),  # no ° in the 36 px font
            Bar(id="b", r=(0, 0, 10, 10), v=[(1, "#ffffff")] * 5),  # too many segments
            Bars(id="b", r=(0, 0, 10, 10), v=[1] * 33),
            Spark(id="s", r=(0, 0, 10, 10), v=[1] * 121),
        ]
        for widget in cases:
            with self.subTest(widget=widget), self.assertRaises(LayoutError):
                widget.to_json()

    def test_degree_sign_allowed_in_small_font(self) -> None:
        ValueText(id="v", r=(0, 0, 10, 10), v=21, unit="°C", f=0).to_json()


class ScreenTest(unittest.TestCase):
    def test_overlap_and_duplicates_rejected(self) -> None:
        with self.assertRaisesRegex(LayoutError, "overlap"):
            screen_of(Text(id="a", r=(0, 0, 50, 50), text="a"), Text(id="b", r=(49, 49, 10, 10), text="b")).to_json()
        with self.assertRaisesRegex(LayoutError, "duplicate"):
            screen_of(Text(id="a", r=(0, 0, 10, 10), text="a"), Text(id="a", r=(20, 0, 10, 10), text="b")).to_json()
        screen_of(Text(id="a", r=(0, 0, 50, 50), text="a"), Text(id="b", r=(50, 0, 10, 10), text="b")).to_json()  # touching is fine

    def test_fit_trims_sparklines_to_size(self) -> None:
        sparks = [Spark(id=f"s{i}", r=(0, i * 7, 240, 7), v=[100] * 120) for i in range(30)]
        body = screen_of(*sparks).fit()
        self.assertLessEqual(len(body), MAX_BYTES)
        self.assertTrue(all(len(w["v"]) <= 120 for w in json.loads(body)["w"]))

    def test_fit_trims_both_series_of_a_spark(self) -> None:
        sparks = [Spark(id=f"s{i}", r=(0, i * 30, 240, 30), v=[100] * 120, u=[50] * 120) for i in range(8)]
        body = screen_of(*sparks).fit()
        self.assertLessEqual(len(body), MAX_BYTES)
        widgets = json.loads(body)["w"]
        self.assertTrue(any(len(w["u"]) < 120 for w in widgets))
        self.assertTrue(all(len(w["u"]) == len(w["v"]) for w in widgets))  # trimmed together, newest kept

    def test_fit_gives_up_when_nothing_to_trim(self) -> None:
        texts = [Text(id=f"t{i:02d}", r=(0, i * 7, 240, 7), text="x" * 31, tc="#ffffff", lc="#ffffff", bg="#000000")
                 for i in range(32)]
        with self.assertRaisesRegex(LayoutError, "bytes"):
            screen_of(*texts).fit(limit=1000)

    def test_error_screen_is_valid(self) -> None:
        body = error_screen("überlong message ° with non-ascii " * 3).fit()
        self.assertLess(len(body), 600)


class PolicyTest(unittest.TestCase):
    def test_night_spanning_midnight(self) -> None:
        s = NightSchedule(day_bri=80, night_bri=10, night_start=dtime(22), night_end=dtime(7))
        self.assertEqual(s.brightness(datetime(2026, 1, 1, 23, 30)), 10)
        self.assertEqual(s.brightness(datetime(2026, 1, 1, 6, 59)), 10)
        self.assertEqual(s.brightness(datetime(2026, 1, 1, 7, 0)), 80)
        self.assertEqual(s.brightness(datetime(2026, 1, 1, 21, 59)), 80)

    def test_same_day_window(self) -> None:
        s = NightSchedule(night_start=dtime(13), night_end=dtime(14))
        self.assertTrue(s.is_night(datetime(2026, 1, 1, 13, 30)))
        self.assertFalse(s.is_night(datetime(2026, 1, 1, 14, 0)))

    def test_parse(self) -> None:
        self.assertEqual(parse_hhmm("07:30"), dtime(7, 30))


class ComposerTest(unittest.TestCase):
    NOON = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def setUp(self) -> None:
        self.store, self.history = SnapshotStore(), History()
        track_history(self.history)
        self.store.add_listener(self.history.on_sample)
        self.composer = DashboardComposer(self.store, self.history, NightSchedule(), title="workstation")

    def widgets(self, now=NOON) -> tuple[dict, dict]:
        doc = json.loads(self.composer.compose(now).fit())
        return doc, {w["id"]: w for w in doc["w"]}

    def test_full_data(self) -> None:
        for name, data in (("cpu", CPU), ("memory", MEM), ("gpu", GPU), ("disk", DISK), ("limits", LIMITS),
                           ("slack", {"state": "mention"})):
            self.store.put(name, data, 60)
        doc, w = self.widgets()
        self.assertEqual(doc["screen"]["bri"], 80)
        self.assertEqual(w["clock"]["v"], "12:00")
        self.assertEqual(w["slack"]["v"], "slack-mention")  # Slack's own tray image with the red dot
        self.assertEqual(w["slack"]["h"], ICON_HASH["slack-mention"])
        # limits show the remaining allowance, coloured by what is left (>= 50 green, >= 25 yellow, else red),
        # stale Claude included (last good reading)
        self.assertEqual(w["cla5v"]["v"], ["36%", "#f2cc38"])
        self.assertEqual(w["cod5v"]["v"], ["77%", "#3fb950"])
        self.assertEqual(w["cod5b"]["v"], [77.0, "#3fb950"])
        self.assertEqual(w["codwv"]["v"], ["26%", "#f2cc38"])  # 25-49 % left
        self.assertEqual(w["curv"]["v"], ["24%", "#f2384c"])  # < 25 % left
        # time to reset: Claude/Codex "<5h hours>-<weekly days>", then Cursor's cycle days (rounded up)
        self.assertEqual(w["curr"]["text"], "3-5 1-? 2")  # 2h10m, 4d21h; 30m, unknown; exactly 2d
        self.assertEqual({w[i]["v"] for i in ("iclaude", "icodex", "icursor", "icpu", "imem", "idisk", "igpu")},
                         {"claude", "codex", "cursor", "cpu", "mem", "disk", "gpu"})
        # htop memory meter: used, shared, buffers, cache; straight (no radius)
        self.assertEqual([seg[1] for seg in w["mem"]["v"]], ["#3fb950", "#d946ef", "#3b82f6", "#f2cc38"])
        self.assertNotIn("radius", w["mem"])
        self.assertEqual(w["memv"]["v"], "29/62G")
        # free space: / at 9.6 % -> yellow, /home at 4.1 % -> red
        self.assertEqual((w["root"]["label"], w["root"]["v"]), ("/", ["9.4G", "#f2cc38"]))  # label left, value right
        self.assertNotIn("a", w["root"])
        self.assertEqual(w["home"]["v"], ["40G", "#f2384c"])
        self.assertEqual(w["gpow"]["a"], "r")
        self.assertEqual(w["gpow"]["v"], ["250/300W", "#f2cc38"])
        self.assertEqual(w["gvram"]["v"], ["15G", "#f2384c"])  # > 90 % used
        self.assertEqual(w["cpuh"]["v"], [41])
        self.assertEqual(w["cpuh"]["r"], [30, 104, 60, 34])  # left half of the row, with the number
        self.assertEqual(w["io"]["r"], [134, 104, 98, 34])  # right half: disk I/O
        self.assertIsNone(w["io"]["v"])  # no diskio sample yet

    def test_disk_io_sparkline(self) -> None:
        for read, write in ((0.0, 0.0), (2.0, 5.0), (40.0, 10.0)):
            self.store.put("diskio", {"read_mib_s": read, "write_mib_s": write, "disks": ["sda"]}, 60)
        io = self.widgets()[1]["io"]
        # Reads (main series, green area) drawn over writes (u, red area); both scaled so the busiest sample
        # shown (40 MiB/s) is the top pixel row (33 of a 34 px rect)
        self.assertEqual((io["c"], io["fill"], io["uc"], io["max"]), ("#3fb950", "#3fb950", "#f2384c", 33))
        self.assertEqual(io["v"], [0, 2, 33])
        self.assertEqual(io["u"], [0, 4, 8])
        self.assertNotIn("grad", io)

    def test_disk_io_scale_has_a_floor(self) -> None:
        self.store.put("diskio", {"read_mib_s": 0.5, "write_mib_s": 1.0, "disks": ["sda"]}, 60)
        io = self.widgets()[1]["io"]
        self.assertEqual((io["v"], io["u"]), ([2], [3]))  # scaled to 10 MiB/s, not to the 1 MiB/s peak

    def test_no_data_at_all(self) -> None:
        doc, w = self.widgets(datetime(2026, 1, 1, 23, 0).astimezone())  # 23:00 local
        self.assertEqual(doc["screen"]["bri"], 10)
        self.assertEqual((w["slack"]["v"], w["slack"]["c"]), ("slack", "#1c2128"))  # Slack not running: faint
        self.assertEqual(w["cla5v"]["v"], "--")
        self.assertIsNone(w["cla5b"]["v"])
        self.assertIsNone(w["cpuv"]["v"])
        self.assertIsNone(w["cpuh"]["v"])
        self.assertIsNone(w["mem"]["v"])
        self.assertEqual(w["root"]["v"], "--")
        self.assertEqual(w["gpow"]["v"], "--")

    def test_stale_source_counts_as_missing(self) -> None:
        self.store.put("gpu", GPU, 0.0)
        time.sleep(0.01)
        _, w = self.widgets()
        self.assertEqual(w["gvram"]["v"], "--")

    def test_worst_case_size(self) -> None:
        for i in range(200):
            self.store.put("cpu", CPU, 60)
        for name, data in (("memory", MEM), ("gpu", GPU), ("disk", DISK), ("limits", LIMITS)):
            self.store.put(name, data, 60)
        self.assertLessEqual(len(self.composer.compose(self.NOON).fit()), MAX_BYTES)


class DisplayViewTest(unittest.TestCase):
    def test_serves_screen_and_records_telemetry(self) -> None:
        store = SnapshotStore()
        store.put("cpu", CPU, 60)
        composer = DashboardComposer(store, History(), NightSchedule(), title="workstation")
        view = DisplayView(store, composer.compose)
        status, body = view(Request("/v1/display", {"fw": "1.5.0-w1", "heap": "24512", "rssi": "-61", "err": "x"}, "192.0.2.12"))
        self.assertEqual(status, 200)
        self.assertLessEqual(len(body), MAX_BYTES)
        self.assertEqual(json.loads(body)["proto"], 1)
        telemetry = store.snapshot()[1]["display"].data
        self.assertEqual(telemetry["heap"], 24512)
        self.assertEqual(telemetry["rssi"], -61)
        self.assertIsNone(telemetry["err"])  # junk ignored
        self.assertEqual(telemetry["client"], "192.0.2.12")

    def test_compose_failure_serves_error_screen(self) -> None:
        def broken() -> Screen:
            raise RuntimeError("boom")

        status, body = DisplayView(SnapshotStore(), broken)(None)
        self.assertEqual(status, 200)
        texts = [w.get("text") for w in json.loads(body)["w"]]
        self.assertIn("SCREEN ERROR", texts)


class IconTest(unittest.TestCase):
    def test_mono_payloads_are_valid(self) -> None:
        for name in ("claude", "codex", "cursor", "slack", "cpu", "mem", "disk", "gpu"):
            payload = ICONS[name]
            self.assertEqual(payload[:5], b"SI\x01\x10\x10", name)
            self.assertEqual(len(payload), 5 + 16 * 16 // 2)
            _, _, px = decode_rgba(payload, (255, 255, 255))
            self.assertTrue(any(a for *_, a in px), f"{name} is empty")

    def test_colour_payloads(self) -> None:
        for name in ("slack-rest", "slack-unread", "slack-mention"):
            if name not in ICONS:
                self.skipTest("Slack desktop app not installed when icons were built")
            payload = ICONS[name]
            self.assertEqual(payload[:3], b"SI\x02")
            self.assertLessEqual(payload[5], 16)
            self.assertLessEqual(len(payload), 512)
        _, _, px = decode_rgba(ICONS["slack-mention"], (0, 0, 0))
        self.assertTrue(any(r > 180 and g < 110 and a > 200 for r, g, b, a in px))  # the red dot

    def test_widget_carries_hash_and_rejects_unknown(self) -> None:
        self.assertEqual(Icon(id="i", r=(0, 0, 18, 18), v="cpu").to_json()["h"], fnv1a32(ICONS["cpu"]))
        self.assertNotIn("h", Icon(id="i", r=(0, 0, 18, 18), v="dot").to_json())  # firmware built-in
        with self.assertRaises(LayoutError):
            Icon(id="i", r=(0, 0, 18, 18), v="nope").to_json()

    def test_icon_route(self) -> None:
        status, body, ctype = IconView()(Request("/v1/icon/cpu", {}, "127.0.0.1"))
        self.assertEqual((status, body, ctype), (200, ICONS["cpu"], "application/octet-stream"))
        self.assertEqual(IconView()(Request("/v1/icon/nope", {}, "127.0.0.1"))[0], 404)

    def test_bar_radius_serialised(self) -> None:
        self.assertEqual(Bar(id="b", r=(0, 0, 50, 8), v=5, radius=4).to_json()["radius"], 4)
        self.assertNotIn("radius", Bar(id="b", r=(0, 0, 50, 8), v=5).to_json())


class GradientTest(unittest.TestCase):
    def test_heat_scale(self) -> None:
        from smalltv_monitor.display.dashboard import HEAT, gradient_colour

        self.assertEqual(gradient_colour(HEAT, -5), "#3b82f6")  # clamped cold
        self.assertEqual(gradient_colour(HEAT, 0), "#3b82f6")
        self.assertEqual(gradient_colour(HEAT, 50), "#f2cc38")  # exact stop: yellow in the middle
        self.assertEqual(gradient_colour(HEAT, 100), "#f2384c")
        self.assertEqual(gradient_colour(HEAT, 150), "#f2384c")  # clamped hot
        self.assertEqual(gradient_colour(((0, "#000000"), (10, "#ffffff")), 5), "#808080")  # midpoint

    def test_spark_grad_validation(self) -> None:
        ok = Spark(id="s", r=(0, 0, 50, 20), v=[1, 2], grad=[(0, "#000000"), (100, "#ffffff")], fa=40).to_json()
        self.assertEqual(ok["grad"], [[0, "#000000"], [100, "#ffffff"]])
        for bad in ([(0, "#000000")], [(50, "#000000"), (10, "#ffffff")], [(5, "#000000"), (5, "#ffffff")],
                    [(0, "#000000"), (1, "blue")]):
            with self.subTest(bad=bad), self.assertRaises(LayoutError):
                Spark(id="s", r=(0, 0, 50, 20), v=[1], grad=bad).to_json()

    def test_spark_under_series(self) -> None:
        ok = Spark(id="s", r=(0, 0, 50, 20), v=[1.4, 2], u=[3, 4.6], uc="#f2384c").to_json()
        self.assertEqual((ok["v"], ok["u"], ok["uc"]), ([1, 2], [3, 5], "#f2384c"))
        self.assertNotIn("u", Spark(id="s", r=(0, 0, 50, 20), v=[1]).to_json())
        with self.assertRaisesRegex(LayoutError, "grad"):
            Spark(id="s", r=(0, 0, 50, 20), v=[1], u=[1], grad=[(0, "#000000"), (1, "#ffffff")]).to_json()
        with self.assertRaisesRegex(LayoutError, "samples"):
            Spark(id="s", r=(0, 0, 50, 20), v=[1], u=[1] * 121).to_json()
        with self.assertRaises(LayoutError):
            Spark(id="s", r=(0, 0, 50, 20), v=[1], u=[1], uc="red").to_json()


if __name__ == "__main__":
    unittest.main()
