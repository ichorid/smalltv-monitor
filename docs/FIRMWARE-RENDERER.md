# Widget renderer firmware (device side of `/v1/display`)

Implements `docs/WIDGET-PROTOCOL.md` (draft v1 as of 2026-10-25: rendering conventions, `bar.radius`, PC icons
in mono and palette formats, `value.label`, sparkline height gradient `grad`/`fa`) on the SmallTV-Ultra.
Status: running on the device (see On-device findings below).

- Branch: [`widget-renderer`](https://github.com/ichorid/GeekMagic-Open-Firmware/tree/widget-renderer) of the fork (from tag v1.5.0).
- Patch: `firmware/patches/0002-widget-renderer.patch` = `git diff v1.5.0` of that branch. It **includes
  the 0001 changes**, so apply it alone on a clean v1.5.0 (`git apply`), not on top of 0001. It is ~550 KB
  because it carries the three VLW font arrays.

## Build

```sh
firmware/build-firmware.sh --renderer   # FIRMWARE_DIR on branch widget-renderer; DISPLAY_URL from SMALLTV_PC_IP
"$FIRMWARE_DIR"/test/host/run.sh          # host test of validation, hashing, change detection, geometry (g++, ASan/UBSan)
```

| Flag | Effect |
|---|---|
| `DISPLAY_URL` (required by `env:renderer`) | Poll this URL instead of running `DashboardManager`. No fallback dashboard. `#error` if missing, or if set outside `env:renderer` |
| `WIFI_FALLBACK_REBOOT_MS` | Unchanged from 0001 |

`env:renderer` extends `esp12e`, adds `bodmer/TFT_eSPI@2.5.43` with the lab's pin and SPI flags (`TFT_CS` left
undefined), `SMOOTH_FONT`, no bitmap fonts, and drops `src/dashboard/` from the build. `env:esp12e` is
unchanged and still builds (RAM identical, flash +64 B from the DisplayManager hooks below).

Measured from the final build (2026-10-25):

| | esp12e (production) | renderer |
|---|---|---|
| Static RAM | 42 992 B (52.5 %) | 46 448 B (56.7 %), **+3.4 KB** |
| Flash | 441 639 B (42.3 %) | 602 175 B (57.7 %), +161 KB |
| firmware.bin | 445 792 B | 606 320 B (OTA slot is 1 MB) |

Flash: fonts 70 KB (NotoSansBold15 10.8 KB, NotoSansBold36 44.2 KB, NotoSansMono-Bold 20 15.1 KB), the rest is
TFT_eSPI and the renderer. Static RAM: the two frame tables are 800 B (`.data`), the icon cache index 384 B;
the rest is mostly `.rodata` (JSON keys, log strings, library tables), which the ESP8266 keeps in DRAM.

## Files

| File | Role |
|---|---|
| `include/renderer/WidgetModel.h`, `src/renderer/WidgetModel.cpp` | Plain C++ on ArduinoJson (no Arduino.h): limits, validation, FNV-1a hashing, change detection, colour/UTF-8/number helpers, ring/bars/spark geometry. Compiled by the host test too |
| `include/renderer/WidgetPainter.h`, `src/renderer/WidgetPainter.cpp` | TFT_eSPI drawing of the 8 widget types, font cache, clipping, waiting screen, offline dot |
| `include/renderer/RendererManager.h`, `src/renderer/RendererManager.cpp` | Poll loop, HTTP, heap-capped parse, frame tables, offline state, backlight PWM |
| `include/renderer/IconCache.h`, `src/renderer/IconCache.cpp` | RAM LRU cache of PC icons (16 entries), one `GET …/v1/icon/<name>` per poll at most |
| `include/renderer/Fonts.h`, `src/renderer/Fonts.cpp`, `src/renderer/fonts/*.h` | VLW arrays: 0 = NotoSansBold15, 1 = NotoSansBold36 (from the lab), 2 = NotoSansMonoBold20 (generated) |
| `scripts/make_vlw.py` | Reproducible TTF → VLW generator (Pillow). Font 2: `python3 scripts/make_vlw.py /usr/share/fonts/truetype/noto/NotoSansMono-Bold.ttf 20 NotoSansMonoBold20 src/renderer/fonts/NotoSansMonoBold20.h` |
| `test/host/run.sh`, `test/host/widget_model_test.cpp` | Host test (183 checks) |
| `src/main.cpp` | `#ifdef DISPLAY_URL`: start `RendererManager` lazily in station mode, as 0001 does for the dashboard |
| `src/display/DisplayManager.{h,cpp}` | `drawEpoch()` (bumped by every DisplayManager drawing call) and `isGifPlaying()` |
| `platformio.ini` | `[env:renderer]` |

