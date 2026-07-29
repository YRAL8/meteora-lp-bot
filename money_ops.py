"""Shared money-path helpers for Telegram commands (suggest → swap → open/add/close)."""
from __future__ import annotations

import logging
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
from telegram_notify import escape_html, format_exec_replies

log = logging.getLogger(__name__)

FEE_RESERVE_SOL = float(os.getenv("FEE_RESERVE_SOL", "0.02"))
# Prefer bot_config (raised default 0.08); keep env override for scripts.
MIN_SOL_BALANCE = float(
    os.getenv("MIN_SOL_BALANCE", str(getattr(bot_config, "MIN_SOL_BALANCE", 0.08)))
)

# Refuse opens smaller than this after scaling (dust / failed-swap dead ends).
_MIN_OPEN_USD = 0.05


class SwapFailedOpenAborted(RuntimeError):
    """Swap failed and a proportion-correct open was not possible — do not open crooked."""


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
        "priority_fee": int(bot_config.PRIORITY_FEE_MICROLAMPORTS),
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


class StepNotes:
    """Копит пояснения по ходу операции, чтобы отправить их ОДНИМ сообщением.

    Раньше каждый шаг слал отдельное сообщение в Telegram: одно открытие
    порождало восемь штук с прыгающими числами, читать это было тяжело.
    Теперь шаги накапливаются, а наружу уходит один короткий свод.
    """

    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, text: str) -> None:
        self.items.append(text)

    def __call__(self, text: str) -> None:  # совместимость с сигнатурой reply
        self.add(text)

    def render(self) -> str:
        if not self.items:
            return ""
        if len(self.items) == 1:
            return self.items[0]
        return "\n".join(f"• {i}" for i in self.items)


def apply_max_position_cap(
    requested_usd: float,
    *,
    reply: Callable[[str], Any],
    action: str = "открываю",
) -> float:
    """Clamp requested total position USD to MAX_POSITION_USD; announce cuts."""
    cap = bot_config.MAX_POSITION_USD
    if cap is None:
        return float(requested_usd)
    if requested_usd <= cap + 1e-9:
        return float(requested_usd)
    reply(
        f"⚠️ Бюджет ограничен потолком MAX_POSITION_USD=${cap:g}, "
        f"{action} на ${cap:.2f} вместо ${requested_usd:.2f}"
    )
    return float(cap)


def apply_add_cap(
    add_usd: float,
    position_value_usd: float,
    *,
    reply: Callable[[str], Any],
) -> float:
    """Clamp add so resulting position ≤ MAX_POSITION_USD."""
    cap = bot_config.MAX_POSITION_USD
    if cap is None:
        return float(add_usd)
    room = max(0.0, cap - position_value_usd)
    if add_usd <= room + 1e-9:
        return float(add_usd)
    reply(
        f"⚠️ Доливка ограничена потолком MAX_POSITION_USD=${cap:g} "
        f"(позиция сейчас ${position_value_usd:.2f}): "
        f"добавляю ${room:.2f} вместо ${add_usd:.2f}"
    )
    return float(room)


def apply_swap_suggestion(suggestion: dict | None, reply: Callable[[str], Any]) -> bool:
    """Execute swapSuggestion if present. Returns False if swap was needed but failed."""
    if not suggestion:
        return True
    side = suggestion.get("side")
    amount = float(suggestion.get("amount") or 0)
    if not side or amount <= 0:
        return True
    human_side = "SOL → USDC" if side == "sol-to-usdc" else "USDC → SOL"
    try:
        payload = meteora_exec.exec_swap(owner(), side, amount, **exec_kwargs())
    except MeteoraExecError as e:
        # Самая частая причина на тонком пуле — нехватка встречной ликвидности;
        # переводим на человеческий, полный текст всё равно уходит в лог.
        text = str(e)
        human = (
            "в пуле не хватило встречной ликвидности"
            if "insufficient liquidity" in text.lower()
            else text
        )
        log.warning("swap failed: %s", text)
        # reply уходит в Telegram с parse_mode=HTML — экранируем внешний текст.
        reply(f"своп {human_side} не прошёл: {escape_html(human)}")
        return False
    assert_exec_fully_confirmed(payload)
    reply(f"своп {human_side}: {amount:.6f} ✅")
    price = float(meteora_ops.pool_info(**ops_kwargs()).get("usdcPerSol") or 0)
    direction = "SOL_TO_USDC" if side == "sol-to-usdc" else "USDC_TO_SOL"
    cycle_journal.safe_call(
        cycle_journal.get_default_journal().record_swap_success,
        direction=direction,
        amount_in=amount,
        price=price,
    )
    return True


