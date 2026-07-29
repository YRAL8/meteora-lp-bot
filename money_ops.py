"""Shared money-path helpers for Telegram commands (suggest → swap → open/add/close)."""
from __future__ import annotations

import logging
import math
import os
from typing import Any, Callable

import bot_config
import meteora_cycle_journal as cycle_journal
import meteora_exec
import meteora_ops
import range_state
from meteora_exec import MeteoraExecError
from position_state import clear_last_position, save_last_position
from reopen_pending import set_reopen_pending
from telegram_notify import format_exec_replies

log = logging.getLogger(__name__)

FEE_RESERVE_SOL = float(os.getenv("FEE_RESERVE_SOL", "0.02"))
MIN_SOL_BALANCE = float(os.getenv("MIN_SOL_BALANCE", "0.05"))


def ops_kwargs() -> dict[str, Any]:
    net = bot_config.effective_network()
    return {
        "pool": bot_config.pool_pubkey(),
        "rpc": bot_config.effective_rpc(net),
    }


def exec_kwargs() -> dict[str, Any]:
    net = bot_config.effective_network()
    extra_env = None
    if net == "mainnet" and not bot_config.DRY_RUN:
        extra_env = {"METEORA_ALLOW_MAINNET": "1"}
    return {
        "network": net,
        "send": True,
        "pool": bot_config.pool_pubkey(),
        "rpc": bot_config.effective_rpc(net),
        "extra_env": extra_env,
    }


def owner() -> str:
    return bot_config.wallet_pubkey()


def bin_prices(
    active_id: int, active_price: float, bin_step: int, lo: int, hi: int
) -> tuple[float, float]:
    growth = 1 + bin_step / 10_000
    return (
        active_price * (growth ** (lo - active_id)),
        active_price * (growth ** (hi - active_id)),
    )


def list_open_positions() -> list[dict]:
    payload = meteora_ops.list_positions(owner(), **ops_kwargs())
    return list(payload.get("positions") or [])


def get_primary_position(*, include_empty: bool = False) -> dict | None:
    """Return first on-chain position. Zero-liq ghosts are ignored by default
    (SDK cannot close them after a prior full withdraw — binId crash)."""
    positions = list_open_positions()
    for p in positions:
        sol = float(p.get("sol") or 0)
        usdc = float(p.get("usdc") or 0)
        if include_empty or sol > 0 or usdc > 0:
            return p
    return None


def position_value_usd(pos: dict, usdc_per_sol: float) -> float:
    return float(pos.get("sol") or 0) * usdc_per_sol + float(pos.get("usdc") or 0)


def apply_swap_suggestion(suggestion: dict | None, reply: Callable[[str], Any]) -> None:
    if not suggestion:
        return
    side = suggestion.get("side")
    amount = float(suggestion.get("amount") or 0)
    if not side or amount <= 0:
        return
    reply(f"🔄 Своп {side} amount={amount}…")
    try:
        payload = meteora_exec.exec_swap(owner(), side, amount, **exec_kwargs())
    except MeteoraExecError as e:
        reply(f"⚠️ Своп не удался ({e}) — продолжаю с текущим балансом кошелька")
        return
    reply(format_exec_replies(payload))
    price = float(
        meteora_ops.pool_info(**ops_kwargs()).get("usdcPerSol") or 0
    )
    direction = "SOL_TO_USDC" if side == "sol-to-usdc" else "USDC_TO_SOL"
    cycle_journal.safe_call(
        cycle_journal.get_default_journal().record_swap_success,
        direction=direction,
        amount_in=amount,
        price=price,
    )


def suggest_for_budget(
    budget_usdc: float,
    *,
    half_width: int | None = None,
    min_bin_id: int | None = None,
    max_bin_id: int | None = None,
) -> dict:
    return meteora_ops.suggest_amounts(
        owner(),
        budget_usdc=budget_usdc,
        budget_sol=0.0,
        half_width=half_width,
        min_bin_id=min_bin_id,
        max_bin_id=max_bin_id,
        **ops_kwargs(),
    )


