# Display widget protocol: draft v1

Status: **draft for discussion**. Nothing implemented yet.

Goal: the PC decides *what* is on screen (layout, colours, brightness, alerts, priorities); the firmware
only draws. New data sources, redesigns and night-mode policies become Python changes, not firmware
changes. Replaces the `/metrics` dashboard entirely (see Decisions).

## Principle: full state every poll, device redraws only what changed

Every second the device fetches the **complete screen description**. There are no revisions, no partial
updates and no device-side history, so a reboot on either side or a missed poll can never leave the two
out of sync.

Not redrawing unchanged pixels is the device's job, not the protocol's: it remembers a 32-bit hash of each
widget's JSON as last drawn and repaints only widgets whose hash changed. If any widget's `id` or
rectangle changed (new layout, alert banner appeared), it clears the screen and redraws everything once
(≈ 113 ms measured).

Why this is affordable (measured in `docs/GFX-LAB.md`):

- A full screen is ~1–3 KB of JSON; ArduinoJson needs roughly 2× that while parsing, against ~24 KB
  free heap while drawing.
- One widget redraw ≈ 10 ms; a typical second changes a handful of widgets.
- Transport: the device polls (HTTP/1.0 with `Content-Length`, no chunking), which also serves as liveness.

## Request

```
GET /v1/display?fw=1.5.0-w1&heap=24512&blk=23900&up=86400&rssi=-61&err=0
```

The query string reports device health the PC can't see today: free heap, largest block, uptime,
Wi-Fi RSSI, and the error of the previous poll as a number (`err`; 0 = fine, 1 http, 2 size, 3 json,
4 nomem, 5 proto, 6 screen, 7 widgets missing/too many, 8 id, 9 type, 10 rect, 11 overlap, 12 font,
13 colour, 14 value, 15 string, 16 field). Icon fetch errors are only logged on the device.

## Response

```json
{
  "proto": 1,
  "screen": { "bg": "#101418", "bri": 80, "poll_ms": 1000, "offline_bri": 5, "offline_dim_s": 300 },
  "w": [
    { "id": "title", "t": "text",  "r": [8, 4, 224, 18], "f": 0, "c": "#ffffff", "text": "PC" },
    { "id": "cpu",   "t": "ring",  "r": [16, 26, 92, 92], "f": 1, "label": "CPU", "max": 100,
      "v": [[24, "#fd8c00"], [9, "#f2384c"]] },
    { "id": "gpu",   "t": "ring",  "r": [132, 26, 92, 92], "f": 1, "label": "GPU", "max": 100,
      "v": 82, "c": "#f2384c" },
    { "id": "ram",   "t": "bar",   "r": [8, 128, 224, 12], "label": "RAM", "max": 64,
      "v": [[27.1, "#a855f7"], [21.4, "#4b3a6b"]] },
    { "id": "cores", "t": "bars",  "r": [8, 146, 224, 36], "dir": "v", "max": 100, "gap": 2,
      "v": [[[40, "#fd8c00"], [8, "#f2384c"]], 12, 95, 3, 7, 60, 22, 18, 5, 5, 9, 31, 2, 4, 70, 11] ,
      "c": "#fd8c00" },
    { "id": "cpuh",  "t": "spark", "r": [8, 186, 224, 24], "max": 100, "c": "#fd8c00",
      "v": [31, 35, 37, 36, 40, 52, 48, 39, 37, 33, 30, 29, 35, 41, 39] },
    { "id": "temp",  "t": "value", "r": [8, 214, 112, 22], "f": 0, "a": "l", "unit": "°C", "v": 51, "c": "#ffffff" },
    { "id": "pwr",   "t": "value", "r": [120, 214, 112, 22], "f": 0, "a": "r", "unit": "W", "v": 212, "c": "#f2384c" }
  ]
}
```

## Widgets