def suggest_for_budget(
    position_usd: float,
    *,
    half_width: int | None = None,
    min_bin_id: int | None = None,
    max_bin_id: int | None = None,
) -> dict:
    """Suggest deposit legs for a TOTAL position of ≈ ``position_usd`` USD.

    ``suggest-amounts`` treats budget as total position size (C6), not a single leg.
    """
    return meteora_ops.suggest_amounts(
        owner(),
        budget_usdc=float(position_usd),
        budget_sol=None,
        half_width=half_width,
        min_bin_id=min_bin_id,
        max_bin_id=max_bin_id,
        **ops_kwargs(),
    )


def _scale_legs_to_wallet(
    need_sol: float, need_usdc: float, usable_sol: float, usdc_have: float
) -> tuple[float, float, float]:
    """Uniform scale so both legs fit the wallet; keeps Spot proportion."""
    scales = [1.0]
    if need_sol > 1e-12:
        scales.append(usable_sol / need_sol)
    if need_usdc > 1e-12:
        scales.append(usdc_have / need_usdc)
    scale = min(scales)
    if scale <= 0:
        return 0.0, 0.0, 0.0
    return need_sol * scale, need_usdc * scale, scale


def _wallet_balances() -> tuple[float, float, float]:
    bal = meteora_ops.balances(owner(), **ops_kwargs())
    sol_have = float((bal.get("sol") or {}).get("ui") or 0)
    usdc_have = float((bal.get("usdc") or {}).get("ui") or 0)
    # Prefer TS field that already subtracts fee reserve + position rent.
    sao = bal.get("solAvailableForOpen")
    if sao is not None:
        usable_sol = max(0.0, float(sao))
    else:
        usable_sol = max(0.0, sol_have - MIN_SOL_BALANCE)
    return sol_have, usdc_have, usable_sol


def assert_exec_fully_confirmed(payload: dict) -> None:
    """Refuse to treat confirmation-unknown as success (belt after TS ok:false)."""
    if payload.get("confirmationUnknown"):
        raise MeteoraExecError(
            {
                "ok": False,
                "error": "confirmation unknown — do not retry blindly",
                "stage": "confirm-unknown",
                "confirmationUnknown": True,
                **{k: payload.get(k) for k in ("signatures", "sends", "action")},
            }
        )
    for s in payload.get("sends") or []:
        st = s.get("status")
        if st and st != "confirmed":
            raise MeteoraExecError(
                {
                    "ok": False,
                    "error": f"tx status={st!r} — not confirmed",
                    "stage": "confirm-unknown" if st == "unknown" else "confirm",
                    "confirmationUnknown": st == "unknown",
                    "sends": payload.get("sends"),
                    "signatures": payload.get("signatures"),
                }
            )


