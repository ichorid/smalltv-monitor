"""JSON views over the snapshot store.

/metrics is a compatibility view: the flat contract parsed by the Times-Z firmware dashboard
(src/dashboard/DashboardManager.cpp). Keep its six display fields stable. New consumers should
use /v1/status, which exposes every registered source generically.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from . import __version__
from .display.icon_data import ICONS
from .display.screen import Screen, error_screen
from .server import ClientLog, Request
from .store import SnapshotStore

Response = tuple[int, bytes]

log = logging.getLogger(__name__)


def _dumps(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), allow_nan=False).encode()


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def render_firmware_metrics(fresh: Mapping[str, Mapping[str, Any]], timestamp: float | None) -> dict[str, Any]:
    """Build the firmware payload from the fresh (non-stale, non-failing) source data.

    ok=false makes the firmware draw "PC OFFLINE", so it only depends on the host metrics.
    A missing GPU yields null GPU fields (the firmware shows 0) and gpu_ok=false.
    """
    cpu = fresh.get("cpu")
    mem = fresh.get("memory")
    gpu = fresh.get("gpu") or {}
    disk_mounts = (fresh.get("disk") or {}).get("mounts") or []
    root = disk_mounts[0] if disk_mounts else {}
    return {
        "ok": cpu is not None and mem is not None,
        # --- fields read by the firmware dashboard ---
        "gpu_usage": gpu.get("util_pct"),
        "cpu_usage": _round(cpu["percent"]) if cpu else None,
        "gpu_vram_mb": _round(gpu.get("mem_used_mib"), 0),
        "memory_used_gb": _round(mem["used_gib"]) if mem else None,
        "gpu_power": _round(gpu.get("power_w")),
        "gpu_temp_c": gpu.get("temp_c"),
        # --- extras, ignored by the current firmware ---
        "gpu_ok": bool(gpu),
        "memory_total_gb": _round(mem["total_gib"]) if mem else None,
        "gpu_vram_total_mb": _round(gpu.get("mem_total_mib"), 0),
        "disk_used_pct": _round(root.get("percent")),
        "disk_used_gb": _round(root.get("used_gib")),
        "ts": None if timestamp is None else int(timestamp),
    }


class FirmwareMetricsView:
    """GET /metrics. Re-renders only when a source updates or goes stale; otherwise serves cached bytes."""

    def __init__(self, store: SnapshotStore) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._key: tuple[int, frozenset[str]] | None = None
        self._body = b""

    def __call__(self, request: Request | None = None) -> Response:
        version, readings = self._store.snapshot()
        now = time.monotonic()
        fresh = {name: r.data for name, r in readings.items() if r.is_fresh(now)}
        key = (version, frozenset(fresh))
        with self._lock:
            if key != self._key:
                cpu = readings.get("cpu")
                self._body = _dumps(render_firmware_metrics(fresh, cpu.timestamp if cpu else None))
                self._key = key
            return 200, self._body


class StatusView:
    """GET /v1/status: every source with its data, age and error. The extensible, source-agnostic view."""

    def __init__(self, store: SnapshotStore, clients: ClientLog | None = None) -> None:
        self._store = store
        self._clients = clients
        self._host = socket.gethostname()

    def __call__(self, request: Request | None = None) -> Response:
        _, readings = self._store.snapshot()
        now = time.monotonic()
        sources = {
            name: {
                "ok": r.is_fresh(now),
                "age_s": None if r.updated is None else round(now - r.updated, 2),
                "error": r.error,
                "data": r.data,
            }
            for name, r in sorted(readings.items())
        }
        wall = time.time()
        clients = {
            ip: {"requests": c["requests"], "last_path": c["last_path"], "age_s": round(wall - c["last_seen"], 1)}
            for ip, c in (self._clients.snapshot() if self._clients else {}).items()
        }
        return 200, _dumps(
            {"host": self._host, "version": __version__, "time": wall, "sources": sources, "clients": clients}
        )


class HealthView:
    def __init__(self) -> None:
        self._started = time.monotonic()

    def __call__(self, request: Request | None = None) -> Response:
        return 200, _dumps({"ok": True, "uptime_s": round(time.monotonic() - self._started)})


class DisplayView:
    """GET /v1/display: the full screen for the widget-renderer firmware (docs/WIDGET-PROTOCOL.md).

    The device's query string (fw, heap, blk, up, rssi, err) is recorded as the "display" source,
    so its health shows up in /v1/status.
    """

    TELEMETRY_INT = ("heap", "blk", "up", "rssi", "err")

    def __init__(self, store: SnapshotStore, compose: Callable[[], Screen], telemetry_max_age: float = 10.0) -> None:
        self._store = store
        self._compose = compose
        self._telemetry_max_age = telemetry_max_age
        self._last_error: str | None = None

    def __call__(self, request: Request | None = None) -> Response:
        if request is not None and request.query:
            self._record(request)
        try:
            body = self._compose().fit()
            if self._last_error is not None:
                log.info("display: screen composes again")
                self._last_error = None
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if message != self._last_error:  # log each distinct failure once
                log.error("display: cannot serve screen: %s", message)
                self._last_error = message
            body = error_screen(str(exc)).fit()
        return 200, body

    def _record(self, request: Request) -> None:
        q = request.query
        data: dict[str, Any] = {"client": request.client, "fw": q.get("fw", "")[:32]}
        for key in self.TELEMETRY_INT:
            try:
                data[key] = int(q[key])
            except (KeyError, ValueError):
                data[key] = None
        self._store.put("display", data, self._telemetry_max_age)


class IconView:
    """GET /v1/icon/<name>: 4-bit alpha bitmap in the protocol's binary format (docs/WIDGET-PROTOCOL.md)."""

    def __call__(self, request: Request | None = None) -> tuple[int, bytes] | tuple[int, bytes, str]:
        name = (request.path if request else "").rsplit("/", 1)[-1]
        payload = ICONS.get(name)
        if payload is None:
            return 404, b'{"ok":false,"error":"unknown icon"}'
        return 200, payload, "application/octet-stream"
