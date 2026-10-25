# Changes to GeekMagic-Open-Firmware

Baseline: tag **v1.5.0** (`9d31738`, latest published release, 2026-07-21). Tag `v1.6.0` and `develop`
differ from it only in `.releaserc.json`, so there is nothing newer to pick up.
Branch: [`smalltv-metrics`](https://github.com/ichorid/GeekMagic-Open-Firmware/tree/smalltv-metrics) of the fork.
Full diff: `firmware/patches/0001-dashboard-offline-debounce-and-wifi-recovery.patch`
(apply with `git apply` on v1.5.0). 2 files, +36 / −7. `platformio.ini` is unchanged.

## Build configuration (no source change)

Passed via `PLATFORMIO_BUILD_FLAGS` by `firmware/build-firmware.sh`:

| Flag | Value | Effect |
|---|---|---|
| `METRICS_URL` | `"http://<pc-ip>:8765/metrics"` (`SMALLTV_PC_IP`, else auto-detected) | Enables upstream's dashboard |
| `WIFI_FALLBACK_REBOOT_MS` | `300000` | Enables change 3 below; without it, behaviour is upstream's |

`data/config.json` is generated from `data/config-smartTV.example` (`lcd_rotation: 0`) by
`make-device-config.py`; it is git-ignored upstream.

## Source changes and why

1. **Offline debounce** (`src/dashboard/DashboardManager.cpp`).
   Upstream repaints the full screen as PC OFFLINE on the *first* failed poll, and fully repaints the
   dashboard frame on the next success: a single dropped Wi-Fi packet produced two full-screen flashes.
   Now the offline view appears only after 3 consecutive failures (~3 s), and the last values stay up
   in between.

2. **Dashboard runs whenever the device is in station mode** (`src/main.cpp`).
   Upstream skipped `DashboardManager::update()` while Wi-Fi was disconnected, so a Wi-Fi outage left
   stale numbers on screen under a green LIVE header. `fetchMetrics()` already returns false without
   Wi-Fi, so polling now continues and the offline view appears. `DashboardManager::begin()` moved from
   `setup()` to the first loop iteration in station mode, so the dashboard also starts if station mode
   is reached after boot (Wi-Fi configured from AP mode). In AP mode the dashboard stays off, as before,
   so the AP/IP info screen used for setup is not covered.

3. **Recover from boot-time Wi-Fi failure** (`src/main.cpp`, only with `WIFI_FALLBACK_REBOOT_MS`).
   Upstream tries station mode for 10 s at boot, then stays in AP mode until power-cycled. After a power
   cut a typical access point takes longer than that to come up, so the display would never recover on its own.
   Now, if credentials are configured and no client is connected to the fallback AP for 5 minutes, the
   device reboots and retries. It never triggers during provisioning (no SSID yet) or while someone is
   using the AP. A `static_assert` keeps the interval above `BOOT_STABLE_MS` (20 s) so the reboots are
   never mistaken for a boot loop by Rescue Mode. `WiFi.persistent(false)` stops the ESP SDK from
   rewriting its flash copy of Wi-Fi settings on every STA↔AP flip. The firmware already stores
   credentials itself (SecureStorage/EEPROM), so nothing is lost.

## Verified unchanged

- The metric path does no flash writes. The only flash writers in the firmware are user actions
  (GIF upload/delete, config/token save via SecureStorage `EEPROM.commit`); Rescue Mode uses RTC RAM.
- Poll interval 1000 ms, HTTP timeout 800 ms, value-only redraws, JSON field names: as upstream.
- clang-tidy (upstream's `pio check` profile, warnings-as-errors) reports nothing in the changed code.
  The 3 findings it prints are upstream's pre-existing `char[]` buffers in `loop()` (commit `f915ec01`).
- 0 compiler warnings in changed files.
