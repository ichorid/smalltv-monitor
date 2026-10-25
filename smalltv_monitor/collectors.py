"""Metric collectors and the threads that poll them.

Each collector runs in its own CollectorRunner thread at its own interval and publishes a plain
dict to the SnapshotStore. HTTP handlers only ever read the store, so a slow or wedged source
(e.g. a hung NVIDIA driver) cannot delay a request or starve another collector, and any number
of concurrent requests costs zero extra GPU queries.

Adding a data source means subclassing Collector and registering it in __main__.build_collectors;
nothing in the HTTP layer needs to know about it (it shows up in /v1/status automatically).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import Any

import psutil

from .store import SnapshotStore

log = logging.getLogger(__name__)

GIB = 1024**3
MIB = 1024**2
MB = 1000**2


class SourceUnavailable(Exception):
    """The data source is temporarily unavailable; the runner will keep retrying."""


class Collector(ABC):
    name: str
    interval: float

    def open(self) -> None:
        """Acquire resources. Called once from the runner thread before the first collect()."""

    @abstractmethod
    def collect(self) -> dict[str, Any]:
        """Return one sample, or raise SourceUnavailable."""

    def close(self) -> None:
        """Release resources. Called once from the runner thread on shutdown."""


class CpuCollector(Collector):
    name = "cpu"

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._count = 0

    def open(self) -> None:
        self._count = psutil.cpu_count() or 0
        # The first calls only establish reference samples
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
        psutil.cpu_times_percent(interval=None)

    def collect(self) -> dict[str, Any]:
        # Non-blocking: utilisation since the previous call, i.e. over one collection interval
        times = psutil.cpu_times_percent(interval=None)
        return {
            "percent": psutil.cpu_percent(interval=None),
            "user": times.user + times.nice,
            "system": times.system + getattr(times, "irq", 0.0) + getattr(times, "softirq", 0.0),
            "iowait": getattr(times, "iowait", 0.0),
            "per_core": psutil.cpu_percent(interval=None, percpu=True),
            "count": self._count,
            "load1": os.getloadavg()[0],
        }


class MemoryCollector(Collector):
    name = "memory"

    def __init__(self, interval: float) -> None:
        self.interval = interval

    def collect(self) -> dict[str, Any]:
        vm = psutil.virtual_memory()
        # total - available: memory that cannot be reclaimed without swapping (what htop/free report as used)
        used = vm.total - vm.available
        buffers = getattr(vm, "buffers", 0)
        cached = getattr(vm, "cached", 0)  # psutil on Linux: Cached + SReclaimable (includes Shmem)
        shared = getattr(vm, "shared", 0)
        return {
            "used_gib": used / GIB,
            "cache_gib": (buffers + cached) / GIB,
            "total_gib": vm.total / GIB,
            "percent": vm.percent,
            # htop's memory meter: used + shared + buffers + cache = total - free
            "htop": {
                "used_gib": max(0, vm.total - vm.free - buffers - cached) / GIB,
                "shared_gib": shared / GIB,
                "buffers_gib": buffers / GIB,
                "cache_gib": max(0, cached - shared) / GIB,
            },
        }


class DiskCollector(Collector):
    name = "disk"

    def __init__(self, interval: float, paths: Sequence[str]) -> None:
        self.interval = interval
        self.paths = list(paths)

    def collect(self) -> dict[str, Any]:
        mounts = []
        for path in self.paths:
            try:
                usage = psutil.disk_usage(path)
            except OSError as exc:
                raise SourceUnavailable(f"{path}: {exc}") from exc
            mounts.append(
                {"path": path, "used_gib": usage.used / GIB, "free_gib": usage.free / GIB,
                 "total_gib": usage.total / GIB, "percent": usage.percent}
            )
        return {"mounts": mounts}


class DiskIoCollector(Collector):
    """Read and write throughput in MB/s (10^6 bytes), summed over the configured block devices.

    With no devices configured, every physical disk counts: those with a `device` link in sysfs, since
    partitions, loop devices (snaps) and device-mapper layers would count the same bytes twice.
    """

    name = "diskio"

    def __init__(self, interval: float, disks: Sequence[str] = (), block_dir: str = "/sys/block") -> None:
        self.interval = interval
        self.disks = list(disks)
        self.block_dir = block_dir
        self._last: tuple[float, int, int] | None = None

    def _totals(self) -> tuple[int, int, list[str]]:
        counters = psutil.disk_io_counters(perdisk=True) or {}
        if self.disks:
            missing = [n for n in self.disks if n not in counters]
            if missing:
                raise SourceUnavailable(f"block device not found: {', '.join(missing)}")
            disks = self.disks
        else:
            disks = sorted(n for n in counters if os.path.exists(os.path.join(self.block_dir, n, "device")))
        if not disks:
            raise SourceUnavailable("no physical disks in disk_io_counters")
        return sum(counters[n].read_bytes for n in disks), sum(counters[n].write_bytes for n in disks), disks

    def open(self) -> None:
        read, written, _ = self._totals()  # reference sample: rates start with the first collect()
        self._last = (time.monotonic(), read, written)

    def collect(self) -> dict[str, Any]:
        read, written, disks = self._totals()
        now = time.monotonic()
        last, self._last = self._last, (now, read, written)
        if last is None or now <= last[0]:
            raise SourceUnavailable("no reference sample yet")
        elapsed = now - last[0]
        # A disk that disappeared makes the totals drop: report 0 rather than a negative rate
        return {
            "read_mb_s": max(0, read - last[1]) / MB / elapsed,
            "write_mb_s": max(0, written - last[2]) / MB / elapsed,
            "disks": disks,
        }


class NvidiaGpuCollector(Collector):
    """Reads one GPU through NVML (the library nvidia-smi itself uses), without spawning processes.

    Any NVML error drops the session; reconnects back off exponentially up to MAX_BACKOFF so a
    missing or mismatched driver costs at most one cheap nvmlInit() attempt per MAX_BACKOFF.
    """

    name = "gpu"
    MAX_BACKOFF = 30.0

    def __init__(self, interval: float, index: int) -> None:
        self.interval = interval
        self.index = index
        self._nv: Any = None
        self._initialized = False
        self._handle: Any = None
        self._static: dict[str, Any] = {}
        self._memory_version: Any = None
        self._backoff = 1.0
        self._retry_at = 0.0
        self._last_error = "not connected"

    def open(self) -> None:
        try:
            import pynvml
        except ImportError:
            self._last_error = "nvidia-ml-py is not installed"
            return
        self._nv = pynvml

    def collect(self) -> dict[str, Any]:
        if self._handle is None:
            self._connect()
        nv, handle = self._nv, self._handle
        try:
            util = nv.nvmlDeviceGetUtilizationRates(handle).gpu
            if self._memory_version is not None:
                mem = nv.nvmlDeviceGetMemoryInfo(handle, version=self._memory_version)
            else:
                mem = nv.nvmlDeviceGetMemoryInfo(handle)
            temp = self._optional(lambda: nv.nvmlDeviceGetTemperature(handle, nv.NVML_TEMPERATURE_GPU))
            power_mw = self._optional(lambda: nv.nvmlDeviceGetPowerUsage(handle))
            # Read every sample: the limit can be changed at any time (nvidia-smi -pl)
            limit_mw = self._optional(lambda: nv.nvmlDeviceGetEnforcedPowerLimit(handle))
        except nv.NVMLError as exc:
            # Driver reload, GPU reset, Xid error...: start over with a fresh nvmlInit()
            self._disconnect()
            self._schedule_retry(f"NVML: {exc}")
            raise SourceUnavailable(self._last_error) from exc
        return {
            **self._static,
            "util_pct": util,
            "mem_used_mib": mem.used / MIB,
            "mem_total_mib": mem.total / MIB,
            "temp_c": temp,
            "power_w": None if power_mw is None else power_mw / 1000,
            "power_limit_w": None if limit_mw is None else limit_mw / 1000,
        }

    def close(self) -> None:
        self._disconnect()

    def _connect(self) -> None:
        if self._nv is None:
            raise SourceUnavailable(self._last_error)
        if time.monotonic() < self._retry_at:
            raise SourceUnavailable(self._last_error)
        nv = self._nv
        try:
            nv.nvmlInit()
            self._initialized = True
            handle = nv.nvmlDeviceGetHandleByIndex(self.index)
            name = nv.nvmlDeviceGetName(handle)
            self._memory_version = self._probe_memory_v2(handle)
        except nv.NVMLError as exc:
            self._disconnect()
            self._schedule_retry(f"NVML: {exc}")
            raise SourceUnavailable(self._last_error) from exc
        self._handle = handle
        self._static = {
            "index": self.index,
            "name": name.decode() if isinstance(name, bytes) else name,
        }
        self._backoff = 1.0
        log.info("gpu: connected to %s (index %d) via NVML", self._static["name"], self.index)

    def _probe_memory_v2(self, handle: Any) -> Any:
        # v2 reports "used" excluding driver-reserved memory, matching nvidia-smi's Memory-Usage column
        nv = self._nv
        version = getattr(nv, "nvmlMemory_v2", None)
        if version is None:
            return None
        try:
            nv.nvmlDeviceGetMemoryInfo(handle, version=version)
        except nv.NVMLError:
            return None
        return version

    def _optional(self, read: Callable[[], Any]) -> Any:
        try:
            return read()
        except self._nv.NVMLError_NotSupported:
            return None

    def _schedule_retry(self, error: str) -> None:
        self._last_error = error
        self._retry_at = time.monotonic() + self._backoff
        self._backoff = min(self._backoff * 2, self.MAX_BACKOFF)

    def _disconnect(self) -> None:
        self._handle = None
        if self._initialized:
            self._initialized = False
            try:
                self._nv.nvmlShutdown()
            except self._nv.NVMLError:
                pass


class CollectorRunner:
    """Polls one collector on a fixed cadence until stopped; never lets an exception kill the thread."""

    def __init__(self, collector: Collector, store: SnapshotStore, stop: threading.Event, stale_after: float) -> None:
        self.collector = collector
        self._store = store
        # A sample stays fresh for one interval plus a grace period for jitter and slow reads
        self._max_age = collector.interval + stale_after
        self._stop = stop
        self._thread = threading.Thread(target=self._run, name=f"collector-{collector.name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.warning("%s: collector did not stop within %.1fs", self.collector.name, timeout)

    def _run(self) -> None:
        collector = self.collector
        try:
            collector.open()
        except Exception:
            log.exception("%s: open() failed", collector.name)
        failing: str | None = None
        next_tick = time.monotonic()
        while not self._stop.is_set():
            error = self._collect_once()
            if error is None:
                if failing is not None:
                    log.info("%s: recovered", collector.name)
                failing = None
            else:
                self._store.fail(collector.name, error)
                if error != failing:  # log state changes, not every failed poll
                    log.warning("%s: unavailable: %s", collector.name, error)
                failing = error
            next_tick += collector.interval
            now = time.monotonic()
            if next_tick < now:  # overran (suspend, slow source): skip missed ticks instead of bursting
                next_tick = now + collector.interval
            self._stop.wait(next_tick - now)
        try:
            collector.close()
        except Exception:
            log.exception("%s: close() failed", collector.name)

    def _collect_once(self) -> str | None:
        try:
            self._store.put(self.collector.name, self.collector.collect(), self._max_age)
        except SourceUnavailable as exc:
            return str(exc)
        except Exception as exc:
            log.debug("%s: unexpected error", self.collector.name, exc_info=True)
            return f"{type(exc).__name__}: {exc}"
        return None