def open_with_budget(
    budget_usdc: float,
    *,
    half_width: int | None = None,
    reply: Callable[[str], Any],
) -> dict:
    half = half_width if half_width is not None else range_state.current_half_width()
    pool_meta = meteora_ops.pool_info(**ops_kwargs())
    max_bins = int(pool_meta["maxBinsPerPosition"])
    try:
        range_state.assert_half_within_limit(half, max_bins)
    except range_state.HalfWidthTooWideError as e:
        reply(
            f"❌ half_width={e.half_width} превышает потолок {e.max_half_width} "
            f"(одна позиция ≤ {e.max_bins_per_position} ячеек). "
            "Не открываю. Смени /setrange или DEFAULT_RANGE_HALF."
        )
        raise
    suggestion = suggest_for_budget(budget_usdc, half_width=half)
    need_sol = float(suggestion.get("needSol") or 0)
    need_usdc = float(suggestion.get("needUsdc") or 0)
    params = suggestion.get("params") or {}
    min_bin = int(params.get("minBinId"))
    max_bin = int(params.get("maxBinId"))
    apply_swap_suggestion(suggestion.get("swapSuggestion"), reply)
    reply(f"🆕 Открываю позицию: {need_sol:.6f} SOL + {need_usdc:.4f} USDC…")
    payload = meteora_exec.exec_open(
        owner(),
        need_sol,
        need_usdc,
        min_bin_id=min_bin,
        max_bin_id=max_bin,
        **exec_kwargs(),
    )
    reply(format_exec_replies(payload))
    pk = (payload.get("params") or {}).get("positionPubkey")
    if not pk:
        # fallback: list positions
        pos = get_primary_position()
        pk = pos["pubkey"] if pos else None
    if pk:
        save_last_position(str(pk), bot_config.pool_pubkey())
        pool = meteora_ops.pool_info(**ops_kwargs())
        price = float(pool.get("usdcPerSol") or 0)
        active_id = int(pool["activeId"])
        bin_step = int(pool["binStep"])
        lo_p, hi_p = bin_prices(active_id, price, bin_step, min_bin, max_bin)
        # Prefer on-chain amounts after open
        pos = None
        for p in list_open_positions():
            if p.get("pubkey") == pk:
                pos = p
                break
        sol_qty = float((pos or {}).get("sol") or need_sol)
        usdc_qty = float((pos or {}).get("usdc") or need_usdc)
        cycle_journal.safe_call(
            cycle_journal.get_default_journal().on_open,
            position_pubkey=str(pk),
            range_width_pct=range_state.current_range_pct(),
            lower_bin_id=min_bin,
            upper_bin_id=max_bin,
            lower_price=lo_p,
            upper_price=hi_p,
            open_price=price,
            open_sol_qty=sol_qty,
            open_usdc_qty=usdc_qty,
            open_position_value_usd=sol_qty * price + usdc_qty,
        )
    return payload


def add_with_budget(
    position: dict, budget_usdc: float, *, reply: Callable[[str], Any]
) -> dict:
    lo = int(position["lowerBinId"])
    hi = int(position["upperBinId"])
    suggestion = suggest_for_budget(
        budget_usdc, min_bin_id=lo, max_bin_id=hi
    )
    need_sol = float(suggestion.get("needSol") or 0)
    need_usdc = float(suggestion.get("needUsdc") or 0)
    apply_swap_suggestion(suggestion.get("swapSuggestion"), reply)
    reply(f"💧 Доливаю {need_sol:.6f} SOL + {need_usdc:.4f} USDC…")
    payload = meteora_exec.exec_add(
        owner(),
        position["pubkey"],
        need_sol,
        need_usdc,
        allow_multi_tx=True,
        **exec_kwargs(),
    )
    reply(format_exec_replies(payload))
    cycle_journal.safe_call(
        cycle_journal.get_default_journal().mark_add_liquidity_incomplete
    )
    return payload


def compute_max_addliquidity_usdc(position: dict) -> float:
    """Port of Orca compute_max_addliquidity_usdc — SOL leg is usually the limit."""
    bal = meteora_ops.balances(owner(), **ops_kwargs())
    usdc_balance = float((bal.get("usdc") or {}).get("ui") or 0)
    sol_balance = float((bal.get("sol") or {}).get("ui") or 0)
    if usdc_balance <= 0:
        return 0.0
    # Reference: how much SOL is needed for $1 USDC budget into this range.
    ref = suggest_for_budget(
        1.0,
        min_bin_id=int(position["lowerBinId"]),
        max_bin_id=int(position["upperBinId"]),
    )
    need_sol_per_usd = float(ref.get("needSol") or 0)
    if need_sol_per_usd <= 0:
        return usdc_balance
    usable_sol = max(0.0, sol_balance - MIN_SOL_BALANCE)
    max_by_sol = usable_sol / need_sol_per_usd
    return max(0.0, min(usdc_balance, max_by_sol) * 0.98)


