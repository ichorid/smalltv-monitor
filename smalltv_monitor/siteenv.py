"""Site settings for the build, flash and check tools: the LAN addresses, Wi-Fi network and paths of
one installation, kept out of the repository.

Read from `$SMALLTV_ENV` or `<repo>/.env` (git-ignored; template: `.env.example`). The format is
`KEY=value` per line, `#` starts a comment line, optional surrounding quotes are stripped; the file is
parsed, never executed. A variable already set in the environment wins over the file, and an empty
value counts as unset. The service itself does not need any of this: it detects its LAN address.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if not sep or line.lstrip().startswith("#") or not _KEY.fullmatch(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load(path: Path | None = None) -> dict[str, str]:
    path = path or Path(os.environ.get("SMALLTV_ENV") or ROOT / ".env")
    try:
        return parse(path.read_text())
    except FileNotFoundError:
        return {}


def get(name: str, default: str | None = None, file_values: dict[str, str] | None = None) -> str | None:
    """`name` from the environment, else from the .env file, else `default`."""
    value = os.environ.get(name) or (load() if file_values is None else file_values).get(name)
    return value or default
