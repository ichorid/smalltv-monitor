"""Entry point: python -m smalltv_monitor [--config FILE] [overrides...]"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import signal
import socket
import sys
import threading
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

import psutil

from . import __version__
from .agent_limits import AgentLimitsCollector, default_cache_path
from .collectors import (Collector, CollectorRunner, CpuCollector, DiskCollector, DiskIoCollector, MemoryCollector,
                         NvidiaGpuCollector)
from .display.dashboard import DashboardComposer, track_history
from .slack import SlackCollector
from .display.history import History
from .display.testpattern import test_pattern
from .display.policy import NightSchedule, parse_hhmm
from .server import ClientLog, MetricsHTTPServer, Network, serve_in_thread
from .store import SnapshotStore
from .views import DisplayView, FirmwareMetricsView, HealthView, IconView, StatusView

log = logging.getLogger("smalltv_monitor")

DEFAULT_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "smalltv-monitor" / "config.toml"


@dataclass
class Config:
    # "auto" = the LAN address carrying the default route; otherwise literal IPs ("0.0.0.0" = everywhere)
    bind: list[str] = field(default_factory=lambda: ["auto", "127.0.0.1"])
    port: int = 8765
    # "auto" = loopback + the subnets of the bound addresses; [] disables app-level filtering
    allow: list[str] = field(default_factory=lambda: ["auto"])
    interval: float = 1.0
    disk_interval: float = 10.0
    disk_paths: list[str] = field(default_factory=lambda: ["/", "/home"])
    # Disk I/O sparkline: block devices to sum ([] = every physical disk), and the MB/s each series is drawn
    # against (its full height; 0 = the busiest moment shown)
    io_disks: list[str] = field(default_factory=list)
    io_max_read_mb_s: float = 0.0
    io_max_write_mb_s: float = 0.0
    io_scale: str = "log"  # "log": height ~ log(1 + MB/s), so 1 MB/s and 1 GB/s both show; or "linear"
    gpu: str = "nvidia"  # "nvidia" or "none"
    gpu_index: int = 0
    stale_after: float = 5.0  # grace beyond a source's own interval before it is reported unavailable
    log_level: str = "INFO"
    # /v1/display (widget-renderer firmware)
    title: str = ""  # header text; empty = hostname
    screen: str = "dashboard"  # or "testpattern" (colour diagnostics)
    day_bri: int = 80
    night_bri: int = 10
    night_start: str = "22:00"
    night_end: str = "07:00"
    # AI usage limits, read from Alloy's cache (smalltv_monitor/agent_limits.py)
    limits_interval: float = 30.0
    alloy_limits_cache: str = ""  # empty = $ALLOY_ROOT/limits.json or ~/.alloy/limits.json
    alloy_refresh_cmd: list[str] = field(default_factory=lambda: ["alloy", "limits", "--json"])
    alloy_refresh_after: float = 300.0  # run alloy_refresh_cmd when the cache is older than this; [] disables
    # Slack red-dot detection from its tray icon (smalltv_monitor/slack.py)
    slack: bool = True
    slack_interval: float = 2.0


def load_config(argv: list[str]) -> Config:
    parser = argparse.ArgumentParser(prog="smalltv-monitor", description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help=f"TOML file (default: {DEFAULT_CONFIG})")
    parser.add_argument("--bind", action="append", help="address to listen on; repeatable (default: auto + 127.0.0.1)")
    parser.add_argument("--port", type=int)
    parser.add_argument("--allow", action="append", help="allowed client CIDR or 'auto'; repeatable")
    parser.add_argument("--interval", type=float)
    parser.add_argument("--gpu", choices=["nvidia", "none"])
    parser.add_argument("--gpu-index", type=int)
    parser.add_argument("--log-level")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    config = Config()
    known = {f.name for f in fields(Config)}
    if args.config.is_file():
        with args.config.open("rb") as fh:
            data = tomllib.load(fh)
        unknown = set(data) - known
        if unknown:
            parser.error(f"{args.config}: unknown keys: {', '.join(sorted(unknown))}")
        for key, value in data.items():
            setattr(config, key, value)
    elif args.config != DEFAULT_CONFIG:
        parser.error(f"config file not found: {args.config}")

    for key in known:
        value = getattr(args, key, None)
        if value is not None:
            setattr(config, key, value)
    if min(config.interval, config.disk_interval, config.stale_after) <= 0:
        parser.error("interval, disk_interval and stale_after must be positive")
    if min(config.io_max_read_mb_s, config.io_max_write_mb_s) < 0:
        parser.error("io_max_read_mb_s and io_max_write_mb_s must be 0 (auto) or positive")
    if config.io_scale not in ("log", "linear"):
        parser.error('io_scale must be "log" or "linear"')
    try:
        parse_hhmm(config.night_start), parse_hhmm(config.night_end)
    except ValueError:
        parser.error("night_start/night_end must be HH:MM")
    if not (0 <= config.day_bri <= 100 and 0 <= config.night_bri <= 100):
        parser.error("day_bri and night_bri must be 0..100")
    return config


def setup_logging(level: str) -> None:
    # Under systemd, journald adds its own timestamps
    fmt = "%(levelname)s %(name)s: %(message)s" if "JOURNAL_STREAM" in os.environ else "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=level.upper(), format=fmt, stream=sys.stderr)


def default_route_address() -> str | None:
    """Source address the kernel would use for off-link traffic (UDP connect sends no packet)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
        except OSError:
            return None
        addr = sock.getsockname()[0]
    return None if addr.startswith("0.") else addr