The VLW generator follows TFT_eSPI's loader (`Extensions/Smooth_font.cpp`): 24-byte header, 28 bytes of
metrics per glyph, 8-bit alpha bitmaps. Regenerating NotoSans-Bold 15/36 with it matches the shipped TFT_eSPI
arrays within 1–2 px per glyph box (Processing's rasteriser pads AA rows). Font 2 also stores U+0020 as an empty
glyph; the renderer reads its advance after loading, because TFT_eSPI otherwise guesses the space width
(which would break the mono columns).

## How a poll works

Every `screen.poll_ms` (default 1000, clamped to 250–60 000):

1. `GET DISPLAY_URL?fw=<git describe>-w1&heap=&blk=&up=&rssi=&err=` over HTTP/1.0 (`useHTTP10`), 800 ms timeout.
2. No 200 response → transport failure. 200 → `Content-Length` checked **before** parsing: missing or
   > 4096 → `err=2`, previous frame kept.
3. Streaming parse from the socket through a reader that stops at `Content-Length` (a lying server cannot
   make it read more), into a `JsonDocument` whose allocator refuses any block that would leave less than
   6 KB of free heap (→ `NoMemory`, `err=4`, no crash, no restart).
4. Validation (`WidgetModel.cpp`): `proto == 1`, screen fields, ≤ 32 widgets, unique non-empty ids, known
   types, integer rects inside 240×240, no overlaps, fonts 0–2, `#rrggbb` colours, strings ≤ 31 characters
   (code points), ≤ 4 segments, ≤ 32 bars, ≤ 120 numeric spark samples, `dp` 0–4, `a`/`dir`/`show` enums,
   `max > 0` (spark: `max > min`). Unknown extra fields are ignored. Any violation → keep the previous frame,
   report `err`, log once per change of error.
5. Per widget: 12-byte slot {FNV-1a(id), FNV-1a(rect + own `bg`), FNV-1a(serialised widget JSON)} in a fixed
   table of 32. Two tables (on screen / incoming), swapped after drawing: no per-poll heap besides the document.
6. Drawing:
   - **Full redraw** (clear to `screen.bg`, draw all) when the set of (id, rect, widget `bg`) differs from what
     is on screen, `screen.bg` changed, it is the first frame, or something else drew on the panel since
     (`DisplayManager::drawEpoch()` changed, a GIF played, an OTA upload ran).
   - Otherwise **only widgets whose content hash changed** are redrawn, in place (see below), plus widgets
     under the offline dot when it is removed.
7. `screen.bri` applied if it changed.
8. If a PC icon was missing while drawing, that one icon is fetched now (see Icons).

`err` codes (numeric, the error of the previous poll, 0 = it was fine):
1 http, 2 size, 3 json, 4 nomem, 5 proto, 6 screen, 7 widgets (missing/too many), 8 id, 9 type, 10 rect,
11 overlap, 12 font, 13 colour, 14 value, 15 string, 16 field.

## Drawing

Geometry follows the protocol's rendering conventions (ring R, T, label at cy + 0.72 R, 135°→405° track;
bar label in the left 48 px / bottom 16 px; bar track corners `radius` (default 0), a single value filled with
the same radius, two or more segments square; bars `floor((extent − gap(n−1))/n)`, square; spark mapping;
`value` + verbatim `unit`, with `label`: label left in `lc`, value right-aligned; defaults
`tc`/`lc`/`track`/`dp`/`f`). Widgets are clipped to their rect with TFT_eSPI viewports.

