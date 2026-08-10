#!/usr/bin/env python3
"""Run all offline tests (B3a TS logic + C1 Telegram handlers)."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_JS = ROOT / "ts" / "dist" / "test_logic.js"
REAL_STATE = ROOT / "state"
# Hermetic wallet for host+container (must precede any bot_config import).
os.environ["WALLET_KEYPAIR_PATH"] = str(
    ROOT / "tests" / "fixtures" / "offline_keypair.json"
)


def _fingerprint_dir(path: Path) -> dict[str, str]:
    """Map relative path → sha256 for every regular file under path."""
    out: dict[str, str] = {}
    if not path.is_dir():
        return out
    for p in sorted(path.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(path))
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def main() -> int:
    # Redirect ALL bot state writes to a temp dir (C12). Must be set before any
    # test module imports cycle_journal / reopen_pending / etc.
    td = tempfile.mkdtemp(prefix="meteora-test-state-")
    os.environ["METEORA_STATE_DIR"] = td
    Path(td).mkdir(parents=True, exist_ok=True)

    before = _fingerprint_dir(REAL_STATE)

    if not TEST_JS.is_file():
        print(f"missing {TEST_JS}; run: (cd ts && npx tsc)", file=sys.stderr)
        return 1
    env = os.environ.copy()
    proc = subprocess.run(["node", str(TEST_JS)], cwd=str(ROOT), env=env)
    if proc.returncode != 0:
        return proc.returncode

    # Ensure discover imports see the fixture even if dotenv would otherwise win.
    os.environ["WALLET_KEYPAIR_PATH"] = str(
        ROOT / "tests" / "fixtures" / "offline_keypair.json"
    )
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        return 1

    after = _fingerprint_dir(REAL_STATE)
    if before != after:
        print(
            "C12 GUARD FAIL: real state/ changed during tests "
            f"(METEORA_STATE_DIR was {td!r})",
            file=sys.stderr,
        )
        only_before = sorted(set(before) - set(after))
        only_after = sorted(set(after) - set(before))
        changed = sorted(
            k for k in before.keys() & after.keys() if before[k] != after[k]
        )
        if only_before:
            print(f"  removed: {only_before}", file=sys.stderr)
        if only_after:
            print(f"  added: {only_after}", file=sys.stderr)
        if changed:
            print(f"  modified: {changed}", file=sys.stderr)
        return 1
    print(f"C12 state guard OK: real state/ unchanged (test state={td})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
