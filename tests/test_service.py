"""Unit and integration tests (stdlib unittest; no extra dependencies)."""

from __future__ import annotations

import http.client
import ipaddress
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from smalltv_monitor.collectors import CollectorRunner, DiskIoCollector, NvidiaGpuCollector, SourceUnavailable
from smalltv_monitor.server import ClientLog, MetricsHTTPServer, serve_in_thread
from smalltv_monitor.store import SnapshotStore
from smalltv_monitor.views import FirmwareMetricsView, StatusView, render_firmware_metrics

# Fields parsed by GeekMagic-Open-Firmware v1.5.0 src/dashboard/DashboardManager.cpp
FIRMWARE_FIELDS = {"ok", "gpu_usage", "cpu_usage", "gpu_vram_mb", "memory_used_gb", "gpu_power", "gpu_temp_c"}

CPU = {"percent": 24.04, "count": 20, "load1": 1.0}
MEM = {"used_gib": 36.83, "total_gib": 64.0, "percent": 57.5}
GPU = {"util_pct": 28, "mem_used_mib": 6930.4, "mem_total_mib": 16384.0, "temp_c": 50, "power_w": 45.21}
DISK = {"mounts": [{"path": "/", "used_gib": 80.0, "total_gib": 100.0, "percent": 80.0}]}


class FirmwareContractTest(unittest.TestCase):
    def test_all_sources(self) -> None:
        out = render_firmware_metrics({"cpu": CPU, "memory": MEM, "gpu": GPU, "disk": DISK}, 1000.5)
        self.assertLessEqual(FIRMWARE_FIELDS, out.keys())
        self.assertEqual(
            {k: out[k] for k in FIRMWARE_FIELDS},
            {"ok": True, "gpu_usage": 28, "cpu_usage": 24.0, "gpu_vram_mb": 6930, "memory_used_gb": 36.8,
             "gpu_power": 45.2, "gpu_temp_c": 50},
        )
        self.assertTrue(out["gpu_ok"])
        self.assertEqual(out["disk_used_pct"], 80.0)
        self.assertEqual(out["ts"], 1000)

    def test_gpu_missing_keeps_host_metrics_ok(self) -> None:
        out = render_firmware_metrics({"cpu": CPU, "memory": MEM}, None)
        self.assertTrue(out["ok"])
        self.assertFalse(out["gpu_ok"])
        for key in ("gpu_usage", "gpu_vram_mb", "gpu_power", "gpu_temp_c"):
            self.assertIsNone(out[key])

    def test_host_missing_is_not_ok(self) -> None:
        self.assertFalse(render_firmware_metrics({"memory": MEM, "gpu": GPU}, None)["ok"])
        self.assertFalse(render_firmware_metrics({}, None)["ok"])

    def test_payload_is_small_flat_json(self) -> None:
        body = json.dumps(render_firmware_metrics({"cpu": CPU, "memory": MEM, "gpu": GPU, "disk": DISK}, 1.0))
        self.assertLess(len(body), 512)  # parsed into a JsonDocument on an ESP8266 heap


class StoreAndViewTest(unittest.TestCase):
    def test_stale_and_failed_sources_are_dropped(self) -> None:
        store = SnapshotStore()
        store.put("cpu", CPU, 6.0)
        store.put("memory", MEM, 6.0)
        store.put("gpu", GPU, 6.0)
        store.fail("gpu", "driver gone")
        view = FirmwareMetricsView(store)
        out = json.loads(view()[1])
        self.assertTrue(out["ok"])
        self.assertFalse(out["gpu_ok"])
        with mock.patch("smalltv_monitor.views.time.monotonic", return_value=time.monotonic() + 60):
            self.assertFalse(json.loads(view()[1])["ok"])

    def test_slow_source_is_fresh_for_its_own_interval(self) -> None:
        store = SnapshotStore()
        store.put("cpu", CPU, 6.0)
        store.put("memory", MEM, 6.0)
        store.put("disk", DISK, 15.0)  # 10 s interval + 5 s grace
        view, start = FirmwareMetricsView(store), time.monotonic()
        with mock.patch("smalltv_monitor.views.time.monotonic", return_value=start + 8):
            out = json.loads(view()[1])
            self.assertFalse(out["ok"])  # 1 s sources are stale by now...
            self.assertEqual(out["disk_used_pct"], 80.0)  # ...the 10 s one is not
        with mock.patch("smalltv_monitor.views.time.monotonic", return_value=start + 20):
            self.assertIsNone(json.loads(view()[1])["disk_used_pct"])

    def test_view_caches_until_store_changes(self) -> None:
        store = SnapshotStore()
        store.put("cpu", CPU, 6.0)
        store.put("memory", MEM, 6.0)
        view = FirmwareMetricsView(store)
        first = view()[1]
        self.assertIs(view()[1], first)
        store.put("cpu", {**CPU, "percent": 99.0}, 6.0)
        self.assertEqual(json.loads(view()[1])["cpu_usage"], 99.0)

    def test_status_view_reports_errors(self) -> None:
        store = SnapshotStore()
        store.put("cpu", CPU, 6.0)
        store.fail("gpu", "NVML: mismatch")
        status = json.loads(StatusView(store)()[1])
        self.assertTrue(status["sources"]["cpu"]["ok"])
        self.assertEqual(status["sources"]["gpu"], {"ok": False, "age_s": None, "error": "NVML: mismatch", "data": None})


