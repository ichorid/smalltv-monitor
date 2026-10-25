#!/usr/bin/env bash
# Stage B of the first install: upload littlefs.bin to the SmallTV through its own setup AP
# ("GeekMagic", 192.168.4.1) using this workstation's Wi-Fi, without disturbing Ethernet.
#
#   sudo ./flash-filesystem-via-ap.sh [--check] [--yes] [path/to/littlefs.bin]
#
#   --check   preflight only: no network changes, no upload, root not required
#   --yes     skip the confirmation prompt before uploading
#
# How Ethernet is protected:
#   * device-level autoconnect on the Wi-Fi interface is switched off (runtime only) before the radio
#     comes on, so saved profiles (e.g. the home network) cannot join and put a second address on the LAN
#   * the temporary profile never takes the default route or DNS, IPv6 is off, and it is never saved
#     for autoconnect; curl is pinned to the Wi-Fi interface
#   * the default route is compared before/after; the script aborts if it changes
#   * an EXIT trap deletes the profile and restores autoconnect, radio and rfkill state, also on errors/Ctrl-C
#
# Site settings from .env (see .env.example) or the environment: SMALLTV_WIFI_IF (default: the first
# Wi-Fi device NetworkManager reports), FIRMWARE_DIR.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/site-env.sh
. "$ROOT/scripts/site-env.sh"
load_site_env "$ROOT"

WIFI_IF="${SMALLTV_WIFI_IF:-${WIFI_IF:-}}"
if [[ -z "$WIFI_IF" ]] && command -v nmcli >/dev/null; then
    WIFI_IF="$(nmcli -t -f DEVICE,TYPE device | awk -F: '$2 == "wifi" {print $1; exit}')"
fi
AP_SSID="GeekMagic"
AP_PSK='$str0ngPa$$w0rd'  # upstream default, see src/main.cpp
AP_IP="192.168.4.1"
CON_NAME="geekmagic-setup-tmp"
FS_SIZE=$((0x3FA000 - 0x200000))  # eagle.flash.4m2m.ld LittleFS partition
SERVICE_STATUS="http://127.0.0.1:8765/v1/status"

CHECK_ONLY=0 ASSUME_YES=0 FS_BIN=""
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        --yes) ASSUME_YES=1 ;;
        -h|--help) sed -n '2,/^set -euo/{/^#/p}' "$0"; exit 0 ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) FS_BIN="$arg" ;;
    esac
done

USER_HOME="$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)"
FIRMWARE_DIR="${FIRMWARE_DIR:-$(dirname "$ROOT")/GeekMagic-Open-Firmware}"
FS_BIN="${FS_BIN:-$FIRMWARE_DIR/.pio/build/esp12e/littlefs.bin}"
CONFIG_JSON="$FIRMWARE_DIR/data/config.json"

log() { printf '[%s] %s\n' "$(date +%T)" "$*"; }
die() { printf '[%s] ERROR: %s\n' "$(date +%T)" "$*" >&2; exit 1; }
default_route() { ip -4 route show default | sort; }

# ---------------------------------------------------------------- preflight (read-only)
log "preflight"
[[ -f "$FS_BIN" ]] || die "not found: $FS_BIN (run build-firmware.sh)"
[[ "$(stat -c %s "$FS_BIN")" -eq "$FS_SIZE" ]] || die "$FS_BIN is not $FS_SIZE bytes; wrong image?"
head -c 16384 "$FS_BIN" | grep -aq littlefs || die "$FS_BIN has no LittleFS superblock"
[[ -f "$CONFIG_JSON" ]] || die "$CONFIG_JSON missing (run make-device-config.py, then build-firmware.sh)"
python3 -I -c 'import json,sys; c=json.load(open(sys.argv[1])); sys.exit(0 if c.get("wifi_ssid") and c.get("wifi_password") else 1)' \
    "$CONFIG_JSON" || die "config.json has no Wi-Fi credentials; the device would stay in AP mode"
