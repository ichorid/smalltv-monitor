"""Site settings loader (.env): Python and shell must read the same file the same way."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smalltv_monitor import siteenv

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = """# comment
SMALLTV_PC_IP=192.0.2.3
  SMALLTV_DEVICE_IP = 192.0.2.12
SMALLTV_WIFI_SSID="Home # 2.4"
export FIRMWARE_DIR='/opt/fw'
SMALLTV_WIFI_IF=
lower=ignored
not a setting
"""
EXPECTED = {"SMALLTV_PC_IP": "192.0.2.3", "SMALLTV_DEVICE_IP": "192.0.2.12", "SMALLTV_WIFI_SSID": "Home # 2.4",
            "FIRMWARE_DIR": "/opt/fw"}


class SiteEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / ".env"
        self.path.write_text(SAMPLE)

    def test_parse(self) -> None:
        values = siteenv.parse(SAMPLE)
        self.assertEqual({k: v for k, v in values.items() if v}, EXPECTED)
        self.assertEqual(values["SMALLTV_WIFI_IF"], "")
        self.assertNotIn("lower", values)

    def test_environment_wins_and_empty_is_unset(self) -> None:
        clean = {k: v for k, v in os.environ.items() if not k.startswith("SMALLTV_")}
        with mock.patch.dict(os.environ, {**clean, "SMALLTV_ENV": str(self.path), "SMALLTV_PC_IP": "192.0.2.99"},
                             clear=True):
            self.assertEqual(siteenv.get("SMALLTV_PC_IP"), "192.0.2.99")
            self.assertEqual(siteenv.get("SMALLTV_DEVICE_IP"), "192.0.2.12")
            self.assertEqual(siteenv.get("SMALLTV_WIFI_IF", "detect"), "detect")
        self.assertEqual(siteenv.load(self.path.with_name("missing")), {})

    def test_shell_loader_matches(self) -> None:
        names = sorted(EXPECTED) + ["SMALLTV_WIFI_IF"]
        script = f'. "{ROOT}/scripts/site-env.sh"; load_site_env /nonexistent; ' + "; ".join(
            f'printf "%s\\n" "${{{n}:-}}"' for n in names)
        env = {"PATH": os.environ["PATH"], "SMALLTV_ENV": str(self.path), "SMALLTV_PC_IP": "192.0.2.99"}
        out = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=True).stdout
        expected = {**EXPECTED, "SMALLTV_PC_IP": "192.0.2.99", "SMALLTV_WIFI_IF": ""}
        self.assertEqual(out.split("\n")[:-1], [expected[n] for n in names])


if __name__ == "__main__":
    unittest.main()
