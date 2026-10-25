"""Short in-memory histories of selected metrics, for sparklines.

Filled from SnapshotStore updates (collector cadence), not from display polls, so the series stays
regular however often (or rarely) the display asks.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

Extractor = Callable[[Mapping[str, Any]], float | None]


class History:
    def __init__(self, length: int = 120) -> None:
        self._length = length
        self._lock = threading.Lock()
        self._series: dict[str, deque[float]] = {}
        self._sources: dict[str, list[tuple[str, Extractor]]] = {}

    def track(self, name: str, source: str, extract: Extractor) -> None:
        """Record `extract(data)` as series `name` whenever `source` publishes a sample."""
        self._series[name] = deque(maxlen=self._length)
        self._sources.setdefault(source, []).append((name, extract))

    def on_sample(self, source: str, data: Mapping[str, Any]) -> None:
        for name, extract in self._sources.get(source, ()):
            value = extract(data)
            if value is not None:
                with self._lock:
                    self._series[name].append(value)

    def get(self, name: str, count: int) -> list[float]:
        with self._lock:
            series = self._series.get(name)
            return list(series)[-count:] if series else []