def interface_network(addr: str) -> Network | None:
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family == socket.AF_INET and a.address == addr and a.netmask:
                return ipaddress.ip_network(f"{addr}/{a.netmask}", strict=False)
    return None


def resolve_binds(config: Config, stop: threading.Event) -> list[str] | None:
    """Expand "auto", waiting for the network if needed (the service may start before it is up)."""
    warned = False
    while True:
        lan = default_route_address() if "auto" in config.bind else None
        if lan is not None or "auto" not in config.bind:
            break
        if not warned:
            log.warning("no default route yet; waiting for the network to come up")
            warned = True
        if stop.wait(2.0):
            return None
    binds = [lan if b == "auto" else b for b in config.bind]
    return list(dict.fromkeys(binds))  # dedupe, keep order


def resolve_allowed(config: Config, binds: list[str]) -> list[Network] | None:
    if not config.allow:
        return None
    nets: list[Network] = []
    for entry in config.allow:
        if entry == "auto":
            nets += [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]
            nets += [n for b in binds if b not in ("0.0.0.0", "::") and (n := interface_network(b)) is not None]
        else:
            nets.append(ipaddress.ip_network(entry, strict=False))
    return list(dict.fromkeys(nets))


def build_collectors(config: Config) -> list[Collector]:
    collectors: list[Collector] = [
        CpuCollector(config.interval),
        MemoryCollector(config.interval),
        DiskCollector(config.disk_interval, config.disk_paths),
        DiskIoCollector(config.interval, config.io_disks),
    ]
    if config.gpu == "nvidia":
        collectors.append(NvidiaGpuCollector(config.interval, config.gpu_index))
    cache = Path(config.alloy_limits_cache).expanduser() if config.alloy_limits_cache else default_cache_path()
    refresh = [str(Path(config.alloy_refresh_cmd[0]).expanduser()), *config.alloy_refresh_cmd[1:]] if config.alloy_refresh_cmd else None
    collectors.append(AgentLimitsCollector(config.limits_interval, cache, refresh, config.alloy_refresh_after))
    if config.slack:
        collectors.append(SlackCollector(config.slack_interval))
    return collectors


def main(argv: list[str] | None = None) -> int:
    config = load_config(sys.argv[1:] if argv is None else argv)
    setup_logging(config.log_level)

    stop = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        log.info("received %s, shutting down", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    store = SnapshotStore()
    history = History()
    track_history(history)
    history.track("gpu", "gpu", lambda d: d.get("util_pct"))
    store.add_listener(history.on_sample)
    schedule = NightSchedule(config.day_bri, config.night_bri, parse_hhmm(config.night_start), parse_hhmm(config.night_end))
    composer = DashboardComposer(store, history, schedule, config.title or None,
                                 io_max_mb_s=(config.io_max_read_mb_s, config.io_max_write_mb_s),
                                 io_scale=config.io_scale)
    runners = [CollectorRunner(c, store, stop, config.stale_after) for c in build_collectors(config)]
    for runner in runners:
        runner.start()

    binds = resolve_binds(config, stop)
    if binds is None:
        return 0
    allowed = resolve_allowed(config, binds)
    clients = ClientLog()
    routes = {
        "/metrics": FirmwareMetricsView(store),
        "/v1/status": StatusView(store, clients),
        "/v1/display": DisplayView(store, test_pattern if config.screen == "testpattern" else composer.compose),
        "/v1/icon/": IconView(),
        "/healthz": HealthView(),
    }

    servers: list[MetricsHTTPServer] = []
    for host in binds:
        try:
            servers.append(MetricsHTTPServer((host, config.port), routes, allowed, clients))
        except OSError as exc:
            # Fail as a whole so systemd restarts us, rather than limping along on loopback only
            log.error("cannot listen on %s:%d: %s", host, config.port, exc)
            for server in servers:
                server.server_close()
            stop.set()
            return 1
    for server in servers:
        serve_in_thread(server)
    log.info(
        "smalltv-monitor %s serving %s; allowed clients: %s",
        __version__,
        ", ".join(f"http://{s.server_address[0]}:{s.server_address[1]}/metrics" for s in servers),
        "any" if allowed is None else ", ".join(map(str, allowed)),
    )

    stop.wait()
    for server in servers:
        server.shutdown()
        server.server_close()
    for runner in runners:
        runner.join(timeout=3.0)
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