[[ "$FS_BIN" -nt "$CONFIG_JSON" ]] || die "littlefs.bin is older than config.json; rerun build-firmware.sh"
log "  image ok: $FS_BIN ($(sha256sum "$FS_BIN" | cut -c1-16)…), built after config.json"

command -v nmcli >/dev/null || die "nmcli not found"
command -v curl >/dev/null || die "curl not found"
[[ -n "$WIFI_IF" ]] || die "no Wi-Fi interface found (set SMALLTV_WIFI_IF in .env)"
ip link show "$WIFI_IF" >/dev/null 2>&1 || die "Wi-Fi interface $WIFI_IF not found (set SMALLTV_WIFI_IF in .env)"
ROUTE_BEFORE="$(default_route)"
[[ -n "$ROUTE_BEFORE" ]] || die "no default route; is Ethernet up?"
if ip -4 -o addr show | grep -v " $WIFI_IF " | grep -q ' inet 192\.168\.4\.'; then
    die "another interface already uses 192.168.4.0/24; uploading could go to the wrong host"
fi
log "  default route: $(echo "$ROUTE_BEFORE" | tr '\n' ' ')"

if [[ $CHECK_ONLY -eq 1 ]]; then
    log "preflight passed (--check: no changes made)"
    exit 0
fi
[[ $EUID -eq 0 ]] || die "run as root: sudo $0 $*"

# ---------------------------------------------------------------- remember state, install cleanup
RADIO_WAS="$(nmcli radio wifi)"                                       # enabled | disabled
DEV_AUTOCONNECT_WAS="$(nmcli -g GENERAL.AUTOCONNECT device show "$WIFI_IF")"  # yes | no
RFKILL_WAS_BLOCKED=0
if command -v rfkill >/dev/null && rfkill -n -o TYPE,SOFT list | grep -q '^wlan *blocked'; then
    RFKILL_WAS_BLOCKED=1
fi