class FakeNVMLError(Exception):
    pass


class FakeNotSupported(FakeNVMLError):
    pass


def fake_nvml(**overrides):
    nv = SimpleNamespace(
        NVMLError=FakeNVMLError,
        NVMLError_NotSupported=FakeNotSupported,
        NVML_TEMPERATURE_GPU=0,
        nvmlMemory_v2=0x2000028,
        nvmlInit=mock.Mock(),
        nvmlShutdown=mock.Mock(),
        nvmlDeviceGetHandleByIndex=mock.Mock(return_value="h0"),
        nvmlDeviceGetName=mock.Mock(return_value="NVIDIA Test GPU"),
        nvmlDeviceGetEnforcedPowerLimit=mock.Mock(return_value=300000),
        nvmlDeviceGetUtilizationRates=mock.Mock(return_value=SimpleNamespace(gpu=28)),
        nvmlDeviceGetMemoryInfo=mock.Mock(return_value=SimpleNamespace(used=6930 * 1024**2, total=16384 * 1024**2)),
        nvmlDeviceGetTemperature=mock.Mock(return_value=50),
        nvmlDeviceGetPowerUsage=mock.Mock(return_value=45210),
    )
    for key, value in overrides.items():
        setattr(nv, key, value)
    return nv


class NvidiaGpuCollectorTest(unittest.TestCase):
    def make(self, nv) -> NvidiaGpuCollector:
        gpu = NvidiaGpuCollector(interval=1.0, index=0)
        gpu._nv = nv
        return gpu

    def test_reads_all_fields(self) -> None:
        nv = fake_nvml()
        data = self.make(nv).collect()
        self.assertEqual(data["util_pct"], 28)
        self.assertAlmostEqual(data["mem_used_mib"], 6930)
        self.assertEqual(data["temp_c"], 50)
        self.assertAlmostEqual(data["power_w"], 45.21)
        self.assertEqual(data["power_limit_w"], 300.0)
        self.assertEqual(nv.nvmlDeviceGetMemoryInfo.call_args.kwargs, {"version": nv.nvmlMemory_v2})

    def test_init_failure_backs_off_then_recovers(self) -> None:
        nv = fake_nvml(nvmlInit=mock.Mock(side_effect=FakeNVMLError("RM has detected an NVML/RM version mismatch")))
        gpu = self.make(nv)
        clock = [100.0]
        with mock.patch("smalltv_monitor.collectors.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(SourceUnavailable, "version mismatch"):
                gpu.collect()
            with self.assertRaises(SourceUnavailable):  # within the 1 s backoff: no new nvmlInit()
                gpu.collect()
            self.assertEqual(nv.nvmlInit.call_count, 1)
            clock[0] += 1.0
            with self.assertRaises(SourceUnavailable):
                gpu.collect()
            self.assertEqual(nv.nvmlInit.call_count, 2)
            self.assertEqual(gpu._backoff, 4.0)
            nv.nvmlInit.side_effect = None  # driver fixed (e.g. after reboot)
            clock[0] += 2.0
            self.assertEqual(gpu.collect()["util_pct"], 28)
            self.assertEqual(gpu._backoff, 1.0)

    def test_read_error_reconnects(self) -> None:
        nv = fake_nvml()
        gpu = self.make(nv)
        gpu.collect()
        nv.nvmlDeviceGetUtilizationRates.side_effect = FakeNVMLError("GPU is lost")
        with self.assertRaisesRegex(SourceUnavailable, "GPU is lost"):
            gpu.collect()
        nv.nvmlShutdown.assert_called_once()
        nv.nvmlDeviceGetUtilizationRates.side_effect = None
        gpu._retry_at = 0
        gpu.collect()
        self.assertEqual(nv.nvmlInit.call_count, 2)

    def test_unsupported_sensors_are_null(self) -> None:
        nv = fake_nvml(nvmlDeviceGetPowerUsage=mock.Mock(side_effect=FakeNotSupported()))
        self.assertIsNone(self.make(nv).collect()["power_w"])

    def test_missing_library(self) -> None:
        gpu = NvidiaGpuCollector(interval=1.0, index=0)
        with mock.patch.dict("sys.modules", {"pynvml": None}):
            gpu.open()
        with self.assertRaisesRegex(SourceUnavailable, "not installed"):
            gpu.collect()


class RunnerTest(unittest.TestCase):
    def test_failures_are_recorded_and_thread_survives(self) -> None:
        calls = []

        class Flaky:
            name, interval = "flaky", 0.01

            def open(self): pass
            def close(self): calls.append("close")

            def collect(self):
                calls.append("collect")
                if len(calls) % 2:
                    raise RuntimeError("boom")
                return {"n": len(calls)}

        store, stop = SnapshotStore(), threading.Event()
        runner = CollectorRunner(Flaky(), store, stop, stale_after=1.0)  # type: ignore[arg-type]
        runner.start()
        time.sleep(0.2)
        stop.set()
        runner.join(1.0)
        self.assertGreater(calls.count("collect"), 5)
        self.assertEqual(calls[-1], "close")
        self.assertIn("flaky", store.snapshot()[1])


class HTTPServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SnapshotStore()
        self.store.put("cpu", CPU, 6.0)
        self.store.put("memory", MEM, 6.0)
        self.clients = ClientLog()
        self.server = MetricsHTTPServer(
            ("127.0.0.1", 0),
            {"/metrics": FirmwareMetricsView(self.store), "/v1/status": StatusView(self.store, self.clients)},
            [ipaddress.ip_network("127.0.0.0/8")],
            self.clients,
        )
        serve_in_thread(self.server)
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def get(self, path: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp, resp.read()

    def test_metrics_response_is_esp_friendly(self) -> None:
        resp, body = self.get("/metrics")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.version, 10)  # HTTP/1.0: connection closes after the body
        self.assertEqual(int(resp.getheader("Content-Length")), len(body))
        self.assertIsNone(resp.getheader("Transfer-Encoding"))
        self.assertTrue(json.loads(body)["ok"])

    def test_status_lists_polling_clients(self) -> None:
        for _ in range(3):
            self.get("/metrics")
        client = json.loads(self.get("/v1/status")[1])["clients"]["127.0.0.1"]
        self.assertEqual(client["requests"], 4)
        self.assertEqual(client["last_path"], "/v1/status")

    def test_unknown_path(self) -> None:
        self.assertEqual(self.get("/nope")[0].status, 404)

    def test_concurrent_requests(self) -> None:
        with ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(lambda _: self.get("/metrics")[0].status, range(300)))
        # Requests beyond MAX_CONCURRENT may be shed, but the server must stay up and answer the rest
        self.assertGreater(statuses.count(200), 250)
        self.assertEqual(self.get("/metrics")[0].status, 200)

    def test_client_outside_allowed_networks_is_refused(self) -> None:
        self.server.allowed = [ipaddress.ip_network("10.0.0.0/8")]
        with self.assertRaises((ConnectionError, http.client.HTTPException)):
            self.get("/metrics")



class DiskIoCollectorTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.block = Path(tmp.name)
        for disk in ("nvme0n1", "sda"):  # physical disks have a device link; partitions and loops do not
            (self.block / disk / "device").mkdir(parents=True)
        (self.block / "loop0").mkdir()

    @staticmethod
    def counters(read: int, written: int) -> dict[str, SimpleNamespace]:
        mib = 1024**2
        return {
            "nvme0n1": SimpleNamespace(read_bytes=read * mib, write_bytes=written * mib),
            "nvme0n1p1": SimpleNamespace(read_bytes=read * mib, write_bytes=written * mib),  # partition: same bytes
            "sda": SimpleNamespace(read_bytes=read * mib, write_bytes=0),
            "loop0": SimpleNamespace(read_bytes=999 * mib, write_bytes=999 * mib),
        }

    def test_rates_over_physical_disks_only(self) -> None:
        collector = DiskIoCollector(interval=1.0, block_dir=str(self.block))
        clock = iter([100.0, 102.0, 103.0])
        with mock.patch("psutil.disk_io_counters") as io, mock.patch("time.monotonic", lambda: next(clock)):
            io.return_value = self.counters(10, 4)
            collector.open()
            io.return_value = self.counters(30, 10)
            out = collector.collect()
            self.assertEqual(out["disks"], ["nvme0n1", "sda"])
            self.assertAlmostEqual(out["read_mib_s"], 20.0)  # (20 + 20) MiB over 2 s
            self.assertAlmostEqual(out["write_mib_s"], 3.0)  # 6 MiB over 2 s
            io.return_value = self.counters(1, 1)  # a disk went away: totals drop
            out = collector.collect()
            self.assertEqual((out["read_mib_s"], out["write_mib_s"]), (0.0, 0.0))

    def test_no_physical_disk_is_unavailable(self) -> None:
        collector = DiskIoCollector(interval=1.0, block_dir=str(self.block / "missing"))
        with mock.patch("psutil.disk_io_counters", return_value=self.counters(1, 1)):
            with self.assertRaises(SourceUnavailable):
                collector.open()


if __name__ == "__main__":
    unittest.main()
