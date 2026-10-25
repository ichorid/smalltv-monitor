# Graphics library lab (2026-10-24)

Question: can the SmallTV-Ultra (ESP8266, ~26 KB free heap at idle, no frame buffer possible) draw an
anti-aliased widget UI fast enough for a server-driven renderer, and with which library?

Setup: a local branch `gfx-lab` (not published) = v1.5.0 + production patch +
`src/lab/*` + `[env:gfxlab]` in `platformio.ini`. Full firmware (Wi-Fi, web UI, OTA, Rescue Mode)
with an extra `GET /api/v1/gfxlab?lib=tft|lgfx|agfx&scene=bars|dash|anim`. Nothing ran at boot.
Flashed over the air (`POST /api/v1/ota/fw`), then production restored from
a local build of `smalltv-metrics` (sha256 `be73ebc3…`). Both directions worked.

Scenes: `bars` = colour/orientation check; `dash` = 2 ring gauges + 4 rounded bars + VLW smooth fonts
(NotoSansBold 15/36); `anim` = 201 incremental updates (arc segment + number + one bar).

| | TFT_eSPI 2.5.43 | LovyanGFX 1.2.34 | Arduino_GFX 1.6.9 (current, no AA) |
|---|---|---|---|
| Works on this panel | ✅ (standard ST7789 init, SPI mode 3, inversion on, RGB) | ❌ nothing usable on screen (not investigated) | ✅ |
| Anti-aliased arcs | ✅ `drawArc(..., smooth=true)`; looked great | no AA arc in the API | no |
| Init | 571 ms | 228 ms | 526 ms (vendor init) |
| Heap for library object | ~0.3 KB | ~0.5 KB | 0 |
| Full AA dashboard draw | 113 ms | 113 ms | 86 ms |
| Incremental update | 9.5 ms avg, 12.3 ms max | 8.9 / 11.8 ms | 5.7 / 9.4 ms |
| Lowest free heap while drawing | 24.5 KB | 24.2 KB | 25.3 KB |
| Web server latency right after a run | ~10 ms | ~10 ms | ~10 ms |

Static RAM of the lab image: +2.1 KB vs production. Flash: +150 KB for both libraries and two fonts
(608 KB of 1044 KB). Loading/unloading a VLW font costs ~1.2 KB transiently.

Visual result (checked on the device): TFT_eSPI `dash` and `anim` "look great", with smooth gauges and no
flicker. In the `bars` scene the legacy bitmap Font 2 "M" lacked its right leg. "W" has the same 10 px
advance and rendered fine, so this is unexplained but irrelevant for a renderer that uses VLW fonts.

Conclusion: use **TFT_eSPI** for the widget renderer. Speed and memory are not constraints: a full AA
redraw is ~0.1 s and a widget update ~10 ms. Keep `TFT_CS` undefined (CS is tied low on this board;
`-1` would make TFT_eSPI toggle a bogus GPIO mask).

## Parse benchmark (2026-10-24)

Question: can the device fetch, parse and change-detect a full `/v1/display` screen every second?
Lab endpoint `GET /api/v1/gfxlab/parse?url=…&n=20&mode=stream|buffer` (branch `gfx-lab`,
`src/lab/LabParse.cpp`); payloads from `lab/make_payloads.py`, served by a temporary `http.server`
on the workstation. 20 iterations each. Idle free heap 25.4 KB. Hash = FNV-1a of each widget's JSON.

| Payload | Mode | Fetch | Parse | Hash all widgets | Total | Lowest free heap | Largest block |
|---|---|---|---|---|---|---|---|
| 1.2 KB, 8 widgets | stream | 8.9 ms | 13.9 ms | 3.1 ms | 26 ms | 21.8 KB | 20.4 KB |
| 2.2 KB, 16 widgets | stream | 8.6 | 26.7 | 5.9 | 41 ms | 19.7 KB | 18.6 KB |
| **4.4 KB, 32 widgets** | **stream** | 8.7 | 50.6 | 11.6 | **71 ms** | **15.4 KB** | **13.0 KB** |
| 4.4 KB, 32 widgets | buffer | 40.6 | 14.3 | 11.6 | 67 ms | 11.0 KB | 10.6 KB |
| 7.4 KB, 24 widgets | stream | 9.0 | 82.3 | 20.9 | 112 ms | 10.7 KB | 7.7 KB |
| 7.4 KB, 24 widgets | buffer | 61.1 | 20.7 | 21.0 | 103 ms | 3.3 KB | 2.9 KB |
| 9.8 KB, 32 widgets, all limits maxed | stream | 8.3 | 108.9 | 27.8 | 145 ms | 6.3 KB | 2.9 KB |
| 9.8 KB, 32 widgets, all limits maxed | buffer | | | | | | **NoMemory (20/20), handled, no crash** |

Findings:
- 240 runs, 0 crashes; the heap returned to its idle level afterwards (no leak, 4 % fragmentation).
- Parsing straight from the socket ("stream") is as fast overall as buffering the body (reading the
  network dominates either way) and needs ~4 KB less peak memory. Use stream.
- Peak memory ≈ 2.3 bytes of heap per byte of JSON in stream mode.
- At 4.4 KB a poll costs ~71 ms and leaves ≥ 15 KB free (13 KB largest block). That is the right cap.
  At 7–10 KB the largest free block drops to 3–8 KB: too close to fragmentation failures once fonts
  and drawing also need memory.
- Change detection by hashing every widget costs ~0.36 ms per widget (11.6 ms for 32): acceptable.
- The draft's per-widget limits combined (32 widgets × 4 segments, 32-bar groups, 120-sample
  sparklines) produce 9.8 KB, which contradicts the 4 KB response cap. The total size is the
  binding limit.
