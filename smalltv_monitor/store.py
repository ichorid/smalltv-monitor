"""In-memory store holding the latest reading of every data source."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Reading:
    data: Mapping[str, Any] | None  # last successful sample, kept after a failure for diagnostics
    error: str | None  # set while the source is failing
    updated: float | None  # time.monotonic() of the last successful sample
    timestamp: float | None  # time.time() of the same sample
    max_age: float  # seconds after `updated` at which the sample counts as stale

    def is_fresh(self, now: float) -> bool:
        return self.error is None and self.updated is not None and now - self.updated <= self.max_age


class SnapshotStore:
    """Thread-safe map of source name -> Reading.

    Collectors write, HTTP handlers read. Readings are immutable, so readers only hold the lock
    long enough to copy a small dict. ``version`` increases on every write, letting views cache
    their rendered output until something actually changes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._readings: dict[str, Reading] = {}
        self._version = 0
        self._listeners: list[Callable[[str, Mapping[str, Any]], None]] = []

    def add_listener(self, callback: Callable[[str, Mapping[str, Any]], None]) -> None:
        """Call `callback(source, data)` after every successful put (from the collector's thread)."""
        self._listeners.append(callback)

    def put(self, source: str, data: Mapping[str, Any], max_age: float) -> None:
        reading = Reading(data, None, time.monotonic(), time.time(), max_age)
        with self._lock:
            self._readings[source] = reading
            self._version += 1
        for callback in self._listeners:
            callback(source, data)

    def fail(self, source: str, error: str) -> None:
        with self._lock:
            prev = self._readings.get(source)
            if prev is not None and prev.error == error:
                return
            if prev is None:
                self._readings[source] = Reading(None, error, None, None, 0.0)
            else:
                self._readings[source] = Reading(prev.data, error, prev.updated, prev.timestamp, prev.max_age)
            self._version += 1

    def snapshot(self) -> tuple[int, dict[str, Reading]]:
        with self._lock:
            return self._version, dict(self._readings)
