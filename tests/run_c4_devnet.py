#!/usr/bin/env python3
"""C4 live proof on devnet: real out-of-range position → auto-rebalance.

Does NOT flip AUTO_REBALANCE in .env. Overrides are process-local only.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Process-local overrides BEFORE importing bot_config consumers that cache flags.
os.environ["WALLET_KEYPAIR_PATH"] = os.environ.get(
    "WALLET_KEYPAIR_PATH", os.path.expanduser("~/.config/solana/meteora-devnet.json")
)
os.environ["AUTO_REBALANCE"] = "true"
os.environ["REBALANCE_DELAY_MIN"] = "0"
os.environ["MIN_REBALANCE_INTERVAL_MIN"] = "0"
os.environ["MAX_REBALANCES_PER_DAY"] = "6"
os.environ["REBALANCE_BLOCKED_REMINDER_HOURS"] = "0.0001"

import bot_config  # noqa: E402
import main as main_mod  # noqa: E402
import money_ops  # noqa: E402
import meteora_exec  # noqa: E402
import meteora_ops  # noqa: E402
import range_state  # noqa: E402
import auto_rebalance_limits as ar_limits  # noqa: E402
from reopen_pending import is_reopen_pending, set_reopen_pending  # noqa: E402


def _log(msg: str) -> None:
    print(msg, flush=True)


async def open_offset_position(budget_usdc: float = 0.35) -> dict:
    """Open a narrow position entirely BELOW activeId so it is out of range now."""
    set_reopen_pending(False)
    existing = money_ops.get_primary_position(include_empty=True)
    if existing is not None:
        _log(f"closing existing {existing.get('pubkey')} before offset open…")
        money_ops.close_position_full(
            existing, reply=_log, record_cycle=False
        )

    pool = meteora_ops.pool_info(**money_ops.ops_kwargs())
    active = int(pool["activeId"])
    # 5-bin pocket well below active
    hi = active - 8
    lo = active - 12
    _log(f"activeId={active} → offset bins [{lo},{hi}] (intentionally OOR)")
    sug = money_ops.suggest_for_budget(
        budget_usdc, min_bin_id=lo, max_bin_id=hi
    )
    need_sol = float(sug.get("needSol") or 0)
    need_usdc = float(sug.get("needUsdc") or 0)
    money_ops.apply_swap_suggestion(sug.get("swapSuggestion"), _log)
    _log(f"open offset {need_sol:.6f} SOL + {need_usdc:.4f} USDC")
    payload = meteora_exec.exec_open(
        money_ops.owner(),
        need_sol,
        need_usdc,
        min_bin_id=lo,
        max_bin_id=hi,
        **money_ops.exec_kwargs(),
    )
    _log(f"OPEN sigs={payload.get('signatures')}")
    pos = money_ops.get_primary_position()
    if not pos:
        raise RuntimeError("offset open left no position")
    price = float(pool.get("usdcPerSol") or 0)
    lo_p, hi_p = money_ops.bin_prices(active, price, int(pool["binStep"]), lo, hi)
    import meteora_cycle_journal as cycle_journal

    cycle_journal.safe_call(
        cycle_journal.get_default_journal().on_open,
        position_pubkey=str(pos["pubkey"]),
        range_width_pct=range_state.current_range_pct(),
        lower_bin_id=lo,
        upper_bin_id=hi,
        lower_price=lo_p,
        upper_price=hi_p,
        open_price=price,
        open_sol_qty=float(pos.get("sol") or 0),
        open_usdc_qty=float(pos.get("usdc") or 0),
        open_position_value_usd=money_ops.position_value_usd(pos, price),
    )
    _log(
        f"POS {pos['pubkey']} bins=[{pos['lowerBinId']},{pos['upperBinId']}] "
        f"sol={pos.get('sol')} usdc={pos.get('usdc')}"
    )
    return pos


async def run_auto_chain_only() -> None:
    """Shorter path used for a clean journal line after the full proof."""
    bot_config.AUTO_REBALANCE = True
    bot_config.REBALANCE_DELAY_MIN = 0
    bot_config.MIN_REBALANCE_INTERVAL_MIN = 0
    bot_config.MAX_REBALANCES_PER_DAY = 6
    main_mod.out_of_range_since = None
    main_mod._reset_rebalance_blocked_state()
    await open_offset_position(0.3)
    t0 = datetime.now(timezone.utc)
    await main_mod.monitor_position(now=t0)
    await main_mod.monitor_position(now=t0 + timedelta(seconds=5))
    _log(f"pending={is_reopen_pending()} pos={money_ops.get_primary_position()}")



async def run_auto_chain() -> None:
    # Force config attrs (bot_config already loaded env at import).
    bot_config.AUTO_REBALANCE = True
    bot_config.REBALANCE_DELAY_MIN = 0
    bot_config.MIN_REBALANCE_INTERVAL_MIN = 0
    bot_config.MAX_REBALANCES_PER_DAY = 6

    main_mod.out_of_range_since = None
    main_mod._reset_rebalance_blocked_state()

    t0 = datetime.now(timezone.utc)
    _log("=== tick1: detect OOR ===")
    await main_mod.monitor_position(now=t0)
    _log(f"out_of_range_since={main_mod.out_of_range_since}")

    _log("=== tick2: delay=0 elapsed → auto rebalance ===")
    await main_mod.monitor_position(now=t0 + timedelta(seconds=5))

    pos = money_ops.get_primary_position()
    _log(
        f"after auto: pending={is_reopen_pending()} pos="
        f"{None if not pos else pos.get('pubkey')}"
    )
    if pos:
        pool = meteora_ops.pool_info(**money_ops.ops_kwargs())
        active = int(pool["activeId"])
        in_rng = int(pos["lowerBinId"]) <= active <= int(pos["upperBinId"])
        _log(f"new bins=[{pos['lowerBinId']},{pos['upperBinId']}] active={active} in_range={in_rng}")
    lim = ar_limits.load_state()
    _log(f"limit state count_today={lim.count_today} last={lim.last_rebalance_at}")


async def run_daily_cap_refusal() -> None:
    _log("=== daily cap refusal ===")
    bot_config.AUTO_REBALANCE = True
    bot_config.REBALANCE_DELAY_MIN = 0
    bot_config.MIN_REBALANCE_INTERVAL_MIN = 0
    bot_config.MAX_REBALANCES_PER_DAY = 1

    # Seed: already used today's one slot; last rebalance long ago.
    now = datetime.now(timezone.utc)
    st = ar_limits.load_state()
    st.day_utc = now.strftime("%Y-%m-%d")
    st.count_today = 1
    st.last_rebalance_at = (now - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
    st.daily_limit_notified_day = None
    ar_limits.save_state(st)

    # Ensure we have an OOR position (re-open offset if previous reopen put us in range)
    pos = money_ops.get_primary_position()
    pool = meteora_ops.pool_info(**money_ops.ops_kwargs())
    active = int(pool["activeId"])
    if pos is None or (
        int(pos["lowerBinId"]) <= active <= int(pos["upperBinId"])
    ):
        await open_offset_position(0.3)

    main_mod.out_of_range_since = None
    main_mod._reset_rebalance_blocked_state()
    t0 = datetime.now(timezone.utc)
    await main_mod.monitor_position(now=t0)
    await main_mod.monitor_position(now=t0 + timedelta(seconds=5))
    lim = ar_limits.load_state()
    _log(
        f"after cap ticks: count={lim.count_today} notified_day={lim.daily_limit_notified_day} "
        f"pending={is_reopen_pending()}"
    )
    assert lim.daily_limit_notified_day == lim.day_utc, "expected daily-limit Telegram mark"
    assert lim.count_today == 1, "cap must not increment when blocked"


async def main() -> None:
    range_state.half_width_bins = 3
    _log(
        f"network={bot_config.effective_network()} pool={bot_config.pool_pubkey()} "
        f"owner={money_ops.owner()}"
    )
    await open_offset_position(0.35)
    await run_auto_chain()
    await run_daily_cap_refusal()
    # cleanup: close leftover OOR position so wallet is clean
    pos = money_ops.get_primary_position(include_empty=True)
    if pos is not None:
        _log("cleanup close…")
        try:
            money_ops.close_position_full(pos, reply=_log, record_cycle=False)
        except Exception as e:
            _log(f"cleanup failed: {e}")
    _log("C4 live done")


if __name__ == "__main__":
    asyncio.run(main())
