#!/usr/bin/env python3
"""Run all offline tests (B3a TS logic + C1 Telegram handlers)."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_JS = ROOT / "ts" / "dist" / "test_logic.js"


def main() -> int:
    if not TEST_JS.is_file():
        print(f"missing {TEST_JS}; run: (cd ts && npx tsc)", file=sys.stderr)
        return 1
    proc = subprocess.run(["node", str(TEST_JS)], cwd=str(ROOT))
    if proc.returncode != 0:
        return proc.returncode

    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
