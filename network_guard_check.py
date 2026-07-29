"""Mainnet double-lock check without importing exec network side effects into report."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def check_mainnet_blocked() -> str:
    env = os.environ.copy()
    env.pop("METEORA_ALLOW_MAINNET", None)
    exec_js = ROOT / "ts" / "dist" / "exec.js"
    if not exec_js.is_file():
        return "MISSING_EXEC_JS"
    proc = subprocess.run(
        [
            "node",
            str(exec_js),
            "exec-open",
            "--network",
            "mainnet",
            "--send",
            "--owner",
            "11111111111111111111111111111111",
            "--sol",
            "0.01",
            "--usdc",
            "1",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if "mainnet send blocked" in out or "METEORA_ALLOW_MAINNET" in out:
        return "BLOCKED_OK"
    return f"UNEXPECTED exit={proc.returncode} out={out[:300]}"
