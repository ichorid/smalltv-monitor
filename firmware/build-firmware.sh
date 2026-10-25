#!/usr/bin/env bash
# Build the SmallTV-Ultra firmware with this workstation's URL compiled in, then sanity-check the
# images. Nothing is flashed.
#
#   build-firmware.sh             metrics dashboard (env:esp12e): firmware.bin + littlefs.bin, factory OTA budget
#   build-firmware.sh --renderer  widget renderer (env:renderer, branch widget-renderer): firmware.bin only
#
# Site settings come from .env (see .env.example) or the environment:
#   SMALLTV_PC_IP  workstation address the device polls (default: the address carrying the default route)
#   FIRMWARE_DIR   firmware checkout (default: GeekMagic-Open-Firmware next to this repository)
#   METRICS_URL / DISPLAY_URL  override the whole URL compiled in
#   OTA_BUDGET     max firmware.bin size accepted (default: factory 9.0.50 updater, or the 4m2m sketch limit)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
# shellcheck source=../scripts/site-env.sh
. "$ROOT/scripts/site-env.sh"
load_site_env "$ROOT"

MODE=metrics
case "${1:-}" in
    "") ;;
    --renderer) MODE=renderer ;;
    *) echo "usage: $0 [--renderer]" >&2; exit 2 ;;
esac

FIRMWARE_DIR="${FIRMWARE_DIR:-$(dirname "$ROOT")/GeekMagic-Open-Firmware}"
PIO="${PIO:-$(command -v pio || echo "$HOME/.local/bin/pio")}"
FS_SIZE=$((0x3FA000 - 0x200000))  # eagle.flash.4m2m.ld LittleFS partition
REBOOT_MS="${WIFI_FALLBACK_REBOOT_MS:-300000}"

fail() { echo "FAIL: $*" >&2; exit 1; }

pc_ip() {
    local ip="${SMALLTV_PC_IP:-}"
    if [[ -z "$ip" ]]; then
        ip="$(PYTHONPATH="$ROOT" "$ROOT/.venv/bin/python" -c 'from smalltv_monitor.__main__ import default_route_address as f; print(f() or "")' 2>/dev/null || true)"
    fi
    [[ -n "$ip" ]] || fail "could not detect this host's LAN address; set SMALLTV_PC_IP in .env"
    echo "$ip"
}

if [[ "$MODE" == renderer ]]; then
    ENV=renderer FLAG=DISPLAY_URL
    URL="${DISPLAY_URL:-http://$(pc_ip):8765/v1/display}"
    # The open firmware's own OTA (4m2m layout): the ESP8266 core's maximum sketch size
    OTA_BUDGET="${OTA_BUDGET:-1044464}"
else
    ENV=esp12e FLAG=METRICS_URL
    URL="${METRICS_URL:-http://$(pc_ip):8765/metrics}"
    # Factory Ultra-V9.0.50: LittleFS starts at 0x100000 (/space.json total=0x2FA000) and the running
    # 505200-byte app occupies 0x7C000, so ESP8266HTTPUpdateServer accepts at most
    # 0x100000 - 0x7C000 - 0x1000 bytes.
    OTA_BUDGET="${OTA_BUDGET:-$((0x100000 - 0x7C000 - 0x1000))}"
    CONFIG="$FIRMWARE_DIR/data/config.json"
    [[ -f "$CONFIG" ]] || fail "$CONFIG missing; run $HERE/make-device-config.py first"
    CONFIG_SSID="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("wifi_ssid",""))' "$CONFIG")"
    [[ -n "$CONFIG_SSID" ]] || echo "WARNING: wifi_ssid is empty in config.json; the device will stay in AP mode" >&2
fi
[[ "$URL" =~ ^http://[0-9.]+(:[0-9]+)?/ ]] || fail "$FLAG must be plain http:// with a numeric IP: $URL"

echo "== building $(git -C "$FIRMWARE_DIR" describe --tags --dirty --always) (env:$ENV) with $FLAG=$URL"
export PLATFORMIO_BUILD_FLAGS="-D$FLAG=\\\"$URL\\\" -DWIFI_FALLBACK_REBOOT_MS=$REBOOT_MS"
"$PIO" run -d "$FIRMWARE_DIR" -e "$ENV"
OUT="$FIRMWARE_DIR/.pio/build/$ENV"
FW="$OUT/firmware.bin"
FS=""
if [[ "$MODE" == metrics ]]; then
    "$PIO" run -d "$FIRMWARE_DIR" -e "$ENV" --target buildfs
    FS="$OUT/littlefs.bin"
    chmod 600 "$FS"  # contains Wi-Fi credentials and the API token in plain text
fi

echo "== verifying"
python3 -I - "$FW" "$FS" "$URL" "$OTA_BUDGET" "$FS_SIZE" <<'EOF'
import sys
fw_path, fs_path, url, budget, fs_size = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
fw = open(fw_path, "rb").read()
checks = [
    ("firmware magic byte 0xE9", fw[0] == 0xE9),
    ("flash mode DIO (0x02), like the factory image", fw[2] == 0x02),
    ("flash size 4MB in header (0x4_)", fw[3] >> 4 == 0x4),
    (f"firmware {len(fw)} B fits OTA budget {budget} B (margin {budget - len(fw)} B)", len(fw) <= budget),
    ("URL compiled in", url.encode() in fw),
]
if fs_path:
    fs = open(fs_path, "rb").read()
    checks += [
        (f"littlefs.bin is {fs_size} B (4m2m layout)", len(fs) == fs_size),
        ("littlefs superblock magic", b"littlefs" in fs[:0x4000]),
        ("config.json packed in littlefs", b"config.json" in fs),
    ]
bad = [name for name, ok in checks if not ok]
for name, ok in checks:
    print(("  ok   " if ok else "  FAIL ") + name)
sys.exit(1 if bad else 0)
EOF
sha256sum "$FW" ${FS:+"$FS"} | sed "s|$OUT/||"
echo "== artifacts in $OUT (not flashed)"
