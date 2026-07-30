#!/usr/bin/env python3
"""C9c: live scripts must not silently keep the offline fixture wallet."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class LiveGuardTests(unittest.TestCase):
    def test_begin_live_refuses_fixture_when_nothing_saved(self) -> None:
        code = r"""
import os, sys
from pathlib import Path
ROOT = Path(%r)
sys.path.insert(0, str(ROOT))
os.environ.pop("WALLET_KEYPAIR_PATH", None)
os.environ.pop("METEORA_LIVE_WALLET", None)
from tests.live_guard import begin_live_script
begin_live_script()
""" % str(ROOT)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr + proc.stdout)
        self.assertIn("REFUSE", proc.stderr)

    def test_begin_live_restores_saved_wallet(self) -> None:
        real = Path.home() / ".config/solana/meteora-devnet.json"
        if not real.is_file():
            self.skipTest("no local meteora-devnet.json")
        code = r"""
import os, sys
from pathlib import Path
ROOT = Path(%r)
real = Path(%r)
sys.path.insert(0, str(ROOT))
os.environ["WALLET_KEYPAIR_PATH"] = str(real)
from tests.test_telegram_commands import _update_with_args  # noqa: F401
from tests.live_guard import begin_live_script, offline_fixture_path
import bot_config
assert Path(bot_config.WALLET_KEYPAIR_PATH).resolve() == offline_fixture_path().resolve()
pk = begin_live_script()
assert Path(bot_config.WALLET_KEYPAIR_PATH).resolve() == real.resolve()
assert "offline_keypair" not in bot_config.WALLET_KEYPAIR_PATH
print("RESTORED_OK", pk)
""" % (str(ROOT), str(real))
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        self.assertTrue(proc.stdout.startswith("LIVE wallet="), msg=proc.stdout)
        self.assertIn("RESTORED_OK", proc.stdout)
        self.assertNotIn("offline_keypair", proc.stdout)


if __name__ == "__main__":
    unittest.main()