Content-only redraws avoid a blank frame: text is drawn with its cell background after filling only the strips
around it; a ring repaints its number band and label band, then the arcs over them (the arc never blanks);
bars and sparklines repaint column by column (background above, fill/track below). Icons and panels clear
their rect first. Because a widget's own `bg` is part of its geometry key, in-place repaints can rely on an
unchanged background.

Sparkline gradient (`grad`, 2–8 strictly ascending stops; `fa` fill percent, default 35): each pixel row maps
back to a value (inverse of the y mapping); its colour is interpolated linearly in RGB888 between the stops
(clamped to the end colours), then converted to RGB565. Two per-row tables (line colour, and line colour
blended over the background at `fa` %) are built once per draw. Each column is composed in a buffer
(background above, the 2 px line, fill below) and pushed with one `pushImage` window. The line spans halfway to
the neighbouring columns' y so it stays connected (not anti-aliased, unlike the plain sparkline). `c` and
`fill` are ignored with `grad`. Scratch memory is one short-lived heap block of 2·(3·h + n) bytes: at most
1 680 B (240-row LUTs 960 B + 480 B column buffer + 240 B for 120 sample positions); if it can't be
allocated, the plain sparkline is drawn instead.

A rounded single-value bar is painted as fill body, then the fill's rounded end over the track, then the rest
of the track, so only the few pixels at the fill's end are repainted twice.

Fonts are loaded on first use in a pass and unloaded at its end (~1.2 KB of metric tables); text is filtered to
glyphs present in the loaded font (TFT_eSPI would draw a box for the others).

Coexistence with the rest of the firmware (Arduino_GFX for boot screens, OTA progress, GIFs): TFT_eSPI is
created when the renderer starts; afterwards Arduino_GFX re-applies the configured rotation (incl. mirrored
4–7) since the panel is square without RAM offsets. Each drawing pass resets TFT_eSPI's cached address window
first and Arduino_GFX's afterwards, so neither library draws through a stale window. The renderer pauses while
a GIF plays or an OTA upload runs, and does a full redraw afterwards.

## Icons

- Built-in `dot`, `warn`, `check`: drawn natively at min(w, h), no `h` needed (a PC icon with one of these
  names is never fetched).
- Any other name needs `h` (uint32), else the screen is rejected (`err=16`). Cache key = (FNV-1a(name), `h`);
  a new `h` for a known name replaces that entry; when full, the least recently used of 16 is evicted. Bodies
  live on the heap (≤ 342 B: 24×24 with a 16-entry palette) and are only stored while ≥ 8 KB heap stays free.
- Miss: the rect stays cleared, the first missing icon of the poll is requested, and the widget's stored hash
  is perturbed so it is redrawn on the next poll. After the frame, one `GET <DISPLAY_URL up to its last
  "/v1/display">/v1/icon/<name, percent-encoded>` (HTTP/1.0, 800 ms) is made; `Content-Length` must be 5–512.
  A failed (name, h) is not requested again for 30 s. Icon errors are logged only, not reported in `err`.
- Formats: `0x01` mono (4-bit alpha, colour `c`) and `0x02` palette (`n` ≤ 16 entries of RGB565 hi, lo,
  alpha; 4-bit indices, an index ≥ `n` is transparent; `c` ignored). Drawn centred, unscaled, clipped to the
  rect; each row is blended (`alphaBlend`) over the widget background into a 24-pixel line buffer and pushed
  with `pushImage` (one address window per row, no per-pixel `drawPixel`).

## Offline, brightness, startup