cleanup() {
    local rc=$?
    set +e
    log "cleanup"
    nmcli connection delete "$CON_NAME" >/dev/null 2>&1 && log "  removed temporary profile $CON_NAME"
    if [[ "$RADIO_WAS" == "disabled" ]]; then
        nmcli radio wifi off && log "  Wi-Fi radio off again"
    fi
    if [[ $RFKILL_WAS_BLOCKED -eq 1 ]] && command -v rfkill >/dev/null; then
        rfkill block wlan && log "  wlan rfkill soft-block restored"
    fi
    nmcli device set "$WIFI_IF" autoconnect "$DEV_AUTOCONNECT_WAS" >/dev/null 2>&1 \
        && log "  $WIFI_IF autoconnect restored to $DEV_AUTOCONNECT_WAS"
    local after
    after="$(default_route)"
    if [[ "$after" == "$ROUTE_BEFORE" ]]; then
        log "  default route unchanged"
    else
        log "  WARNING: default route changed: $(echo "$after" | tr '\n' ' ')"
    fi
    exit $rc
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ---------------------------------------------------------------- join the device AP
nmcli connection delete "$CON_NAME" >/dev/null 2>&1 || true
# Runtime-only: keeps saved profiles (the home network, …) from auto-joining once the radio is on
nmcli device set "$WIFI_IF" autoconnect no
[[ $RFKILL_WAS_BLOCKED -eq 1 ]] && rfkill unblock wlan
nmcli radio wifi on

log "waiting for $WIFI_IF to become available"
for _ in $(seq 30); do
    state="$(nmcli -g GENERAL.STATE device show "$WIFI_IF" | cut -d' ' -f1)"
    [[ "$state" -ge 30 ]] && break  # 30 = disconnected (usable)
    sleep 1
done
[[ "$state" -ge 30 ]] || die "$WIFI_IF did not become available (state $state)"

log "scanning for SSID $AP_SSID"
found=0
for _ in $(seq 10); do
    nmcli device wifi rescan ifname "$WIFI_IF" >/dev/null 2>&1 || true
    sleep 3
    if nmcli -t -f SSID device wifi list ifname "$WIFI_IF" | grep -qx "$AP_SSID"; then found=1; break; fi
done
[[ $found -eq 1 ]] || die "SSID $AP_SSID not visible. Is Stage A done and the device in AP mode (screen shows 192.168.4.1)?"

nmcli connection add type wifi ifname "$WIFI_IF" con-name "$CON_NAME" ssid "$AP_SSID" \
    connection.autoconnect no \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$AP_PSK" \
    ipv4.method auto ipv4.never-default yes ipv4.ignore-auto-dns yes ipv4.route-metric 1000 \
    ipv6.method disabled >/dev/null
log "connecting to $AP_SSID"
nmcli --wait 45 connection up "$CON_NAME" >/dev/null || die "could not join $AP_SSID"

WIFI_ADDR="$(ip -4 -o addr show dev "$WIFI_IF" | awk '{print $4}' | head -1)"
[[ "$WIFI_ADDR" == 192.168.4.* ]] || die "unexpected address on $WIFI_IF: ${WIFI_ADDR:-none}"
[[ "$(default_route)" == "$ROUTE_BEFORE" ]] || die "default route changed after joining; aborting"
[[ "$(ip -4 route get "$AP_IP" | grep -o "dev [^ ]*")" == "dev $WIFI_IF" ]] || die "$AP_IP is not routed via $WIFI_IF"
log "  joined: $WIFI_IF $WIFI_ADDR; Ethernet default route untouched"

# ---------------------------------------------------------------- upload
FORM="$(curl -fsS --interface "$WIFI_IF" -m 10 "http://$AP_IP/legacyupdate")" \
    || die "http://$AP_IP/legacyupdate not reachable. Only served while LittleFS is empty (Stage A just done)"
grep -q "name='filesystem'" <<<"$FORM" || die "/legacyupdate has no filesystem form; is this the open firmware?"
log "  /legacyupdate form present"

if [[ $ASSUME_YES -ne 1 ]]; then
    read -r -p "Upload $(basename "$FS_BIN") ($FS_SIZE bytes) to $AP_IP now? Do not power off the device. [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || die "aborted by user (nothing uploaded)"
fi

log "uploading (a 2 MB image takes roughly 30-90 s)"
RESPONSE="$(curl -sS --interface "$WIFI_IF" -m 300 -F "filesystem=@$FS_BIN" "http://$AP_IP/legacyupdate" || true)"
if grep -q "Update Success" <<<"$RESPONSE"; then
    log "  device replied: Update Success, rebooting"
else
    die "upload did not report success. Device said: $(head -c 300 <<<"$RESPONSE" | tr '\n' ' ')"
fi
# cleanup (trap) now drops the AP link; the device leaves AP mode anyway

# ---------------------------------------------------------------- watch for the device on the LAN
log "waiting up to 120 s for the display to poll the metrics service from the LAN"
for _ in $(seq 60); do
    sleep 2
    seen="$(curl -fsS -m 2 "$SERVICE_STATUS" 2>/dev/null | LOCAL_IPS="127.0.0.1 $(hostname -I 2>/dev/null)" python3 -I -c '
import json, os, sys
clients = json.load(sys.stdin).get("clients", {})
local = set(os.environ["LOCAL_IPS"].split())  # this workstation polling itself does not count
hits = ["%s (last %s s ago, %s requests)" % (ip, c["age_s"], c["requests"]) for ip, c in clients.items()
        if ip not in local and c.get("last_path") == "/metrics" and c["age_s"] < 5]
print(", ".join(hits))' 2>/dev/null || true)"
    if [[ -n "$seen" ]]; then
        log "  display is polling: $seen"
        exit 0
    fi
done
log "  no poll seen yet. Check the screen; the device may need more time, or Wi-Fi credentials may be wrong (then it falls back to AP $AP_SSID)"
exit 0