All rectangles are absolute pixels `[x, y, w, h]` on the 240×240 screen. **Widgets must not overlap**:
each owns its rectangle and repaints it on the screen background, which is what makes per-widget redraws
correct without a frame buffer. The PC-side layout helpers enforce this, and the device rejects violations.

Common fields: `id` (unique, stable across polls), `t` (type), `r` (rect), `c` (main colour, `#rrggbb`), `tc`/`lc` (text/label colours),
`bg` (optional, overrides `screen.bg` inside this rect), `v` (value).

| `t` | Draws | `v` | Extra fields |
|---|---|---|---|
| `text` | Static or changing text, clipped to its rect | string (optional, replaces `text`) | `f`, `a` (l/c/r), `text` |
| `value` | Number or short string plus unit, optionally with a left label | number or string | `f`, `a`, `unit`, `dp` (decimals), `label` |
| `bar` | One bar, horizontal or vertical | number, or **segments** | `dir` (h/v), `max`, `track` (colour), `label` (drawn inside the rect: left for h, bottom for v), `radius` (default 0 = straight) |
| `bars` | N bars side by side in one rect (per-core CPU, histograms) | array; each element a number or segments | `dir`, `max`, `gap`, `track` |
| `ring` | 270° anti-aliased gauge, number centred, label in the bottom gap | number, or segments | `f`, `max`, `track`, `label`, `show` (`value`/`none`) |
| `spark` | Sparkline (line + filled area) | array of samples, oldest first (≤ 120) | `max`, `min`, `fill` (colour), `grad` (colour stops), `fa` (gradient fill %) |
| `icon` | Small anti-aliased icon served by the PC (see Icons) | icon name | `h` (content hash, required for PC icons) |
| `panel` | Filled rounded block (decoration, alert background) | none | `radius` |

**Segments** (stacked, multi-colour): `[[value, "#colour"], [value, "#colour"], ...]`, drawn in order
from the start of the bar or arc; the remainder up to `max` shows the `track` colour. Examples:
CPU user + system, RAM used + cache, GPU compute + memory controller. A plain number uses the widget's `c`.

**Missing data**: `v: null` means "no data" (e.g. GPU driver unavailable). `value` and `ring` draw
`--`, `bar`/`bars` draw only the track, `spark` draws nothing.

**Characters**: every string must only use glyphs present in the font it is drawn with; the device
skips unknown glyphs. Font 0 (NotoSansBold 15): U+0021–U+007E plus `°` (U+00B0). Font 1 (NotoSansBold 36):
U+0021–U+007E only, so no `°`; put units in a smaller widget. Font 2 (mono 20): U+0020–U+007E plus `°`.
Space (U+0020) is always allowed. The PC composer validates this.

Colour rules (thresholds, warnings) live in Python: the PC just sends whatever colour each value or
segment should have this second.

## Rendering conventions (device and PC preview must agree)

Angles: 0° = 3 o'clock, clockwise (screen coordinates). Defaults: text colour `tc` = `#ffffff`,
label colour `lc` = `#8b949e`, `track` = `#1c2128`, `dp` = 0, `f` = 0 (ring number: `f` = 1), `c` = `#ffffff`
for every type, `max` = 100 for bar/bars/ring, spark `max` = largest sample, `bar` `dir` = `h`, `bars` `dir` = `v`,
`gap` = 1, `a` = `l`, `screen.bg` = `#000000`, `screen.bri` = 100.

Strictness: `proto` is required; an unknown widget type, or a PC icon without `h`, rejects the whole
screen (the PC must only send types the firmware knows, by checking `fw`). `[value, "#colour"]` works for
`value`, `bar`, `ring` and each `bars` element; a ring or bar number shows the sum of its segments.
`null` details: `value` draws `--` without the unit, `bars: null` draws nothing but the background,
a `null` inside `bars` draws that bar's track, `text` with `v: null` shows `text`. Text is centred
vertically by font cell, so the PC preview may differ by a pixel or two.

