#!/usr/bin/env python3
"""C6 live proof: /open N ≈ $N on-chain; MAX_POSITION_USD announces cut."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "WALLET_KEYPAIR_PATH",
    os.path.expanduser("~/.config/solana/meteora-devnet.json"),
)

import bot_config  # noqa: E402
import money_ops  # noqa: E402
import range_state  # noqa: E402
from reopen_pending import set_reopen_pending  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def measure_suggest(n: float, half: int = 2) -> dict:
    s = money_ops.suggest_for_budget(n, half_width=half)
    price = float((s.get("params") or {}).get("usdcPerSol") or 0)
    ns = float(s.get("needSol") or 0)
    nu = float(s.get("needUsdc") or 0)
    return {
        "n": n,
        "needSol": ns,
        "needUsdc": nu,
        "total": ns * price + nu,
        "price": price,
        "frac": s.get("targetSolFraction"),
    }


def main() -> None:
    set_reopen_pending(False)
    range_state.half_width_bins = 2
    log(f"owner={money_ops.owner()} pool={bot_config.pool_pubkey()}")

    log("\n=== suggest totals (after C6: N → ≈$N) ===")
    for n in (1.0, 2.0, 5.0):
        m = measure_suggest(n)
        err = abs(m["total"] - n) / n * 100
        log(
            f"/open {n:g}: needUsdc={m['needUsdc']:.4f} needSol={m['needSol']:.6f} "
            f"total=${m['total']:.4f} err={err:.3f}%"
        )

    # Clean slate
    pos = money_ops.get_primary_position(include_empty=True)
    if pos:
        log(f"closing existing {pos.get('pubkey')}…")
        money_ops.close_position_full(pos, reply=log, record_cycle=False)

    log("\n=== live /open 1.0 ===")
    bot_config.MAX_POSITION_USD = None
    money_ops.open_with_budget(1.0, half_width=2, reply=log)
    pool = money_ops.meteora_ops.pool_info(**money_ops.ops_kwargs())
    price = float(pool.get("usdcPerSol") or 0)
    pos = money_ops.get_primary_position()
    assert pos, "no position after open"
    onchain = money_ops.position_value_usd(pos, price)
    log(
        f"on-chain position {pos['pubkey']}: sol={pos.get('sol')} usdc={pos.get('usdc')} "
        f"value=${onchain:.4f} (requested $1.00)"
    )

    log("\n=== MAX_POSITION_USD=0.4 while requesting open 5 ===")
    money_ops.close_position_full(pos, reply=log, record_cycle=False)
    bot_config.MAX_POSITION_USD = 0.4
    money_ops.open_with_budget(5.0, half_width=2, reply=log)
    pos = money_ops.get_primary_position()
    pool = money_ops.meteora_ops.pool_info(**money_ops.ops_kwargs())
    price = float(pool.get("usdcPerSol") or 0)
    onchain = money_ops.position_value_usd(pos, price) if pos else 0
    log(f"on-chain after capped open: ${onchain:.4f} (cap $0.40)")
    assert pos and onchain <= 0.55, f"cap failed: {onchain}"

    log("\n=== cleanup ===")
    money_ops.close_position_full(pos, reply=log, record_cycle=False)
    bot_config.MAX_POSITION_USD = None
    log("C6 live done")


if __name__ == "__main__":
    main()
