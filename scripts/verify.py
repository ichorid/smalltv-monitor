#!/usr/bin/env python3
"""Check a running service against independent sources (nvidia-smi, /proc) and the firmware contract.

    .venv/bin/python scripts/verify.py [--url http://PC_IP:8765/metrics] [--samples 10]

Use the LAN address as --url to test the path the display takes (default: loopback).

Exit status 0 = all checks passed; GPU checks are reported as SKIP when nvidia-smi itself fails.
"""

from __future__ import annotations

import argparse
import http.client
import json
import statistics
import subprocess
import sys
import time
from urllib.parse import urlsplit

FIRMWARE_FIELDS = {"ok": bool, "gpu_usage": (int, float), "cpu_usage": (int, float), "gpu_vram_mb": (int, float),
                   "memory_used_gb": (int, float), "gpu_power": (int, float), "gpu_temp_c": (int, float)}
GIB = 1024**3

results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool | None, detail: str = "") -> None:
    results.append(("SKIP" if ok is None else "ok" if ok else "FAIL", name, detail))


def fetch(url: str) -> tuple[dict, float]:
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=2)
    start = time.perf_counter()
    conn.request("GET", parts.path)
    resp = conn.getresponse()
    body = resp.read()
    elapsed = (time.perf_counter() - start) * 1000
    conn.close()
    if resp.status != 200:
        raise RuntimeError(f"HTTP {resp.status}")
    if resp.getheader("Transfer-Encoding"):
        raise RuntimeError("chunked response; the firmware cannot parse it")
    return json.loads(body), elapsed


def nvidia_smi() -> dict | None:
    query = "utilization.gpu,memory.used,temperature.gpu,power.draw"
    try:
        out = subprocess.run(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits", "-i", "0"],
                             capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    util, mem, temp, power = (float(x) for x in out.strip().split(","))
    return {"util": util, "mem": mem, "temp": temp, "power": power}


def meminfo_used_gib() -> float:
    info = dict(line.split(":", 1) for line in open("/proc/meminfo"))
    kib = lambda k: int(info[k].split()[0])  # noqa: E731
    return (kib("MemTotal") - kib("MemAvailable")) * 1024 / GIB


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765/metrics")
    parser.add_argument("--samples", type=int, default=10)
    args = parser.parse_args()

    try:
        first, _ = fetch(args.url)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  cannot fetch {args.url}: {exc}")
        return 1
    missing = [k for k, t in FIRMWARE_FIELDS.items() if k not in first]
    record("firmware fields present", not missing, ", ".join(missing))
    wrong = [k for k, t in FIRMWARE_FIELDS.items() if first.get(k) is not None and not isinstance(first[k], t)]
    record("firmware field types", not wrong, ", ".join(wrong))
    record("ok=true", first.get("ok") is True)
    latencies, ram_err, gpu_rows, disk_missing = [], [], [], 0
    for _ in range(args.samples):
        # The service's sample is up to one interval old: compare with /proc/meminfo over that window
        window = []
        for _ in range(10):
            window.append(meminfo_used_gib())
            time.sleep(0.1)
        data, ms = fetch(args.url)
        window.append(meminfo_used_gib())
        latencies.append(ms)
        used = data["memory_used_gb"]
        ram_err.append(max(0.0, min(window) - used, used - max(window)))
        disk_missing += not isinstance(data.get("disk_used_pct"), (int, float))
        smi = nvidia_smi()
        if smi is not None:
            gpu_rows.append((data, smi))
    record("latency well under the firmware's 800 ms timeout", max(latencies) < 100,
           f"median {statistics.median(latencies):.1f} ms, max {max(latencies):.1f} ms")
    record("RAM within /proc/meminfo range of the last second (±0.1 GiB rounding)", max(ram_err) <= 0.1, f"max diff {max(ram_err):.2f} GiB")
    record("disk usage in every sample", disk_missing == 0, f"{first.get('disk_used_pct')}%, missing in {disk_missing}")
    record("cpu_usage within 0..100", 0 <= first["cpu_usage"] <= 100, f"{first['cpu_usage']}%")

    if not gpu_rows:
        record("GPU vs nvidia-smi", None, "nvidia-smi unavailable (driver/library mismatch? reboot)")
        record("gpu_ok flag false while GPU unavailable", first.get("gpu_ok") is False)
    else:
        usable = [(d, s) for d, s in gpu_rows if d.get("gpu_ok")]
        record("gpu_ok while nvidia-smi works", len(usable) == len(gpu_rows), f"{len(usable)}/{len(gpu_rows)} samples")
        if usable:
            def worst(key: str, smi_key: str) -> float:
                return max(abs(d[key] - s[smi_key]) for d, s in usable)
            record("VRAM matches nvidia-smi (±64 MiB)", worst("gpu_vram_mb", "mem") <= 64, f"max diff {worst('gpu_vram_mb', 'mem'):.0f} MiB")
            record("GPU temp matches (±2 C)", worst("gpu_temp_c", "temp") <= 2, f"max diff {worst('gpu_temp_c', 'temp'):.0f} C")
            # utilisation and power are instantaneous and sampled at slightly different moments
            record("GPU power close (±20 W)", worst("gpu_power", "power") <= 20, f"max diff {worst('gpu_power', 'power'):.1f} W")
            record("GPU util close (±25 %)", worst("gpu_usage", "util") <= 25, f"max diff {worst('gpu_usage', 'util'):.0f} %")

    for status, name, detail in results:
        print(f"{status:<5} {name}" + (f"  [{detail}]" if detail else ""))
    return 1 if any(s == "FAIL" for s, _, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
