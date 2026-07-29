#!/usr/bin/env python3
"""Find and optionally close zero-liquidity DLMM positions (rent reclaim).

Dry-run by default. Pass --send to actually close via exec-close-empty.
Not a Telegram command — maintenance only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bot_config  # noqa: E402
import meteora_exec  # noqa: E402
import meteora_ops  # noqa: E402
from meteora_exec import MeteoraExecError  # noqa: E402


def _is_empty(pos: dict) -> bool:
    sol = float(pos.get("sol") or 0)
    usdc = float(pos.get("usdc") or 0)
    if sol > 0 or usdc > 0:
        return False
    raw = pos.get("raw") or {}
    tx = str(raw.get("totalXAmount") or "0")
    ty = str(raw.get("totalYAmount") or "0")
    return tx in ("0", "") and ty in ("0", "")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--send",
        action="store_true",
        help="Actually close empty positions (default: dry-run)",
    )
    p.add_argument("--pool", default=None)
    p.add_argument("--rpc", default=None)
    p.add_argument("--owner", default=None)
    p.add_argument(
        "--position",
        default=None,
        help="Only consider this position pubkey (optional filter)",
    )
    args = p.parse_args()

    net = bot_config.effective_network()
    pool = args.pool or bot_config.pool_pubkey()
    rpc = args.rpc or bot_config.effective_rpc(net)
    owner = args.owner or bot_config.wallet_pubkey()

    print(f"network={net} pool={pool} owner={owner} send={args.send}")

    bal_before = meteora_ops.balances(owner, pool=pool, rpc=rpc)
    sol_before = float((bal_before.get("sol") or {}).get("ui") or 0)
    print(f"wallet SOL before: {sol_before:.9f}")

    listed = meteora_ops.list_positions(owner, pool=pool, rpc=rpc)
    positions = list(listed.get("positions") or [])
    ghosts = [pos for pos in positions if _is_empty(pos)]
    if args.position:
        ghosts = [pos for pos in ghosts if pos.get("pubkey") == args.position]

    if not ghosts:
        print("No empty (ghost) positions found.")
        return 0

    print(f"Found {len(ghosts)} empty position(s):")
    for pos in ghosts:
        pk = pos["pubkey"]
        # Rent estimate: fetch account lamports via balances is not enough;
        # report pubkey and zero liq; rent shown after close delta.
        print(
            f"  {pk}  sol={pos.get('sol')} usdc={pos.get('usdc')} "
            f"bins=[{pos.get('lowerBinId')},{pos.get('upperBinId')}]"
        )

    if not args.send:
        print("Dry-run only. Re-run with --send to close via closePositionIfEmpty.")
        return 0

    extra_env = None
    if net == "mainnet" and not bot_config.DRY_RUN:
        extra_env = {"METEORA_ALLOW_MAINNET": "1"}

    for pos in ghosts:
        pk = pos["pubkey"]
        print(f"Closing empty {pk}…")
        try:
            out = meteora_exec.exec_close_empty(
                owner,
                pk,
                send=True,
                network=net,
                pool=pool,
                rpc=rpc,
                extra_env=extra_env,
            )
        except MeteoraExecError as e:
            print(f"FAILED: {e}")
            print(json.dumps(e.payload, indent=2, ensure_ascii=False)[:2000])
            return 1
        sigs = out.get("signatures") or []
        print(f"  ok signatures={sigs}")
        for s in out.get("sends") or []:
            print(f"  send status={s.get('status')} explorer={s.get('explorer')}")

    bal_after = meteora_ops.balances(owner, pool=pool, rpc=rpc)
    sol_after = float((bal_after.get("sol") or {}).get("ui") or 0)
    print(f"wallet SOL after:  {sol_after:.9f}")
    print(f"delta SOL:         {sol_after - sol_before:+.9f}")
    return 0


if __name__ == "__main__":
    # Prefer absolute keypair path (tilde expand is unreliable in some shells).
    kp = os.environ.get("WALLET_KEYPAIR_PATH", "")
    if kp.startswith("~"):
        os.environ["WALLET_KEYPAIR_PATH"] = os.path.expanduser(kp)
    raise SystemExit(main())
