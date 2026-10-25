"""Claude / Codex / Cursor usage limits, as implemented by Alloy (https://github.com/ichorid/alloy,
src/alloy/limits/).

Alloy probes each harness (Claude: OAuth usage API; Codex: local session rollouts; Cursor: dashboard
API) and writes the result to its shared cache `~/.alloy/limits.json` (or `$ALLOY_ROOT`); the Alloy
monitor refreshes it every 60 s. This collector reads that cache instead of probing again, so the
rate-limited Claude endpoint is not hit twice. When the cache is stale (monitor not running) it runs
`alloy limits --json` itself, at most every `refresh_after` seconds.

Probes fail often (Claude answers HTTP 429 for long stretches), so the last good windows of each
harness are kept in memory and reported as stale rather than dropped.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .collectors import Collector, SourceUnavailable

log = logging.getLogger(__name__)

HARNESSES = ("claude", "codex", "cursor")


def default_cache_path() -> Path:
    return Path(os.environ.get("ALLOY_ROOT") or Path.home() / ".alloy").expanduser() / "limits.json"


class AgentLimitsCollector(Collector):
    name = "limits"

    def __init__(
        self,
        interval: float,
        cache_path: Path,
        refresh_cmd: Sequence[str] | None,
        refresh_after: float = 300.0,
        refresh_timeout: float = 60.0,
    ) -> None:
        self.interval = interval
        self.cache_path = cache_path
        self.refresh_cmd = list(refresh_cmd) if refresh_cmd else None
        self.refresh_after = refresh_after
        self.refresh_timeout = refresh_timeout
        self._last_refresh = float("-inf")  # never: monotonic time starts near 0 at boot
        self._last_good: dict[str, dict[str, Any]] = {}

    def collect(self) -> dict[str, Any]:
        self._refresh_if_stale()
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            age = time.time() - self.cache_path.stat().st_mtime
        except (OSError, ValueError) as exc:
            if not self._last_good:
                raise SourceUnavailable(f"{self.cache_path}: {exc}") from exc
            raw, age = {}, None
        harnesses = {h: self._harness(h, raw.get(h) if isinstance(raw, dict) else None) for h in HARNESSES}
        return {"cache_age_s": None if age is None else round(age), "harnesses": harnesses}

    def _harness(self, harness: str, sample: Any) -> dict[str, Any]:
        sample = sample if isinstance(sample, dict) else {}
        windows = {
            w["label"]: {"used": float(w["used_percent"]), "resets_at": w.get("resets_at")}
            for w in sample.get("windows") or []
            if isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float)) and w.get("label")
        }
        if sample.get("available") and windows:
            entry = {"windows": windows, "as_of": sample.get("as_of"), "stale": False,
                     "error": None, "status": sample.get("status")}
            self._last_good[harness] = entry
            return entry
        error = sample.get("error") or ("not in cache" if not sample else "no windows")
        if harness in self._last_good:
            return {**self._last_good[harness], "stale": True, "error": error}
        return {"windows": {}, "as_of": None, "stale": False, "error": error, "status": sample.get("status")}

    def _refresh_if_stale(self) -> None:
        if self.refresh_cmd is None:
            return
        try:
            age = time.time() - self.cache_path.stat().st_mtime
        except OSError:
            age = float("inf")
        now = time.monotonic()
        if age < self.refresh_after or now - self._last_refresh < self.refresh_after:
            return
        self._last_refresh = now
        log.info("limits: Alloy cache is %s, running %s", "missing" if age == float("inf") else f"{age:.0f}s old",
                 " ".join(self.refresh_cmd))
        try:
            subprocess.run(self.refresh_cmd, cwd=Path.home(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=self.refresh_timeout, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("limits: refresh failed: %s", exc)
