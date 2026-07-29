#!/usr/bin/env python3
"""Direct handler invocations on devnet with real meteora_exec transactions."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot_config  # noqa: E402
import meteora_ops  # noqa: E402
import telegram_commands as tg  # noqa: E402
from position_state import load_last_position  # noqa: E402
from tests.test_telegram_commands import _update_with_args  # noqa: E402


async def _run(name: str, args: list[str]) -> dict:
    update, msg, ctx = _update_with_args(args)
    handlers = {
        "status": tg.status_command,
        "open": tg.open_command,
        "addliquidity": tg.addliquidity_command,
        "withdraw": tg.withdraw_command,
        "close": tg.close_command,
        "swap": tg.swap_command,
    }
    await handlers[name](update, ctx)
    return {"command": name, "args": args, "replies": msg.replies}


async def main() -> None:
    os.environ.setdefault("WALLET_KEYPAIR_PATH", str(Path.home() / ".config/solana/meteora-devnet.json"))
    print(f"network={bot_config.effective_network()} pool={bot_config.pool_pubkey()}")
    print(f"owner={bot_config.wallet_pubkey()}")

    results = []

    # Optional: swap USDC if needed — skip if enough USDC
    bal = meteora_ops.balances(
        bot_config.wallet_pubkey(),
        pool=bot_config.pool_pubkey(),
        rpc=bot_config.effective_rpc(),
    )
    usdc = (bal.get("usdc") or {}).get("ui", 0)
    print(f"start balances: SOL={(bal.get('sol') or {}).get('ui')} USDC={usdc}")

    results.append(await _run("open", ["0.02", "0.05"]))
    pos = load_last_position()
    print(f"last_position={pos}")

    if pos:
        results.append(await _run("addliquidity", ["0.005", "0.01"]))
        results.append(await _run("withdraw", ["5000"]))
        results.append(await _run("close", []))

    out_path = ROOT / "state" / "live_handler_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_path}")
    for r in results:
        print("---", r["command"], r["args"])
        for line in r["replies"]:
            print(line[:200])


if __name__ == "__main__":
    asyncio.run(main())
