#!/usr/bin/env python3
"""Build-only width scan: how many txs initializePositionAndAddLiquidityByStrategy returns.

Never signs or sends. Writes JSON under logs/ (gitignored). RPC host only in stdout.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

PROBE = ROOT / "ts" / "dist" / "width_probe.js"
LOG_DIR = ROOT / "logs"
MAINNET_POOL = "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"
DEVNET_POOL = "FRTZiQqJig2ib5Urico3yKsQikGG8EERM8mjK94D4o8q"


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or "(rpc)"
    except Exception:
        return "(rpc)"


def _rpc(kind: str) -> str:
    if kind == "devnet":
        return os.environ["SOLANA_DEVNET_RPC_URL"]
    explicit = os.environ.get("SOLANA_RPC_URL", "").strip()
    if explicit:
        return explicit
    dev = os.environ.get("SOLANA_DEVNET_RPC_URL", "")
    if "devnet.helius-rpc.com" in dev:
        return dev.replace("devnet.helius-rpc.com", "mainnet.helius-rpc.com")
    return "https://api.mainnet-beta.solana.com"


def run(kind: str, pool: str, widths: str) -> dict:
    rpc = _rpc(kind)
    print(f"=== {kind} host={_host(rpc)} pool={pool} widths={widths}", flush=True)
    if not PROBE.is_file():
        raise SystemExit(f"missing {PROBE}; cd ts && npx tsc")
    proc = subprocess.run(
        ["node", str(PROBE), "--rpc", rpc, "--pool", pool, "--widths", widths],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if not proc.stdout.strip():
        sys.stderr.write(proc.stderr[-2000:] if proc.stderr else "")
        raise SystemExit(f"empty stdout exit={proc.returncode}")
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    for r in data.get("rows") or []:
        print(
            f"  w={r['width']:5d} pct={r['pct']:8.4f} txs={r['txCount']} "
            f"rent={r['positionRentSol']:.8f} arr={r['binArrayNew']}/{r['binArrayTotal']}",
            flush=True,
        )
    LOG_DIR.mkdir(exist_ok=True)
    (LOG_DIR / f"lite14_{kind}.json").write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )
    return data


def main() -> None:
    widths = sys.argv[1] if len(sys.argv) > 1 else "26,69,70,91,140,175,280,700,1400"
    run("devnet", DEVNET_POOL, widths)
    run("mainnet", MAINNET_POOL, widths)


if __name__ == "__main__":
    main()
