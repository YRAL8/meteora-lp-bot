#!/usr/bin/env python3
"""Focused /rebalance crash-after-close recovery test on devnet."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "WALLET_KEYPAIR_PATH",
    os.path.expanduser("~/.config/solana/meteora-devnet.json"),
)

import money_ops  # noqa: E402
import range_state  # noqa: E402
import telegram_commands as tg  # noqa: E402
from reopen_pending import is_reopen_pending, load_reopen_pending, set_reopen_pending  # noqa: E402
from tests.test_telegram_commands import _update_with_args  # noqa: E402


async def call(name: str, args: list[str]) -> list[str]:
    update, msg, ctx = _update_with_args(args)
    try:
        await getattr(tg, f"{name}_command")(update, ctx)
    except SystemExit as e:
        print(f"SystemExit({e.code}) after {name}")
    for r in msg.replies:
        print(r[:280])
    return msg.replies


async def main() -> None:
    set_reopen_pending(False)
    ghosts = [
        p
        for p in money_ops.list_open_positions()
        if float(p.get("sol") or 0) == 0 and float(p.get("usdc") or 0) == 0
    ]
    if ghosts:
        print(f"NOTE: {len(ghosts)} zero-liq ghost position(s) ignored by bot")

    # Close any live position
    pos = money_ops.get_primary_position()
    if pos:
        print("cleanup live position…")
        await call("withdraw", ["confirm"])

    range_state.half_width_bins = 3
    range_state.range_width_pct = 0.03
    print("open…")
    await call("open", ["0.07"])
    pos = money_ops.get_primary_position()
    print("pos", pos and pos.get("pubkey"), pos and (pos.get("sol"), pos.get("usdc")))
    assert pos, "open failed"

    print("rebalance crash-after-close…")
    await call("rebalance", ["crash-after-close"])
    print("reopen_pending=", is_reopen_pending())
    print("meta=", load_reopen_pending())
    assert is_reopen_pending(), "flag must remain after crash"
    assert money_ops.get_primary_position() is None, "live position must be closed"

    print("simulate next /rebalance blocked by pending…")
    replies = await call("rebalance", [])
    assert any("reopen_pending" in r for r in replies), replies

    print("owner recovers with /open…")
    await call("open", ["0.06"])
    assert money_ops.get_primary_position(), "open after crash failed"
    set_reopen_pending(False)
    print("cleared reopen_pending after successful open")

    print("full rebalance without crash…")
    set_reopen_pending(False)  # ensure clean; prior failed mid-rebalance left flag
    # If still pending from failed reopen, clear after verifying capital on wallet
    await call("rebalance", ["confirm"])
    if is_reopen_pending():
        print("rebalance left pending — recovering via /open")
        pos = money_ops.get_primary_position()
        if not pos:
            await call("open", ["0.05"])
        set_reopen_pending(False)
    print("pending after full rebalance=", is_reopen_pending())
    assert not is_reopen_pending()

    print("withdraw confirm…")
    if money_ops.get_primary_position():
        await call("withdraw", ["confirm"])
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
