#!/usr/bin/env python3
"""Devnet acceptance for C_PARITY — invoke handlers directly with real txs."""
from __future__ import annotations

import asyncio
import json
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
import telegram_commands as tg  # noqa: E402
from reopen_pending import is_reopen_pending, load_reopen_pending, set_reopen_pending  # noqa: E402
from tests.test_telegram_commands import _update_with_args  # noqa: E402
from tests.live_guard import begin_live_script  # noqa: E402

begin_live_script()


async def _run(name: str, args: list[str]) -> dict:
    update, msg, ctx = _update_with_args(args)
    handler = getattr(tg, f"{name}_command")
    try:
        await handler(update, ctx)
    except SystemExit as e:
        return {"command": name, "args": args, "replies": msg.replies, "exit": e.code}
    return {"command": name, "args": args, "replies": msg.replies}


def _print(r: dict) -> None:
    print("---", r["command"], r["args"], "exit=" + str(r.get("exit")))
    for line in r["replies"]:
        print(line[:300])


async def main() -> None:
    results = []

    # Clear leftover reopen flag from prior crash tests
    set_reopen_pending(False)

    # Close any leftover position first
    pos = money_ops.get_primary_position()
    if pos:
        print(f"closing leftover {pos['pubkey']}")
        results.append(await _run("withdraw", ["confirm"]))
        _print(results[-1])

    # /setrange
    results.append(await _run("setrange", ["5"]))
    _print(results[-1])

    # Narrow half_width for out-of-range setup (binStep=1 makes setrange 1% → ~100 bins)
    range_state.half_width_bins = 2
    range_state.range_width_pct = 0.02  # informational only
    print(f"forced half_width_bins={range_state.current_half_width()}")

    # /open <usdc>
    results.append(await _run("open", ["0.08"]))
    _print(results[-1])
    pos = money_ops.get_primary_position()
    print(f"position after open: {pos}")

    if pos:
        # /addliquidity
        results.append(await _run("addliquidity", ["0.02"]))
        _print(results[-1])

        # /status /pnl (no tx)
        results.append(await _run("status", []))
        _print(results[-1])
        results.append(await _run("pnl", []))
        _print(results[-1])

        # /pauza /boevoy /stop (state only)
        results.append(await _run("pauza", []))
        results.append(await _run("boevoy", []))
        _print(results[-1])

        # Move price out of range with swaps if possible
        import meteora_exec
        try:
            for amt in (0.05, 0.05, 0.05):
                print(f"swap sol-to-usdc {amt} to push activeId…")
                meteora_exec.exec_swap(
                    money_ops.owner(),
                    "sol-to-usdc",
                    amt,
                    **money_ops.exec_kwargs(),
                )
        except Exception as e:
            print(f"swap push note: {e}")

        # /rebalance with crash-after-close
        results.append(await _run("rebalance", ["crash-after-close"]))
        _print(results[-1])
        print(f"reopen_pending after crash: {is_reopen_pending()} meta={load_reopen_pending()}")

        # Simulate startup warning
        if is_reopen_pending():
            meta = load_reopen_pending()
            print("STARTUP WARN would say:", meta)
            # Complete reopen via /open after clearing? Task wants warn, then we finish rebalance
            # Clear crash flag path: finish with open after manual clear of blocked rebalance check
            # Actually rebalance refuses if pending — so owner opens manually:
            results.append(await _run("open", ["0.06"]))
            _print(results[-1])
            # If open refused because we need to clear pending for rebalance only —
            # open is allowed. After open, clear pending.
            set_reopen_pending(False)

        pos = money_ops.get_primary_position()
        if pos:
            # Full rebalance without crash
            results.append(await _run("rebalance", ["confirm"]))
            _print(results[-1])

        pos = money_ops.get_primary_position()
        if pos:
            # /withdraw confirm
            results.append(await _run("withdraw", ["confirm"]))
            _print(results[-1])

    # Mainnet guard
    from network_guard_check import check_mainnet_blocked

    print("mainnet_guard:", check_mainnet_blocked())

    out = ROOT / "state" / "parity_live_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # Strip huge HTML for file
    slim = [{"command": r["command"], "args": r["args"], "replies": r["replies"], "exit": r.get("exit")} for r in results]
    out.write_text(json.dumps(slim, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    # helper inline if module missing
    asyncio.run(main())
