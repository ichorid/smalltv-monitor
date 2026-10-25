# smalltv-monitor

Feeds live workstation metrics to a **GeekMagic SmallTV-Ultra** (ESP8266, 240×240 ST7789) running a fork of
the [Times-Z GeekMagic-Open-Firmware](https://github.com/Times-Z/GeekMagic-Open-Firmware):
[ichorid/GeekMagic-Open-Firmware](https://github.com/ichorid/GeekMagic-Open-Firmware), branch
`widget-renderer` (the screen is drawn from widgets this service sends) or `smalltv-metrics` (upstream's
metrics dashboard plus fixes).

```text
 workstation (static LAN address)                             SmallTV-Ultra (DHCP)
 ┌─────────────────────────────────────────┐                   ┌──────────────────────────────┐
 │ collector threads (own cadence each)    │                   │ Times-Z firmware v1.5.0 +    │
 │   cpu, memory 1 s · disk 10 s · gpu 1 s │  GET /metrics     │ patch (docs/FIRMWARE-CHANGES)│
 │        │ (psutil / NVML, no subprocess) │◄──── every 1 s ───│ DashboardManager, 800 ms     │
 │        ▼                                │  Wi-Fi via        │ timeout, redraws only        │
 │ SnapshotStore (RAM) ─► HTTP :8765       │  home Wi-Fi AP    │ changed values, no flash I/O │
 └─────────────────────────────────────────┘                   └──────────────────────────────┘
```

HTTP requests never touch a data source: they read the latest cached sample (and `/metrics` serves
pre-rendered bytes until something changes), so any number of requests costs zero GPU queries and a
hung driver cannot stall a response.

Layout: this directory is the workstation service plus the build, flash and check tools. The firmware is
a separate checkout of the fork (`FIRMWARE_DIR`); the same changes are kept here as patches on upstream
`v1.5.0` (`firmware/patches/`).

## Site settings

Everything specific to one installation (addresses, Wi-Fi network, paths) lives in `.env`, which is
git-ignored. Start from the template:

```bash
cp .env.example .env && chmod 600 .env   # then fill in SMALLTV_PC_IP, SMALLTV_DEVICE_IP, SMALLTV_WIFI_SSID, …
```

The tools read it (`smalltv_monitor/siteenv.py`, `scripts/site-env.sh`); variables set in the environment
win, and empty values fall back to detection (LAN address, Wi-Fi interface, the polling display). The
Wi-Fi password is never stored: `make-device-config.py` prompts for it. The service itself needs no site
settings: it detects its LAN address at start. Below, `<pc-ip>`, `<device-ip>` and `<lan-subnet>` stand for
your values.

## Service

```bash
./scripts/install.sh                 # venv from hash-pinned requirements.txt, run tests, install + start user unit
systemctl --user status smalltv-monitor
journalctl --user -u smalltv-monitor -f
.venv/bin/python scripts/verify.py --url http://<pc-ip>:8765/metrics   # compare with nvidia-smi and /proc
```

The unit is a **user** service (`~/.config/systemd/user/smalltv-monitor.service`, `WantedBy=default.target`):
it starts with your login session. Lingering is *not* enabled, so it does not run while you are logged
out; `loginctl enable-linger $USER` would change that.

Dependencies: `psutil`, `nvidia-ml-py` (NVML bindings; pure Python). Python 3.11+ (uses `tomllib`).
Regenerate pins with `uv pip compile --generate-hashes requirements.in -o requirements.txt`.

Configuration is optional: copy `config.example.toml` to `~/.config/smalltv-monitor/config.toml`.
CLI flags (`--port`, `--bind`, `--allow`, `--gpu none`, `--log-level DEBUG`, …) override the file.

### Endpoints

| Path | Purpose |
|---|---|
| `GET /metrics` | Flat payload parsed by the firmware. Stable contract, below. |
| `GET /v1/display` | Full screen for the widget-renderer firmware ([protocol](docs/WIDGET-PROTOCOL.md)). The device's health query string (`heap`, `blk`, `up`, `rssi`, `err`, `fw`) is recorded as the `display` source. |
| `GET /v1/status` | Every source with `ok`, `age_s`, `error`, `data`, plus which clients polled and when. For new consumers. |
| `GET /healthz` | Liveness. |

`/metrics` fields read by the firmware: `ok`, `gpu_usage` (%), `cpu_usage` (%), `gpu_vram_mb` (MiB),
`memory_used_gb` (GiB, total − available), `gpu_power` (W), `gpu_temp_c`. Extras ignored by the
firmware: `gpu_ok`, `memory_total_gb`, `gpu_vram_total_mb`, `disk_used_pct`, `disk_used_gb`, `ts`.

Failure semantics:
- `ok` is false only when CPU or RAM data is missing or stale; the display then shows **PC OFFLINE**.
- GPU unavailable (driver mismatch, reset, NVML error): GPU fields are `null` (the display shows 0),
  `gpu_ok` is false, and NVML reconnects with exponential backoff (1 → 30 s) without a restart.
- A source is stale after its own interval + `stale_after` (5 s) without a fresh sample.
- Responses are HTTP/1.0 with `Content-Length` (the ESP8266 client cannot de-chunk).

### Network and firewall

- Workstation `<pc-ip>`: give it a static address (manual, or a DHCP reservation outside the pool) so nothing
  else can take it. The firmware has it compiled in; if it ever changes, rebuild and re-flash firmware.bin
  (OTA, no AP step).
- SmallTV `<device-ip>`: DHCP on a 2.4 GHz network. A DHCP reservation is optional; it only keeps the web UI
  address stable.
- The service listens on the LAN address and loopback only, and itself refuses clients outside
  loopback + `<lan-subnet>`. (A device talking from another subnet, such as an access point's secondary
  address, is refused by design.)
- With ufw enabled, allow the LAN:
  `sudo ufw allow from <lan-subnet> to any port 8765 proto tcp comment smalltv-monitor`
- Never port-forward 8765 on the router.

## Firmware

```bash
firmware/make-device-config.py           # SSID from .env (SMALLTV_WIFI_SSID); prompts for the Wi-Fi password (hidden)
firmware/build-firmware.sh               # metrics dashboard (env:esp12e): builds + verifies, never flashes
firmware/build-firmware.sh --renderer    # widget renderer (env:renderer, FIRMWARE_DIR on branch widget-renderer)
```

`make-device-config.py` writes `data/config.json` (0600, git-ignored) and a random API token to
`~/.config/smalltv-monitor/device-token` (0600). Neither secret is printed. `build-firmware.sh` passes
`-DMETRICS_URL=\"http://<pc-ip>:8765/metrics\" -DWIFI_FALLBACK_REBOOT_MS=300000` (or `DISPLAY_URL` with
`--renderer`; `<pc-ip>` is `SMALLTV_PC_IP`, else the address carrying the default route) through
`PLATFORMIO_BUILD_FLAGS` (upstream `platformio.ini` stays untouched), then checks: ESP8266 image magic,
DIO / 4 MB header, size within the factory OTA budget, URL compiled in, LittleFS size and superblock,
`config.json` packed.

Outputs: `.pio/build/esp12e/firmware.bin` and `littlefs.bin`. **littlefs.bin holds the Wi-Fi password and
API token in plain text** (the firmware moves them to EEPROM on first boot): keep it 0600, delete it after flashing.

Changes to upstream: [docs/FIRMWARE-CHANGES.md](docs/FIRMWARE-CHANGES.md)
(patch: `firmware/patches/0001-dashboard-offline-debounce-and-wifi-recovery.patch`) and
[docs/FIRMWARE-RENDERER.md](docs/FIRMWARE-RENDERER.md) (`0002-widget-renderer.patch`).

Firmware images are not kept in the repository (they have `<pc-ip>` compiled in): keep your own builds,
including the one to roll back to, in `firmware/releases/` (git-ignored).

## Installation (first time, from factory firmware)

Pre-flight, all read-only:
1. `curl http://<device-ip>/v.json` → `{"m": "SmallTV-Ultra","v":"Ultra-V9.0.50"}`. Any other model: stop.
2. `curl http://<device-ip>/space.json` → `total` 3121152 (factory LittleFS at 0x100000). This bounds the OTA
   image to 536576 bytes; `build-firmware.sh` enforces it.
3. Back up photos/GIFs/settings over HTTP (for example into `device-backup/`, which is git-ignored).
4. Keep the device on stable power for the whole procedure.

**Stage A: firmware, over the LAN through the factory updater**
```bash
curl -F "firmware=@$FIRMWARE_DIR/.pio/build/esp12e/firmware.bin" http://<device-ip>/update
```
Expect `Update Success! Rebooting...`. The device reboots into the open firmware, finds no usable
filesystem at its 0x200000 offset, formats an empty one, and opens AP `GeekMagic` / `$str0ngPa$$w0rd`
(it has no Wi-Fi credentials yet). It leaves the LAN. Screen orientation may be wrong until Stage B.

**Stage B: filesystem, through the device's own AP** (uses the workstation's Wi-Fi, `SMALLTV_WIFI_IF` or
detected; it may normally be off)
```bash
firmware/flash-filesystem-via-ap.sh --check   # preflight, no changes, no root
sudo firmware/flash-filesystem-via-ap.sh      # join GeekMagic, upload littlefs.bin, restore Wi-Fi state
```
The script turns off Wi-Fi device autoconnect (runtime only) before enabling the radio, so saved profiles
such as your home network cannot join and add a second LAN address. It uses a temporary profile with no default
route, DNS or IPv6, pins curl to the Wi-Fi interface, aborts if the default route changes, and on exit (including errors
and Ctrl-C) deletes the profile and restores autoconnect, radio and rfkill state. Then it waits for the display
to show up as a `/metrics` client of the service.
After reboot the device joins your network, moves the secrets from `config.json` into EEPROM, and starts
polling. Check with `curl -s localhost:8765/v1/status | jq .clients` (look for `<device-ip>`, `age_s` < 2).
Web UI: `http://<device-ip>/` → token page → paste `~/.config/smalltv-monitor/device-token`.

Later firmware updates need no AP step: rebuild, then upload firmware.bin in the web UI (OTA page, or
`POST /api/v1/ota/fw` with the bearer token).

## Recovery

| Situation | What to do |
|---|---|
| Wrong Wi-Fi password / router down at boot | Device falls back to AP `GeekMagic`. With credentials configured it reboots every 5 min to retry while nobody is connected to the AP (patch). To fix credentials: join the AP, open `http://192.168.4.1/`. |
| Display says PC OFFLINE | `systemctl --user status smalltv-monitor`; `curl http://<pc-ip>:8765/metrics` from another machine. Recovers by itself within ~1 s of the service answering. |
| GPU tiles show 0 | `nvidia-smi` failing (e.g. "Driver/library version mismatch" after a driver update): reboot the workstation. The service reconnects on its own. |
| Boot loop after an update | Firmware enters **Rescue Mode** automatically (RTC boot counter): AP `GeekMagic`, `http://192.168.4.1/`, OTA upload at `POST /api/v1/rescue/ota`, no auth. |
| Filesystem lost/corrupt | `/legacyupdate` reappears when LittleFS is empty: repeat Stage B. |
| Back to factory firmware | **Requires UART** (USB-TTL adapter, GPIO0 to GND at power-on; wiring in upstream `backup/readme.md`). Uploading the official `FW-Smalltv-Ultra-V9.0.xx.bin` over OTA is rejected ("Upload error", upstream #34), and forcing it via the rescue API bricked a unit (black screen). No UART dump of *this* unit exists. Options: upstream's 9.0.40 full dump (`esptool.py write_flash 0x0 9.0.40_backup_full.bin`; one other Ultra owner reported a boot loop after Wi-Fi setup with it, #28), or flash the official 9.0.50 app at 0x0 over UART and let it rebuild its filesystem (untested). |

Before any further experiment, take a full UART dump: `esptool.py --port /dev/ttyUSB0 read_flash 0 0x400000 smalltv-full.bin`.

## Display screens (`/v1/display`)

The screen is composed in Python (`smalltv_monitor/display/dashboard.py`): layout, colours, warning
thresholds and night-mode brightness (`day_bri`, `night_bri`, `night_start`, `night_end` in the config).
Each poll gets the complete screen (≤ 4096 bytes, enforced; sparklines are trimmed if needed).

Rows are marked with 16 px icons ([sources and licences](docs/ICONS.md)); values line up on two right edges.

| Row | Source | Notes |
|---|---|---|
| Slack icon (top right) | Slack's tray icon (`slack.py`) | Slack's own tray image: no dot / blue unread dot / red mention dot. Faint when Slack isn't running. Local only: D-Bus StatusNotifierItem → icon PNG → badge colour. |
| Claude / Codex 5H, WEEK | Alloy's limits cache (`agent_limits.py`) | **Remaining** allowance: a full green bar = 100 % left. Green from 50 % left, yellow 25–49 %, red below 25 %. Claude via its OAuth usage API, Codex from local session logs. Last good values stay up, coloured the same, while a probe fails (Claude often answers HTTP 429). |
| Cursor | Alloy's limits cache | Cursor only exposes a billing-cycle percentage (no 5 h / weekly windows): remaining share of the cycle, plus time to reset. |
| CPU | psutil | Total load sparkline, last 2 minutes, coloured by height from deep red (bottom) to yellow (top). The number goes blue → yellow → red with load. |
| Memory | psutil | htop's memory meter: used (green), shared (magenta), buffers (blue), cache (yellow); used/total GiB. |
| Disk | psutil | Free space on `/` and `/home` (mount left, value right); yellow below 10 % free, red below 5 %. |
| GPU | NVML | Power draw / enforced limit; VRAM used. |
| top right, next to Slack | — | Reserved 18×18 px slot for the "agent requires input" icon (not wired up yet). The bottom 28 px are free. |

The Alloy cache (`~/.alloy/limits.json`) is refreshed every 60 s by the Alloy monitor. If it is older
than 5 minutes the service runs `alloy limits --json` itself (at most every 5 minutes), so the display
keeps working without the monitor. Claude and Cursor numbers come from their cloud APIs (as in Alloy);
everything else is local.

Design without flashing anything:

```bash
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt   # Pillow, preview only
.venv/bin/python scripts/preview.py --demo -o /tmp/demo.png                 # sample data for every row
.venv/bin/python scripts/build_icons.py                                     # regenerate icons (cairosvg)
.venv/bin/python scripts/preview.py --url http://127.0.0.1:8765/v1/display -o /tmp/live.png
```

The preview warns when a text doesn't fit its cell (cell widths are sized for the worst cases).

Images are not kept in the repository; render the demo screen with `scripts/preview.py --demo`.

## Extending

Data sources are `Collector` subclasses (`smalltv_monitor/collectors.py`): implement `collect() -> dict`,
optionally `open()`/`close()`, register in `__main__.build_collectors`. Each gets its own thread and
cadence, appears in `/v1/status` automatically, and is reported unavailable when it raises
`SourceUnavailable` or goes stale. Push-style sources (Claude Code / Codex hooks, Slack) can write to
the same `SnapshotStore` from a small POST endpoint bound to loopback.

`/metrics` is the compatibility adapter for the current six-tile firmware. The widget-renderer
firmware (in development) reads `/v1/display` instead, so new data sources and redesigns are
Python-only: add a collector, then use it in `display/dashboard.py`. Alerts are just layouts the
composer builds (e.g. a banner panel over the top of the screen).

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```
Covers the firmware JSON contract, staleness per source, render caching, NVML failure/backoff/recovery
(fake NVML), runner resilience, HTTP/1.0 framing, concurrency shedding and the client allow-list.

## Licence and third-party artwork

GPL-3.0 ([LICENSE](LICENSE)), like the firmware it patches.

The display icons come from third parties ([docs/ICONS.md](docs/ICONS.md)). In particular, the Slack tray
icons (`slack-rest`, `slack-unread`, `slack-mention` in `smalltv_monitor/display/icon_data.py`) are taken
from the Slack desktop app: Slack, its logo and its artwork are trademarks and copyright of Slack
Technologies, LLC, a Salesforce company. They are included only to show Slack's status on this display and
are not covered by this repository's licence. The Claude, OpenAI and Cursor marks are trademarks of their
respective owners.