- `text`: font `f`, colour `c`, aligned `a` (default `l`), vertically centred in its rect.
- `value`: number formatted with `dp` decimals (string values verbatim), then `unit` appended
  **verbatim** (the PC includes a leading space if it wants one, e.g. `" W"` vs `"°C"`). Font `f`,
  colour `c` (or the value's colour override), aligned `a`, vertically centred. With `label`: the label
  is drawn left-aligned in `lc` (same font `f`) and the value is **right-aligned**, whatever `a` says.
- `ring`: side S = min(w, h), centred in the rect; outer radius R = S/2 − 1; thickness T = max(6, round(S/10)).
  Track arc 135°→405°; segments drawn in order from 135°, each spanning 270° × v / `max`. Number
  (rounded, no unit) centred, font `f`, colour `tc`. Label font 0, colour `lc`, centred horizontally,
  vertically centred at cy + 0.72·R (in the bottom gap).
- `bar`, `dir: h`: if `label`, it occupies the left 48 px (font 0, `lc`, vertically centred) and the bar the
  rest; bar height = rect height; fills left to right. `dir: v`: label in the bottom 16 px (centred), bar
  above it, fills bottom up. Corners: the track uses `radius` (default 0 = straight). A single value or a
  single segment is filled with the same radius; **two or more segments are always drawn square**
  (rounded joins between colours look broken), so use `radius: 0` for segmented bars.
- `bars`: n bars across the rect (`dir: v`: columns filling bottom-up; `h`: rows filling left-to-right),
  thickness = floor((extent − gap·(n−1)) / n), left/top aligned, square corners.
- `spark`: samples spread evenly from x to x+w−1; y = bottom − (v − min)/(max − min)·(h − 1), clamped;
  2 px line in `c`; area under the line filled with `fill` if given. `min` defaults to 0.
  **Gradient** (`grad`: 2–8 stops `[[value, "#colour"], ...]`, strictly ascending values, same units as the
  samples): colour depends on **height**, like btop. Each pixel row gets the colour of the value that row
  represents (the inverse of the y mapping), interpolated linearly in RGB between stops and clamped to the
  end colours. Line pixels use their row's colour; fill pixels use their row's colour blended over the
  background at `fa` percent (default 35; 0 = no fill). With `grad`, `c` and `fill` are ignored. Only
  peaks reach the warm rows, so lows stay cold.
- `panel`: filled rounded rect, colour `c`, radius `radius` (default 6).
- `icon`: the bitmap is centred in the rect (not scaled). Mono icons blend `c` over the background with
  alpha a/15; colour icons blend each palette colour with its own alpha. Built-in names `dot`, `warn`, `check` are drawn by the firmware at size min(w, h) and need no `h`.

## Icons

Icons come from the PC, so adding one never needs a firmware change. A widget names the icon and its
content hash: `{"t": "icon", "id": "icl", "r": [8, 40, 18, 18], "v": "claude", "h": 2846104221, "c": "#d97757"}`.

- The device keeps a RAM cache keyed by (`v`, `h`). On a miss it fetches
  `GET <DISPLAY_URL without "/v1/display">/v1/icon/<v>`, at most **one icon fetch per poll** so a poll's
  duration stays bounded; until it arrives the widget's rect is just cleared.
- Response body (`application/octet-stream`, ≤ 512 bytes, checked via `Content-Length`), two formats:
  - **Mono** `'S' 'I' 0x01 w h`, then ceil(w·h/2) bytes of 4-bit alpha, row-major, high nibble first.
    Drawn in the widget colour `c`.
  - **Colour** `'S' 'I' 0x02 w h n`, then a palette of n ≤ 16 entries of 3 bytes each (RGB565 high byte,
    low byte, alpha 0–255), then ceil(w·h/2) bytes of 4-bit palette indices (row-major, high nibble
    first). Each pixel blends its palette colour over the background with the entry's alpha; `c` is ignored.
  - `w`, `h` ≤ 24.
- Cache: up to 16 icons, least recently used evicted (16 × 16×16 icons ≈ 2.1 KB). RAM only, never flash.
- `h` is the PC's FNV-1a-32 of the response body; the device does not verify it, it only uses it to notice
  that an icon changed.

## Screen, brightness, night mode

- `screen.bri` 0–100 → backlight PWM on GPIO5 (active-low), applied when it changes, never written to flash.
- `screen.poll_ms`: the PC can slow polling (e.g. 5000 at night) or speed it up.
- Night mode is a PC policy that sets `bri`: schedule, session locked or idle, and overrides when
  something needs you. The firmware has no clock logic.
- Offline (3 failed polls): the firmware keeps the last frame, shows a small red dot in a corner, and
  after `offline_dim_s` dims to `offline_bri`, using the last values it received.

## Alerts

No special mechanism: an alert is a layout the PC composes. Because widgets may not overlap, text cannot
sit on top of a `panel`: give the alert's `text`/`value` widgets the alert colour as their own `bg` and
tile the area with them, using `panel` only where there is no text. The geometry change triggers one
full redraw; when the alert ends, the next poll's layout restores the normal screen the same way.
Priority, queueing and timeouts are PC logic.

## Device limits (validated on receipt; violation → keep drawing the previous state, report `err`)

Measured in `docs/GFX-LAB.md` (parse benchmark): a 4.4 KB screen costs ~71 ms per poll (fetch +
streaming parse + per-widget hash) and leaves ≥ 15 KB free heap; 7–10 KB screens leave too little.

| Limit | Value | Why |
|---|---|---|
| **Response size** | **4096 bytes** (compact JSON) | ~10 KB peak while parsed (≈ 2.3× the JSON). Checked against `Content-Length` *before* parsing; larger → rejected, `err=2` |
| Parsing | Streamed from the socket, no body buffer | ~4 KB less peak heap than buffering, same speed |
| Widgets | 32 | Fixed table of id hash, rect hash, content hash (12 B each) |
| Bars per `bars` widget | 32 | |
| Segments per bar or ring | 4 | |
| Spark samples | 120 | |
| String length | 31 chars | |
| Icons | ≤ 24×24 px, 4-bit alpha, ≤ 16 cached | Fetched once per (name, hash) |
| Fonts | `0` NotoSansBold 15, `1` NotoSansBold 36 (TFT_eSPI example VLW arrays), `2` NotoSansMono-Bold 20 (to be generated) | Compiled-in VLW |

The per-widget limits only bound the device's fixed tables. They cannot all be maxed at once: that
would be ~9.8 KB. **The 4096-byte total is the binding constraint**, and the PC composer must check the
compact size before sending and trim (fewer spark samples, fewer segments) rather than let the device
reject the screen.

## PC side

- `smalltv_monitor/display/`: widget dataclasses that serialize to this JSON, layout helpers (grids,
  rows, columns) that produce non-overlapping rects, and a screen composer that turns store data,
  colour rules and alerts into one response.
- `scripts/preview.py`: renders the same JSON to a 240×240 PNG with Pillow and the Noto fonts, so
  designs can be iterated without flashing. It's an approximation; the device is the reference.

## Decisions

- **No input.** The unit has no button or touch area: the display is output-only, and alerts are
  acknowledged on the PC (e.g. answering the agent prompt removes the alert from the next layout).
- **No fallback dashboard.** The new firmware drops the six-tile `/metrics` dashboard and renders only
  `/v1/display`. If the endpoint is unreachable it shows the offline state (last frame, red dot, then dims).
  The service keeps serving `/metrics` until the new firmware is flashed, then it can be removed.

## Open questions

1. Fonts: NotoSansBold 15 and 36 plus a mono 20 (default), or more sizes at ~10–50 KB flash each?
