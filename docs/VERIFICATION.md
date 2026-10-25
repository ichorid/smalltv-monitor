# Verification report

Workstation: Ubuntu, Python 3.14, Intel desktop CPU, NVIDIA RTX GPU.
Device: SmallTV-Ultra, factory `Ultra-V9.0.50`. Addresses: `<pc-ip>` = workstation, `<device-ip>` = SmallTV.

## Before flashing (2026-10-24)

| Check | Result |
|---|---|
| Service starts (systemd user unit, enabled, `Linger=no`) | ✅ active, 33–48 MB RSS, 8 threads |
| `/metrics` valid JSON with all firmware fields and types | ✅ |
| CPU plausible: idle vs `vmstat`; 16-core busy-loop load | ✅ 99.6–99.9 % under load vs `vmstat` 100 % |
| RAM vs `/proc/meminfo` (total − available) | ✅ within 0.04 GiB over 25 samples |
| Disk usage collected (`/`) | ✅ 90.0 % (83.6 of 97.9 GiB). Note: root is nearly full |
| GPU util / VRAM / temp / power vs `nvidia-smi` | ⚠️ **NOT VERIFIED**: `nvidia-smi` itself fails with *Driver/library version mismatch* (kernel module and userspace library versions differ after a driver update; reboot pending). Reading code is unit-tested against a fake NVML. Re-run `scripts/verify.py` after rebooting. |
| GPU-failure path | ✅ live: `ok:true`, GPU fields `null`, `gpu_ok:false`, one log line, NVML retried with 1→30 s backoff |
| Latency (LAN address, 1000 sequential) | ✅ p50 0.50 ms, p99 1.05 ms, max 4.35 ms (firmware timeout 800 ms) |
| Burst: 5000 req from 16 / 64 concurrent clients | ✅ 100 % / 99.1 % 200s (rest shed by the concurrency cap by design); ~3700 req/s; RSS flat; still serving |
| Steady-state CPU of the service | ✅ 0.13 % of one core |
| Graceful shutdown (SIGTERM) | ✅ exit 0, "stopped" logged |
| Reachable from another LAN device | ✅ 100/100 fetches from the Wi-Fi access point, the SmallTV's path. Needed a temporary test instance allowing the AP's secondary source address (another subnet); the production allow-list correctly refuses it |
| Firewall | ✅ ufw disabled, nothing filters 8765. Rule documented for when it is enabled |
| Unit tests | ✅ 19/19 |
| Firmware build (v1.5.0 + patch, METRICS_URL=`http://<pc-ip>:8765/metrics`) | ✅ firmware.bin 457600 B, littlefs.bin 2072576 B |
| Image sanity | ✅ magic 0xE9, DIO, 4 MB header (same as factory), URL embedded, LittleFS superblock, config.json packed |
| Fits factory OTA | ✅ budget 536576 B (factory FS at 0x100000, factory app 505200 B), margin 78976 B |
| clang-tidy / compiler warnings on changed code | ✅ none (3 pre-existing upstream findings elsewhere) |

Bug found and fixed during testing: staleness was global (5 s) while disk is sampled every 10 s, so
disk fields flickered to `null`. Freshness is now per source (interval + 5 s grace), with a regression test.

## Installation (2026-10-24, approved)

| Step | Result |
|---|---|
| Pre-check `v.json` / `space.json` | SmallTV-Ultra, Ultra-V9.0.50, total 3121152 |
| Stage A: `firmware.bin` (sha256 `be73ebc3…`) → `http://<device-ip>/update` | `Update Success! Rebooting...`, 457820 B sent in 4.7 s; device left the LAN within 20 s |
| Stage B: `littlefs.bin` (sha256 `5b3c6ccb…`) via `flash-filesystem-via-ap.sh` (run by the user with sudo) | Completed; device rejoined the home network as `<device-ip>` |
| Workstation Wi-Fi state after Stage B | Radio disabled, rfkill blocked, temporary profile removed, Wi-Fi interface autoconnect `yes`, default route unchanged |

## After flashing

| Check | Result |
|---|---|
| Display polls `/metrics` | ✅ from `<device-ip>` |
| Update rate | ✅ 61 polls in 61.0 s (1.00/s) |
| Web UI | ✅ `/`, `/token.html` 200; `/legacyupdate` 404 (correct once LittleFS is populated) |
| API + auth | ✅ token valid; `/api/v1/ota/status` reachable with token (OTA path available), 401 without |
| Secrets migrated to EEPROM | ✅ on-device `config.json` now only holds `lcd_rotation` |
| Wi-Fi status | ✅ connected to the home network, `<device-ip>` |
| Service outage → recovery | ✅ service stopped 16 s, then display polled again 0.92 s after restart, no manual action |
| Screen: orientation, LIVE header, values updating, no flicker | ✅ confirmed by the user |
| PC OFFLINE shown during the 16 s outage | ⏳ not visually confirmed (recovery confirmed from poll log) |
| Workstation unreachable (connect timeout instead of refused, e.g. PC asleep) | ⚠️ not tested: needs a root firewall DROP rule. Same code path (3 failed polls → offline view) |
| Wi-Fi drop / AP restart recovery | ⚠️ not tested: would interrupt every client of the household AP. Test by reloading the AP's Wi-Fi when convenient |
| Device free heap over time | ⚠️ not observable: the open firmware logs heap to serial only (`/api/v1/logs` keeps warnings). Stability judged from poll gaps |
| Sustained operation | ✅ 30 min: 1800 polls (1.00/s), 0 gaps > 3 s, longest silence 1.0 s, so no reboots or hangs (`scripts/watch-display.py`) |
| GPU tiles | ⚠️ show 0 until the workstation reboots (NVML driver/library mismatch, see above) |

## Widget renderer (2026-10-25)

| Check | Result |
|---|---|
| Renderer firmware (`firmware-renderer-cdc3e1cf.bin`, 606,400 B) flashed via `POST /api/v1/ota/fw` | ✅ rebooted, polling `/v1/display` ~20 s after upload |
| Host tests (validation, hashing, geometry, icons, gradient) | ✅ 183 checks, 0 failures |
| Device validator on PC output (live, demo, error screen) | ✅ accepted |
| Device report: `err`, heap | ✅ `err` 0; 22.0 KB free (21.3 KB block) with the full dashboard |
| Icons fetched from `/v1/icon/<name>` | ✅ |
| Colours | ✅ after the BGR fix (first flash showed red/blue swapped: see FIRMWARE-RENDERER.md) |
| Night mode | ✅ `bri` 10 between 22:00 and 07:00 |
| Visual check of the dashboard after the fix (black background, gradient direction, icon colours, no flicker) | ✅ confirmed by the user |
| Sustained operation (renderer) | ✅ 15 min: 901 polls (1.00/s), 0 gaps > 3 s, uptime 1004 s without reboot, heap steady at 21.9 KB (start 22.0 KB), `err` 0 |
| Disk I/O sparkline (`firmware-renderer-io-f2b6c2c3.bin`, 607,280 B, spark `u` series, fw `-w2`) flashed via `POST /api/v1/ota/fw` | ✅ reports `-w2`, `err` 0, 23.7 KB free (23.0 KB block); 60 polls in 1 min, 0 gaps; `io` sent with 98 + 98 samples, screen 3564 B |