def _position_has_liquidity(position: dict) -> bool:
    sol = float(position.get("sol") or 0)
    usdc = float(position.get("usdc") or 0)
    if sol > 0 or usdc > 0:
        return True
    raw = position.get("raw") or {}
    tx = str(raw.get("totalXAmount") or "0")
    ty = str(raw.get("totalYAmount") or "0")
    return tx not in ("0", "") or ty not in ("0", "")


def close_position_full(
    position: dict, *, reply: Callable[[str], Any], record_cycle: bool = True
) -> None:
    """Full close: removeLiquidity+claim+close, or closePositionIfEmpty if empty.

    Do NOT withdraw separately first on a live position — that leaves an empty
    account; empty accounts must use exec-close-empty, not removeLiquidity.
    """
    pk = position["pubkey"]
    pool = meteora_ops.pool_info(**ops_kwargs())
    price = float(pool.get("usdcPerSol") or 0)
    fees = position.get("fees") or {}
    close_value = position_value_usd(position, price)
    empty = not _position_has_liquidity(position)

    if record_cycle and not empty:
        cycle_journal.safe_call(
            cycle_journal.get_default_journal().capture_close_snapshot,
            close_price=price,
            close_position_value_usd=close_value,
            fees_sol=float(fees.get("sol") or 0),
            fees_usdc=float(fees.get("usdc") or 0),
            position_pubkey=pk,
        )

    if empty:
        reply("🔒 Закрываю пустую позицию (closePositionIfEmpty, возврат ренты)…")
        cl = meteora_exec.exec_close_empty(owner(), pk, **exec_kwargs())
    else:
        reply("🔒 Закрываю позицию (withdraw 100% + claim + close)…")
        cl = meteora_exec.exec_close(owner(), pk, **exec_kwargs())
    reply(format_exec_replies(cl))
    clear_last_position()

    if record_cycle and not empty:
        cycle_journal.safe_call(
            cycle_journal.get_default_journal().finalize_pending_cycle
        )


def rebalance_position(
    position: dict,
    *,
    reply: Callable[[str], Any],
    crash_after_close: bool = False,
) -> dict | None:
    """Close → mark reopen_pending → swap/open new range around current activeId."""
    pk = position["pubkey"]
    pool = meteora_ops.pool_info(**ops_kwargs())
    price = float(pool.get("usdcPerSol") or 0)
    close_value = position_value_usd(position, price)

    # Flag BEFORE close — same discipline as Orca set_rebalance_reopen_pending(True)
    set_reopen_pending(
        True,
        meta={"closed_position": pk, "close_value_usd": close_value, "price": price},
    )

    close_position_full(position, reply=reply, record_cycle=True)

    if crash_after_close or os.environ.get("METEORA_CRASH_AFTER_CLOSE") == "1":
        reply("💥 crash_after_close: останавливаюсь с reopen_pending")
        raise SystemExit(42)

    # Budget for reopen = wallet USDC-equivalent after close (haircut 2%).
    bal = meteora_ops.balances(owner(), **ops_kwargs())
    sol_ui = float((bal.get("sol") or {}).get("ui") or 0)
    usdc_ui = float((bal.get("usdc") or {}).get("ui") or 0)
    usable_sol = max(0.0, sol_ui - MIN_SOL_BALANCE)
    budget = (usable_sol * price + usdc_ui) * 0.98
    if budget <= 0:
        reply("❌ Недостаточно средств для реоткрытия после закрытия.")
        return None

    reply(f"🔁 Реоткрытие на ~${budget:.2f} USDC-эквивалента…")
    payload = open_with_budget(budget, reply=reply)
    set_reopen_pending(False)
    reply("✅ Ребаланс завершён — reopen_pending снят.")
    return payload