def open_with_budget(
    position_usd: float,
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

    # Промежуточные шаги копим и отправляем одним сообщением в самом конце.
    notes = StepNotes()

    requested = float(position_usd)
    budget = apply_max_position_cap(requested, reply=notes, action="открываю")
    if budget <= 0:
        reply("❌ Бюджет открытия ≤ 0 — не открываю.")
        raise RuntimeError("open_with_budget: non-positive budget")

    suggestion = suggest_for_budget(budget, half_width=half)
    need_sol = float(suggestion.get("needSol") or 0)
    need_usdc = float(suggestion.get("needUsdc") or 0)
    params = suggestion.get("params") or {}
    min_bin = int(params.get("minBinId"))
    max_bin = int(params.get("maxBinId"))
    price = float(params.get("usdcPerSol") or pool_meta.get("usdcPerSol") or 0)

    swap_ok = apply_swap_suggestion(suggestion.get("swapSuggestion"), notes)
    _, usdc_have, usable_sol = _wallet_balances()

    if not swap_ok:
        # Do not open with the pre-swap mix. Rebuild a correct-proportion deposit
        # that fits the wallet as-is (smaller position), or abort.
        price_now = float(
            meteora_ops.pool_info(**ops_kwargs()).get("usdcPerSol") or price or 0
        )
        # Сколько позиция вообще может стоить при текущем кошельке, без свопа.
        wallet_capacity = (usable_sol * price_now + usdc_have) * 0.95
        # ВАЖНО: не раздувать заказ. Раньше здесь бралась вся ёмкость кошелька, и
        # неудачный своп превращал "/open 2" в попытку открыть на весь баланс
        # (в живом прогоне — $115.77 вместо $2). Уменьшать запрошенное можно,
        # увеличивать — никогда.
        wallet_usd = min(budget, wallet_capacity)
        wallet_usd = apply_max_position_cap(
            wallet_usd, reply=notes, action="открываю после неудачного свопа"
        )
        if wallet_usd < _MIN_OPEN_USD:
            reply(
                "❌ Своп не удался, а на кошельке недостаточно средств для "
                "позиции с правильной пропорцией. Деньги остаются на кошельке; "
                "не открываю кривую позицию. При ребалансе reopen_pending "
                "останется — дожми вручную после выравнивания баланса."
            )
            raise SwapFailedOpenAborted("swap failed; cannot open proportionally")
        notes.add("пересчитал пропорцию под баланс кошелька, без кривой ноги")
        suggestion = suggest_for_budget(wallet_usd, half_width=half)
        need_sol = float(suggestion.get("needSol") or 0)
        need_usdc = float(suggestion.get("needUsdc") or 0)
        params = suggestion.get("params") or params
        min_bin = int(params.get("minBinId"))
        max_bin = int(params.get("maxBinId"))
        price = float(params.get("usdcPerSol") or price_now)
        _, usdc_have, usable_sol = _wallet_balances()

    need_sol, need_usdc, scale = _scale_legs_to_wallet(
        need_sol, need_usdc, usable_sol, usdc_have
    )
    total_usd = need_sol * price + need_usdc
    if scale < 0.999:
        notes.add(f"урезал депозит под баланс кошелька (×{scale:.3f})")
    if total_usd < _MIN_OPEN_USD or (need_sol <= 0 and need_usdc <= 0):
        reply(
            "❌ Нечего вносить с правильной пропорцией после выравнивания кошелька. "
            "Не открываю."
        )
        raise SwapFailedOpenAborted("zero/dust deposit after proportional scale")

    # Одно сообщение вместо цепочки: что открываем и почему сумма отличается.
    head = f"🆕 Открываю позицию на ~${total_usd:.2f}"
    if abs(total_usd - requested) > 0.01:
        head += f" (просили ${requested:.2f})"
    body = f"{need_sol:.6f} SOL + ${need_usdc:.4f} USDC"
    detail = notes.render()
    reply(f"{head}\n{body}" + (f"\n{detail}" if detail else ""))
    payload = meteora_exec.exec_open(
        owner(),
        need_sol,
        need_usdc,
        min_bin_id=min_bin,
        max_bin_id=max_bin,
        **exec_kwargs(),
    )
    assert_exec_fully_confirmed(payload)
    reply(format_exec_replies(payload))
    pk = (payload.get("params") or {}).get("positionPubkey")
    if not pk:
        pos = get_primary_position()
        pk = pos["pubkey"] if pos else None
    if pk:
        save_last_position(str(pk), bot_config.pool_pubkey())
        pool = meteora_ops.pool_info(**ops_kwargs())
        price = float(pool.get("usdcPerSol") or 0)
        active_id = int(pool["activeId"])
        bin_step = int(pool["binStep"])
        lo_p, hi_p = bin_prices(active_id, price, bin_step, min_bin, max_bin)
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
        # Manual recovery after aborted rebalance: successful open clears the flag.
        from reopen_pending import is_reopen_pending

        if is_reopen_pending():
            set_reopen_pending(False)
            reply("✅ reopen_pending снят после успешного открытия.")
    return payload


def add_with_budget(
    position: dict, add_usd: float, *, reply: Callable[[str], Any]
) -> dict:
    lo = int(position["lowerBinId"])
    hi = int(position["upperBinId"])
    pool = meteora_ops.pool_info(**ops_kwargs())
    price = float(pool.get("usdcPerSol") or 0)
    cur_val = position_value_usd(position, price)
    # Шаги копим и отдаём одним сообщением — как в open_with_budget.
    notes = StepNotes()
    requested = float(add_usd)
    budget = apply_add_cap(requested, cur_val, reply=notes)
    if budget <= 0:
        reply("❌ Нечего доливать с учётом потолка MAX_POSITION_USD.")
        raise RuntimeError("add_with_budget: non-positive budget after cap")

    suggestion = suggest_for_budget(budget, min_bin_id=lo, max_bin_id=hi)
    need_sol = float(suggestion.get("needSol") or 0)
    need_usdc = float(suggestion.get("needUsdc") or 0)
    swap_ok = apply_swap_suggestion(suggestion.get("swapSuggestion"), notes)
    _, usdc_have, usable_sol = _wallet_balances()

    if not swap_ok:
        # Одно сообщение вместо трёх: что случилось и что делать.
        detail = notes.render()
        reply(
            "❌ Доливка отменена — не доливаю кривой ногой.\n"
            + (f"{detail}\n" if detail else "")
            + "Выровняй баланс кошелька и повтори."
        )
        raise SwapFailedOpenAborted("swap failed on add")

    need_sol, need_usdc, scale = _scale_legs_to_wallet(
        need_sol, need_usdc, usable_sol, usdc_have
    )
    total_usd = need_sol * price + need_usdc
    if total_usd < _MIN_OPEN_USD:
        reply("❌ После свопа нечего доливать с правильной пропорцией.")
        raise SwapFailedOpenAborted("dust add after scale")
    if scale < 0.999:
        notes.add(f"урезал доливку под баланс кошелька (×{scale:.3f})")

    head = f"💧 Доливаю ~${total_usd:.2f}"
    if abs(total_usd - requested) > 0.01:
        head += f" (просили ${requested:.2f})"
    detail = notes.render()
    reply(
        f"{head}\n{need_sol:.6f} SOL + ${need_usdc:.4f} USDC"
        + (f"\n{detail}" if detail else "")
    )
    payload = meteora_exec.exec_add(
        owner(),
        position["pubkey"],
        need_sol,
        need_usdc,
        allow_multi_tx=True,
        **exec_kwargs(),
    )
    assert_exec_fully_confirmed(payload)
    reply(format_exec_replies(payload))
    cycle_journal.safe_call(
        cycle_journal.get_default_journal().mark_add_liquidity_incomplete
    )
    return payload


def compute_max_addliquidity_usdc(position: dict) -> float:
    """Max TOTAL USD that can be added given wallet legs and MAX_POSITION_USD."""
    _, usdc_balance, usable_sol = _wallet_balances()
    pool = meteora_ops.pool_info(**ops_kwargs())
    price = float(pool.get("usdcPerSol") or 0)

    ref = suggest_for_budget(
        1.0,
        min_bin_id=int(position["lowerBinId"]),
        max_bin_id=int(position["upperBinId"]),
    )
    need_sol_per_usd = float(ref.get("needSol") or 0)
    need_usdc_per_usd = float(ref.get("needUsdc") or 0)

    limits: list[float] = []
    if need_sol_per_usd > 1e-12:
        limits.append(usable_sol / need_sol_per_usd)
    if need_usdc_per_usd > 1e-12:
        limits.append(usdc_balance / need_usdc_per_usd)
    if not limits:
        return 0.0
    max_add = min(limits) * 0.98

    cap = bot_config.MAX_POSITION_USD
    if cap is not None:
        room = max(0.0, cap - position_value_usd(position, price))
        max_add = min(max_add, room)
    return max(0.0, max_add)


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
    position: dict,
    *,
    reply: Callable[[str], Any],
    record_cycle: bool = True,
    trigger: str = "manual",
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
            trigger=trigger,
        )

    if empty:
        reply("🔒 Закрываю пустую позицию (closePositionIfEmpty, возврат ренты)…")
        cl = meteora_exec.exec_close_empty(owner(), pk, **exec_kwargs())
    else:
        reply("🔒 Закрываю позицию (withdraw 100% + claim + close)…")
        cl = meteora_exec.exec_close(owner(), pk, **exec_kwargs())
    assert_exec_fully_confirmed(cl)
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
    auto: bool = False,
) -> dict | None:
    """Close → mark reopen_pending → swap/open new range around current activeId."""
    pk = position["pubkey"]
    pool = meteora_ops.pool_info(**ops_kwargs())
    price = float(pool.get("usdcPerSol") or 0)
    close_value = position_value_usd(position, price)
    trigger = "auto" if auto else "manual"

    set_reopen_pending(
        True,
        meta={
            "closed_position": pk,
            "close_value_usd": close_value,
            "price": price,
            "trigger": trigger,
        },
    )

    close_position_full(
        position, reply=reply, record_cycle=True, trigger=trigger
    )

    if crash_after_close or os.environ.get("METEORA_CRASH_AFTER_CLOSE") == "1":
        reply("💥 crash_after_close: останавливаюсь с reopen_pending")
        raise SystemExit(42)

    bal = meteora_ops.balances(owner(), **ops_kwargs())
    sol_ui = float((bal.get("sol") or {}).get("ui") or 0)
    usdc_ui = float((bal.get("usdc") or {}).get("ui") or 0)
    sao = bal.get("solAvailableForOpen")
    if sao is not None:
        usable_sol = max(0.0, float(sao))
    else:
        usable_sol = max(0.0, sol_ui - MIN_SOL_BALANCE)
    budget = (usable_sol * price + usdc_ui) * 0.98
    if budget <= 0:
        reply("❌ Недостаточно средств для реоткрытия после закрытия.")
        return None

    # Cap applied inside open_with_budget (with announcement); preview here too.
    budget = apply_max_position_cap(budget, reply=reply, action="реоткрываю")
    reply(f"🔁 Реоткрытие на ~${budget:.2f}…")
    try:
        payload = open_with_budget(budget, reply=reply)
    except SwapFailedOpenAborted as e:
        reply(
            f"❌ Реоткрытие отменено ({e}). reopen_pending остаётся — "
            "выровняй кошелёк и дожми /open или /rebalance confirm."
        )
        return None
    set_reopen_pending(False)
    reply("✅ Ребаланс завершён — reopen_pending снят.")
    return payload