- Before the first valid frame: "Waiting for PC", the device IP and the URL (TFT_eSPI, font 0).
- 3 consecutive transport failures (no Wi-Fi, connect/timeout, non-200) → last frame kept, red dot in the
  top-right corner (`[228, 0, 12, 12]`). After `offline_dim_s` since the last valid frame, brightness drops to
  `offline_bri` (last received values, defaults 300 s / 5). The next valid frame removes the dot (repaints the
  widgets under it) and restores `bri`.
- Backlight: GPIO5, active-low, `analogWrite` at 1 kHz with range 1000, linear in `bri`; written only on change,
  never to flash. DisplayManager only `digitalWrite`s the pin in `begin()` during `setup()`, before the
  renderer starts, so the two never fight; the backlight stays fully on until the first valid frame.
- No flash writes in the poll/render path. `yield()` after parsing, between widgets and inside long widgets.

## Memory budget

Idle heap should be about the lab image's 25.4 KB minus ~1.3 KB (this image has +3.4 KB static RAM vs the
lab's +2.1 KB) minus ~0.3 KB for the TFT_eSPI object: **~23.8 KB (estimate, to be confirmed on device via
the `heap`/`blk` query parameters)**. Per poll, the lab measured ~2.3 B of heap per JSON byte in streaming
mode, so a 4 KB screen peaks at ~10 KB while the socket is open; the HTTP client is closed before drawing,
then a font adds ~1.2 KB: worst case ≈ 12.5 KB free while drawing a maximal screen, minus what the icon cache
holds (~2.1 KB for 16 mono 16×16 icons, 5.5 KB worst case for 16 maximal colour icons, never stored below
8 KB free). The allocator's 6 KB reserve turns a document that doesn't fit into `err=4` instead of starving
Wi-Fi or the web server. An icon download uses a 512 B stack buffer after the screen's document is freed; a gradient sparkline
borrows ≤ 1.7 KB of heap while it draws.
Stack use is small (largest
locals: 256 B URL, 256 B rect list during validation, two 192 B text buffers while drawing).

## Quality gates (2026-10-25)

1. `pio run -e renderer` with the flags above: success, sizes above.
2. Compiler warnings: none in added or changed files. Remaining warnings are in libraries (AnimatedGIF
   `gif.inl` unused function, also in production; TFT_eSPI `Smooth_font.cpp/.h` SPIFFS deprecation when
   compiling `TFT_eSPI.cpp`). `DISABLE_ALL_LIBRARY_WARNINGS` silences TFT_eSPI's `TOUCH_CS` `#warning`.
3. `pio check -e renderer --skip-packages --src-filters="+<src/renderer/*.cpp> +<src/main.cpp> +<src/display/DisplayManager.cpp>"`:
   0 findings in `src/renderer` (incl. `IconCache.cpp`). The 8 remaining are upstream's, unchanged lines: `main.cpp` 274–276
   (`char[]` buffers, as noted for 0001) and 5 `convert-member-functions-to-static` in `DisplayManager.cpp`
   (identical on pristine v1.5.0). Note: the bundled clang-tidy cannot find its own resource headers
   (`stddef.h`), so it parses ArduinoJson in error-recovery mode and misreports `obj["key"]` as
   `readability-misplaced-array-index` and `override`s as static-able. Those are suppressed with commented
   NOLINTs (upstream does the same); a run with a working `-resource-dir` reports nothing in `src/renderer`.
4. `pio run -e esp12e` (no `DISPLAY_URL`): success, RAM 42 992 B, flash 441 639 B.
5. `test/host/run.sh`: 183 checks pass under ASan/UBSan (spec example, change detection incl. reorder and
   widget `bg`, every rejection class, bounded reader vs a short `Content-Length`, geometry, formatting, both
   icon formats, icon URL derivation, `bar.radius` and `value.label` validation, gradient validation, stop
   clamping, exact stop colours, midpoints, and the row↔value inverse mapping).

## Known limitations

- Not run on hardware yet: colours, AA quality, flicker and real heap numbers need the on-device check.
- While the PC host is unreachable, each poll can block up to the 800 ms connect timeout (web UI slower
  while offline; watchdog unaffected).
