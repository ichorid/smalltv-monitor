#!/usr/bin/env python3
"""Watch how regularly the display polls /metrics, using the service's own client log.

    python3 scripts/watch-display.py [--client DEVICE_IP] [--minutes 30]

The client defaults to SMALLTV_DEVICE_IP (.env, see .env.example), else the non-loopback client that
polled most recently.

Prints every gap longer than 3 s (a reboot or Wi-Fi drop shows up as one) and a summary at the end.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smalltv_monitor import siteenv  # noqa: E402

STATUS = "http://127.0.0.1:8765/v1/status"


def clients() -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(STATUS, timeout=2) as resp:
            return json.load(resp)["clients"]
    except OSError:
        return None


def sample(client: str) -> tuple[int, float] | None:
    c = (clients() or {}).get(client)
    return (c["requests"], c["age_s"]) if c else None


def latest_client() -> str | None:
    remote = {ip: c for ip, c in (clients() or {}).items() if not ip.startswith("127.")}
    return min(remote, key=lambda ip: remote[ip]["age_s"]) if remote else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default=siteenv.get("SMALLTV_DEVICE_IP"))
    parser.add_argument("--minutes", type=float, default=30)
    args = parser.parse_args()
    args.client = args.client or latest_client()
    if not args.client:
        sys.exit("no display client seen by the service; pass --client or set SMALLTV_DEVICE_IP")
    print(f"watching {args.client}", flush=True)

    start = time.monotonic()
    end = start + args.minutes * 60
    first = sample(args.client)
    worst_age, gaps, in_gap, misses = 0.0, 0, False, 0
    last = first
    while time.monotonic() < end:
        time.sleep(1)
        s = sample(args.client)
        if s is None:
            misses += 1
            continue
        last = s
        worst_age = max(worst_age, s[1])
        if s[1] > 3 and not in_gap:
            gaps += 1
            in_gap = True
            print(f"{time.strftime('%T')} gap: no poll for {s[1]:.1f} s", flush=True)
        elif s[1] <= 3 and in_gap:
            in_gap = False
            print(f"{time.strftime('%T')} polling again", flush=True)
    elapsed = time.monotonic() - start
    polls = (last[0] - first[0]) if first and last else 0
    print(f"{elapsed / 60:.1f} min: {polls} polls ({polls / elapsed:.2f}/s), "
          f"gaps >3 s: {gaps}, longest silence seen: {worst_age:.1f} s, status fetch failures: {misses}")


if __name__ == "__main__":
    main()
