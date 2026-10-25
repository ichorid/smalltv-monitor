#!/usr/bin/env python3
"""Write the firmware's one-time provisioning file data/config.json without echoing secrets.

Prompts for the Wi-Fi password (hidden), generates a random API token, writes
<firmware>/data/config.json (mode 0600; git-ignored upstream) and stores the token in
~/.config/smalltv-monitor/device-token (mode 0600) so you can log in to the device web UI.
The SSID and firmware checkout default to SMALLTV_WIFI_SSID / FIRMWARE_DIR from .env (see .env.example).

On first boot the firmware moves these secrets into its EEPROM "secure storage" and strips them
from config.json. Note: until then they sit in plain text inside littlefs.bin.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smalltv_monitor import siteenv  # noqa: E402

DEFAULT_FIRMWARE_DIR = Path(__file__).resolve().parent.parent.parent / "GeekMagic-Open-Firmware"  # next to this repo
TOKEN_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "smalltv-monitor" / "device-token"


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    site = siteenv.load()
    parser.add_argument("--firmware-dir", type=Path,
                        default=Path(siteenv.get("FIRMWARE_DIR", str(DEFAULT_FIRMWARE_DIR), site)))
    parser.add_argument("--ssid", default=siteenv.get("SMALLTV_WIFI_SSID", file_values=site),
                        help="Wi-Fi network name (default SMALLTV_WIFI_SSID, else prompted)")
    parser.add_argument("--keep-token", action="store_true", help=f"reuse the token already in {TOKEN_FILE}")
    args = parser.parse_args()

    example = args.firmware_dir / "data" / "config-smartTV.example"
    target = args.firmware_dir / "data" / "config.json"
    if not example.is_file():
        sys.exit(f"not found: {example}")

    config = json.loads(example.read_text())
    ssid = args.ssid or input("Wi-Fi SSID (2.4 GHz): ").strip()
    if not ssid:
        sys.exit("SSID is required")
    password = getpass.getpass(f"Password for {ssid!r} (hidden): ")
    if password != getpass.getpass("Repeat password: "):
        sys.exit("passwords do not match")

    if args.keep_token and TOKEN_FILE.is_file():
        token = TOKEN_FILE.read_text().strip()
    else:
        token = secrets.token_urlsafe(24)
        write_private(TOKEN_FILE, token + "\n")

    config.update(wifi_ssid=ssid, wifi_password=password, api_token=token)
    write_private(target, json.dumps(config, indent=2) + "\n")
    print(f"wrote {target} (0600) for SSID {ssid!r}")
    print(f"API token stored in {TOKEN_FILE} (0600); it was not printed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