- `bri` is linear (no gamma); low values may look brighter than expected.
- `err` reports only the last poll; the widget index of a validation error is only in the device log.
- After foreign drawing (rotation change, GIF stop, Wi-Fi reconnect screen) while offline, the old frame can't
  be restored until the PC answers again (the JSON is not kept).
- Icon loading costs one extra request per poll while icons are missing; a screen with many new icons fills in
  one icon per poll (16 icons ≈ 16 s).
- Not drawn on hardware: palette byte order of PC icons (`pushImage` with `setSwapBytes(true)` on native
  RGB565) is the part most worth checking first.

## Spec issues found (worked around as described)

1. **Alerts vs. no-overlap rule**: "a red `panel` plus two `text` widgets across the top 60 px" cannot be
   valid if the texts sit on the panel, since overlaps are rejected. Workaround for the PC: give the text
   widgets `bg` = the alert colour and tile the area, using `panel` only where no text is.
2. **`err` format**: the request example has `err=0`, the limits table says `err=size`. The device sends a
   **number** (table above).
3. **Unspecified defaults** (device choice, PC preview should match): `max` = 100 for bar/bars/ring; spark
   `max` = largest sample when absent; `bars` `dir` = `v` (bar: `h`); `gap` = 1; `a` = `l` for `value` too;
   `c` = `#ffffff` for every type (also `panel`); `screen.bri` = 100, `screen.bg` = `#000000`.
4. **`null` details**: `value: null` draws `--` without the unit; `bars: null` draws only the background
   (the bar count is unknown); `null` inside a `bars` array draws that bar's track; `text` with `v: null` falls
   back to `text`.
5. **`[value, "#colour"]`** is accepted for `value`, `bar`, `ring` and `bars` elements but is not in the
   spec's table; ring/bar numbers for segments are the sum of segment values.
6. **Vertical centring** of text is by font cell (TFT_eSPI `yAdvance`), not ink; the Pillow preview may sit a
   pixel or two differently.
7. `proto` is required (missing → `err=5`); an unknown widget type rejects the whole screen (`err=9`) rather
   than being skipped, so the PC should gate new types on the `fw` parameter.
8. String limit "31 chars" is enforced in code points, so `°` counts as one.
9. `bar` with `radius` > 0 and two or more segments: the spec's square segments start at the bar's rounded
   end, so they cover the track's rounded corners there (drawn literally; use `radius: 0` as the spec says).
10. Icons: the spec doesn't say what a missing `h` does (device: rejects the screen), whether icon fetch
    errors appear in `err` (device: no), or what "DISPLAY_URL without /v1/display" means when the URL has a
    query (device: cut at the last `/v1/display`). A PC icon named `dot`/`warn`/`check` is shadowed by the
    built-in.
11. `grad`: equal stop values are rejected (`err=14`, "ascending" read as strictly ascending); `fa` outside
    0–100 is `err=16` (field), like other out-of-range fields.

## On-device findings (2026-10-25)

- **Colour order is BGR.** With MADCTL `0x00` (what the stock init, Arduino_GFX and TFT_eSPI-as-built all
  write) pure red and blue were swapped on the panel, green correct. This also made the height-gradient
  sparkline look upside down (blue bottom rendered red, red top rendered blue). Fix: `TFT_RGB_ORDER=TFT_BGR`
  and `setRotationBgr()` in `WidgetPainter.cpp`, which re-sends Arduino_GFX's orientation bits with the BGR
  bit (`0x08`) after every `setRotation()` (start-up and end of each pass). Verified with the service's
  `screen = "testpattern"` mode: RED/GREEN/BLUE blocks correct.
- **Near-black reads grey.** `#101418` looked noticeably grey and `#1c2128`/`#30363d` clearly grey; the
  dashboard background is now `#000000`.
- Heap with the full dashboard: 22.0 KB free, largest block 21.3 KB; `err` 0; icons fetched via `/v1/icon/`.
