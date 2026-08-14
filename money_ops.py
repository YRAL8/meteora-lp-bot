"""Shared money-path helpers for Telegram commands (suggest → swap → open/add/close)."""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

import bot_config
import bot_state
import meteora_cycle_journal as cycle_journal
import meteora_exec
import meteora_ops
import range_state
from meteora_exec import MeteoraExecError
from position_state import clear_last_position, save_last_position
from reopen_pending import set_reopen_pending, ReopenPendingWriteError
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


class SwapOutcomeUnknown(RuntimeError):
    """Swap was signed/sent (or may have been); do not start the next money tx."""

    def __init__(self, err: MeteoraExecError):
        self.exec_error = err
        super().__init__(str(err))


class OpenMateriallyUndersized(RuntimeError):
    """Open would be far below the requested budget — not a successful reopen."""

    def __init__(self, requested_usd: float, actual_usd: float):
        self.requested_usd = requested_usd
        self.actual_usd = actual_usd
        super().__init__(
            f"open undersized: ${actual_usd:.2f} << requested ${requested_usd:.2f}"
        )


class StopRequested(RuntimeError):
    """Owner froze the bot (/stop); do not start the next money transaction."""


# Stages from ts fail() that happen before any signature is created.
_PRE_SIGN_STAGES = frozenset(
    {
        "parseArgs",
        "build",
        "simulate",
        "simulation",
        "journal",
        "network",
    }
)

# Refuse to treat an open as success if wallet scaling cuts below this fraction.
_OPEN_SUCCESS_MIN_RATIO = 0.85


def require_not_frozen(*, reply: Callable[[str], Any], where: str) -> None:
    """Cooperative cancel: already-sent txs finish; do not begin the next one."""
    if bot_state.bot_frozen:
        reply(
            f"🛑 Остановлено по /stop ({where}). "
            "Уже отправленная транзакция дойдёт до конца; новые не отправляю."
        )
        raise StopRequested(where)


def require_max_position_on_mainnet(*, reply: Callable[[str], Any]) -> None:
    """Wallet-sized opens on mainnet need an explicit ceiling (audit C9)."""
    if bot_config.effective_network() != "mainnet":
        return
    if bot_config.MAX_POSITION_USD is not None:
        return
    reply(
        "❌ На mainnet без MAX_POSITION_USD нельзя открывать позицию «на весь "
        "кошелёк». Задай потолок в .env (например MAX_POSITION_USD=10) и "
        "перезапусти бота."
    )
    raise RuntimeError("mainnet requires MAX_POSITION_USD for wallet-sized open")


def _signatures_from_exec_error(err: MeteoraExecError) -> list[str]:
    """Collect any on-wire signatures from an exec error payload."""
    payload = err.payload or {}
    sigs = payload.get("signatures") or []
    if sigs:
        return [str(s) for s in sigs]
    out: list[str] = []
    for s in payload.get("sends") or []:
        if s.get("signature"):
            out.append(str(s["signature"]))
    return out


def _exec_proven_not_sent(err: MeteoraExecError) -> bool:
    """True only when the exec response proves nothing was signed/submitted.

    No signatures is necessary but not sufficient: stage ``send`` without
    signatures (old fail() shape) still means the tx may be in flight.
    Only pre-sign stages count as a clean miss. Empty stage + no signatures
    → clean miss (fail() always sets stage; mocks/legacy omit it).
    """
    if _signatures_from_exec_error(err):
        return False
    payload = err.payload or {}
    if payload.get("confirmationUnknown"):
        return False
    stage = str(payload.get("stage") or "")
    if stage in _PRE_SIGN_STAGES:
        return True
    if stage in ("send", "confirm", "confirm-unknown", "runtime"):
        return False
    if not stage:
        return True
    return False


# Back-compat alias used by older tests/callers.
_close_proven_not_sent = _exec_proven_not_sent


def _reply_close_outcome_unknown(
    reply: Callable[[str], Any],
    *,
    signatures: list[str] | None = None,
    detail: str | None = None,
) -> None:
    sigs = signatures or []
    sig_line = (
        ", ".join(sigs[:3])
        if sigs
        else "(подписи в ответе нет — смотри /status journal)"
    )
    lines = [
        "⚠️ Исход закрытия НЕЯСЕН.",
        "Позиции может уже не быть — деньги могут лежать на кошельке.",
        f"Подпись: <code>{escape_html(sig_line)}</code>",
    ]
    if detail:
        lines.append(f"Деталь: <code>{escape_html(detail)}</code>")
    lines.append(
        "Автоматика стоит, пока не выясним. "
        "/status journal покажет, есть ли подпись в журнале "
        "(дальше /withdraw confirm при необходимости)."
    )
    reply("\n".join(lines))


def _reply_money_outcome_unknown(
    reply: Callable[[str], Any],
    *,
    what: str,
    signatures: list[str] | None = None,
    detail: str | None = None,
) -> None:
    sigs = signatures or []
    sig_line = (
        ", ".join(sigs[:3])
        if sigs
        else "(подписи в ответе нет — смотри /status journal)"
    )
    lines = [
        f"⚠️ Исход {what} НЕЯСЕН — следующую транзакцию не отправляю.",
        f"Подпись: <code>{escape_html(sig_line)}</code>",
    ]
    if detail:
        lines.append(f"Деталь: <code>{escape_html(detail)}</code>")
    lines.append(
        "Автоматика стоит. Проверь /status journal; "
        "не открывай новую позицию, пока не убедишься, что старой уже нет."
    )
    reply("\n".join(lines))


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
    """Execute swapSuggestion if present. Returns False if swap was needed but failed cleanly.

    Unknown/signed outcomes raise SwapOutcomeUnknown — caller must not send the next tx.
    """
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
        text = str(e)
        human = (
            "в пуле не хватило встречной ликвидности"
            if "insufficient liquidity" in text.lower()
            else text
        )
        log.warning("swap failed: %s", text)
        if _exec_proven_not_sent(e):
            reply(f"своп {human_side} не прошёл: {escape_html(human)}")
            return False
        _reply_money_outcome_unknown(
            reply,
            what=f"свопа ({human_side})",
            signatures=_signatures_from_exec_error(e),
            detail=text[:200],
        )
        raise SwapOutcomeUnknown(e) from e
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


def sol_short_after_planned_swap(
    *,
    est_sol: float,
    est_usdc: float,
    est_budget: float,
    need_sol: float | None,
    price: float,
) -> bool:
    """True if even after USDC→SOL swap we cannot fund needSol / budget.

    Used by the auto-rebalance SOL gate and post-close reopen check.
    """
    if est_budget < 0.05:
        return True
    if need_sol is None:
        # Degraded: cannot price needSol — only block if no SOL and no USDC to buy.
        return est_sol <= 1e-12 and est_usdc <= 1e-6
    if est_sol + 1e-12 >= need_sol:
        return False
    if price <= 0:
        return True
    deficit = need_sol - est_sol
    # 2% cushion for fees/slippage on the swap leg.
    need_usdc_for_swap = deficit * price * 1.02
    return est_usdc + 1e-12 < need_usdc_for_swap


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


# Typical rent-exempt for a new SPL token account when balances.ata.*.rentLamports
# is null (account does not exist yet). Not a money-path change — display only.
_MISSING_ATA_RENT_SOL = 0.00203928


def _missing_ata_rent_sol(bal: dict) -> tuple[float, list[str]]:
    """SOL rent still needed for USDC/WSOL ATAs that do not exist yet."""
    ata = bal.get("ata") or {}
    missing: list[str] = []
    total = 0.0
    for key, label in (("usdc", "USDC"), ("wsol", "WSOL")):
        info = ata.get(key) or {}
        if info.get("exists"):
            continue
        lamports = info.get("rentLamports")
        if lamports is not None:
            sol = float(lamports) / 1e9
        else:
            sol = _MISSING_ATA_RENT_SOL
        total += sol
        missing.append(label)
    return total, missing


def _funding_shortfalls_after_swap(
    *,
    need_sol: float,
    need_usdc: float,
    usable_sol: float,
    usdc_have: float,
    price: float,
    swap_suggestion: dict | None,
) -> list[str]:
    """Human fragments: how much SOL / USDC is still missing after the planned swap."""
    sol = float(usable_sol)
    usdc = float(usdc_have)
    if swap_suggestion and price > 0:
        side = swap_suggestion.get("side")
        amt = float(swap_suggestion.get("amount") or 0)
        if side == "usdc-to-sol" and amt > 0:
            usdc -= amt
            sol += amt / price
        elif side == "sol-to-usdc" and amt > 0:
            sol -= amt
            usdc += amt * price
    parts: list[str] = []
    if sol + 1e-9 < need_sol:
        d = need_sol - sol
        parts.append(f"{d:.4f} SOL (~${d * price:.2f})")
    if usdc + 1e-6 < need_usdc:
        d = need_usdc - usdc
        parts.append(f"${d:.2f} USDC")
    return parts


def format_swap_line(swap_suggestion: dict | None) -> str | None:
    """Human line for the swapSuggestion from suggest-amounts (no invented math)."""
    if not swap_suggestion:
        return None
    side = swap_suggestion.get("side")
    amount = float(swap_suggestion.get("amount") or 0)
    if not side or amount <= 0:
        return None
    if side == "usdc-to-sol":
        return f"Нужен обмен: {amount:.2f} USDC → SOL"
    if side == "sol-to-usdc":
        return f"Нужен обмен: {amount:.6f} SOL → USDC"
    return f"Нужен обмен: {side} {amount}"


# SDK calculatePositionSize: POSITION_MIN_SIZE for ≤ DEFAULT_BIN_PER_POSITION (70),
# then +POSITION_BIN_DATA_SIZE per extra bin. On-chain account adds 8-byte discriminator.
_SDK_DEFAULT_BINS_PER_POSITION = 70
_SDK_POSITION_MIN_SIZE = 8112
_SDK_POSITION_BIN_DATA_SIZE = 112
_ACCOUNT_DISCRIMINATOR_BYTES = 8
_RENT_EXEMPT_HEADER_BYTES = 128  # Solana rent is linear in (data_len + 128)


def position_onchain_size_bytes(bin_count: int) -> int:
    extra = max(0, int(bin_count) - _SDK_DEFAULT_BINS_PER_POSITION)
    return (
        _SDK_POSITION_MIN_SIZE
        + extra * _SDK_POSITION_BIN_DATA_SIZE
        + _ACCOUNT_DISCRIMINATOR_BYTES
    )


def position_rent_sol_for_bins(bin_count: int, rent_info: dict) -> float:
    """Position-account rent for ``bin_count``, scaled from balances() default quote.

    Uses the Solana rent-exempt linear rule (size+128) and the SDK size formula.
    When the requested width matches the quoted default, returns the quoted SOL
    unchanged so a 69-bin open does not drift from the RPC figure.
    """
    default_bins = int(rent_info.get("binCount") or 69)
    default_sol = float(rent_info.get("sol") or 0)
    if int(bin_count) == default_bins:
        return default_sol
    default_bytes = int(
        rent_info.get("onChainSizeBytes") or position_onchain_size_bytes(default_bins)
    )
    want_bytes = position_onchain_size_bytes(bin_count)
    lamports = float(rent_info.get("lamports") or default_sol * 1e9)
    if default_bytes <= 0:
        return default_sol
    return (
        lamports
        * (want_bytes + _RENT_EXEMPT_HEADER_BYTES)
        / (default_bytes + _RENT_EXEMPT_HEADER_BYTES)
        / 1e9
    )


def build_open_estimate(
    requested_usd: float,
    *,
    width_pct: float | None = None,
) -> str:
    """Read-only open quote for Telegram. Never signs or sends.

    ``width_pct`` is a one-shot override from the command line; when None, uses
    the saved /setrange setting. Does not mutate ``range_state``.
    """
    pool = meteora_ops.pool_info(**ops_kwargs())
    bal = meteora_ops.balances(owner(), **ops_kwargs())
    bin_step = int(pool["binStep"])
    max_bins = int(pool["maxBinsPerPosition"])
    active_id = int(pool["activeId"])
    price = float(pool.get("usdcPerSol") or 0)

    if width_pct is not None:
        half = range_state.half_width_for_pct(
            float(width_pct), bin_step, max_bins_per_position=max_bins
        )
        requested_pct = float(width_pct)
        width_source = "из команды (настройка не менялась)"
    else:
        half = range_state.current_half_width()
        requested_pct = range_state.current_range_pct()
        width_source = "из настройки /setrange"

    actual_pct = range_state.pct_from_half(half, bin_step)
    asked = range_state.asked_pct_note(actual_pct, requested_pct)
    lo = active_id - half
    hi = active_id + half
    width_bins = range_state.corridor_bins(half)
    lo_p, hi_p = bin_prices(active_id, price, bin_step, lo, hi)

    notes: list[str] = []

    def note(text: str) -> None:
        notes.append(text)

    budget = apply_max_position_cap(
        float(requested_usd), reply=note, action="смета"
    )
    # Cap message from apply_max_position_cap is already in notes.

    rent_info = bal.get("positionRentForDefaultOpen") or {}
    default_rent_sol = float(rent_info.get("sol") or 0)
    rent_sol = position_rent_sol_for_bins(width_bins, rent_info)
    fee_sol = float(bal.get("feeReserveSol") or FEE_RESERVE_SOL)
    ata_rent_sol, ata_missing = _missing_ata_rent_sol(bal)

    sol_have = float((bal.get("sol") or {}).get("ui") or 0)
    usdc_have = float((bal.get("usdc") or {}).get("ui") or 0)
    sao = bal.get("solAvailableForOpen")
    extra_rent = max(0.0, rent_sol - default_rent_sol)
    usable_sol = (
        max(0.0, float(sao) - extra_rent)
        if sao is not None
        else max(0.0, sol_have - fee_sol - rent_sol)
    )
    wallet_usd = sol_have * price + usdc_have

    suggestion = suggest_for_budget(budget, half_width=half)
    need_sol = float(suggestion.get("needSol") or 0)
    need_usdc = float(suggestion.get("needUsdc") or 0)
    swap_sug = suggestion.get("swapSuggestion")
    # Prefer price from suggest-amounts params when present (same RPC snapshot).
    params = suggestion.get("params") or {}
    if params.get("usdcPerSol"):
        price = float(params["usdcPerSol"])
        wallet_usd = sol_have * price + usdc_have
        lo_p, hi_p = bin_prices(active_id, price, bin_step, lo, hi)

    rent_usd = rent_sol * price
    fee_usd = fee_sol * price
    ata_usd = ata_rent_sol * price
    locked_usd = rent_usd + fee_usd + ata_usd
    free_after = wallet_usd - budget - locked_usd

    shortfalls = _funding_shortfalls_after_swap(
        need_sol=need_sol,
        need_usdc=need_usdc,
        usable_sol=usable_sol,
        usdc_have=usdc_have,
        price=price,
        swap_suggestion=swap_sug if isinstance(swap_sug, dict) else None,
    )
    short = bool(shortfalls)

    # Same wallet-capacity idea as open_with_budget after a failed swap.
    # NB: solAvailableForOpen is clamped at zero on the TS side, so it cannot
    # tell "exactly enough for the reserves" from "short of them". Recompute the
    # headroom unclamped: the deposit is payable in SOL only, so when SOL does
    # not cover reserves, no position of any size is possible and USDC alone
    # must not be reported as an openable amount.
    sol_headroom = sol_have - fee_sol - rent_sol - ata_rent_sol
    sol_missing_for_any_open = max(0.0, -sol_headroom)
    sol_for_lp = max(0.0, sol_headroom)
    affordable_now = (sol_for_lp * price + usdc_have) * 0.95
    if bot_config.MAX_POSITION_USD is not None:
        affordable_now = min(affordable_now, float(bot_config.MAX_POSITION_USD))
    if sol_missing_for_any_open > 0:
        affordable_now = 0.0

    lines: list[str] = [
        "🆕 <b>Открытие позиции — проверьте перед подтверждением</b>",
        "",
        f"Кошелёк: {sol_have:.4f} SOL + {usdc_have:.2f} USDC  "
        f"(всего ${wallet_usd:.2f})",
        "",
        f"Диапазон: ±{actual_pct:.2f}%{asked} — от ${lo_p:.2f} до ${hi_p:.2f}",
        f"          (сейчас ${price:.2f}, {width_bins} ячеек,",
        f"          ширина {width_source})",
        "",
    ]

    if short:
        if sol_missing_for_any_open > 0:
            # One statement, blocking fact first: two separate "не хватает"
            # lines with different numbers read as a contradiction.
            lines.append(
                f"❌ Открыть нельзя ничего: не хватает "
                f"{sol_missing_for_any_open:.4f} SOL "
                f"(~${sol_missing_for_any_open * price:.2f}) даже на резервы — "
                f"залог платится только в SOL, одним USDC его не закрыть.\n"
                f"На запрошенные ${budget:.2f} нужно "
                + " и ".join(shortfalls)
                + ". Смета ничего не отправляет."
            )
        else:
            lines.append(
                "❌ Не хватает: "
                + " и ".join(shortfalls)
                + ". Смета ничего не отправляет."
            )
            if affordable_now >= _MIN_OPEN_USD:
                lines.append(f"Сейчас хватит на ~${affordable_now:.2f}.")
        lines.append("")
        lines.append("Почему не хватает (резервы, которые нельзя вложить в пул):")
        lines.append(
            f"  • залог          {rent_sol:.4f} SOL = ${rent_usd:.2f}   "
            f"вернётся при закрытии"
        )
        if ata_missing:
            labels = " + ".join(ata_missing)
            lines.append(
                f"  • залог ATA ({labels})  {ata_rent_sol:.4f} SOL = ${ata_usd:.2f}   "
                f"вернётся при закрытии счетов"
            )
        lines.append(
            f"  • на комиссии    {fee_sol:.4f} SOL = ${fee_usd:.2f}   "
            f"тратится по копейке"
        )
        lines.append(
            f"  • запрошено в пул  ${budget:.2f}   "
            f"(этих денег на кошельке нет)"
        )
    else:
        lines.append("Куда уйдут деньги:")
        lines.append(f"  • в пул          ${budget:.2f}   работает")
        lines.append(
            f"  • залог          {rent_sol:.4f} SOL = ${rent_usd:.2f}   "
            f"вернётся при закрытии"
        )
        if ata_missing:
            labels = " + ".join(ata_missing)
            lines.append(
                f"  • залог ATA ({labels})  {ata_rent_sol:.4f} SOL = ${ata_usd:.2f}   "
                f"вернётся при закрытии счетов"
            )
        lines.append(
            f"  • на комиссии    {fee_sol:.4f} SOL = ${fee_usd:.2f}   "
            f"тратится по копейке"
        )
        # Never print a negative remainder — shortfall branch handles that case.
        if free_after >= -1e-9:
            lines.append(f"  • свободно после ${max(0.0, free_after):.2f}")

    lines.append("")

    for n in notes:
        lines.append(escape_html(n))

    swap_line = format_swap_line(swap_sug if isinstance(swap_sug, dict) else None)
    if swap_line:
        lines.append(swap_line)
    elif not short:
        lines.append("Обмен не нужен — пропорция уже на кошельке.")

    if short:
        lines.append("Дошли и снова /open — confirm не предлагается.")
    else:
        if width_pct is not None:
            confirm_cmd = f"/open {requested_usd:g} {width_pct:g} confirm"
        else:
            confirm_cmd = f"/open {requested_usd:g} confirm"
        lines.append("")
        lines.append(f"Подтвердить: <code>{escape_html(confirm_cmd)}</code>")

    return "\n".join(lines)


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

    require_not_frozen(reply=reply, where="перед свопом при открытии")
    swap_ok = apply_swap_suggestion(suggestion.get("swapSuggestion"), reply)
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
                "останется — сначала /status, убедись что позиции нет, потом /open."
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
    # Compare to post-cap budget, not the raw request (cap is intentional).
    if (
        budget >= _MIN_OPEN_USD
        and total_usd < budget * _OPEN_SUCCESS_MIN_RATIO
    ):
        reply(
            f"❌ Открытие было бы ~${total_usd:.2f} вместо целевых "
            f"${budget:.2f} (мало SOL для пропорции). Не открываю и не "
            f"считаю успехом. Пополни SOL или выведи лишний USDC."
        )
        raise OpenMateriallyUndersized(budget, total_usd)

    # Одно сообщение вместо цепочки: что открываем и почему сумма отличается.
    head = f"🆕 Открываю позицию на ~${total_usd:.2f}"
    if abs(total_usd - requested) > 0.01:
        head += f" (просили ${requested:.2f})"
    body = f"{need_sol:.6f} SOL + ${need_usdc:.4f} USDC"
    detail = notes.render()
    reply(f"{head}\n{body}" + (f"\n{detail}" if detail else ""))
    require_not_frozen(reply=reply, where="перед отправкой открытия")
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
    swap_ok = apply_swap_suggestion(suggestion.get("swapSuggestion"), reply)
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
        allow_multi_tx=bool(bot_config.ALLOW_MULTI_TX_ADD),
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


def position_fees_usd(position: dict, usdc_per_sol: float) -> float:
    fees = position.get("fees") or {}
    return float(fees.get("sol") or 0) * usdc_per_sol + float(fees.get("usdc") or 0)


def fees_are_zero(position: dict) -> bool:
    fees = position.get("fees") or {}
    return float(fees.get("sol") or 0) <= 1e-12 and float(fees.get("usdc") or 0) <= 1e-9


def format_claim_estimate(position: dict, usdc_per_sol: float) -> str:
    """Read-only claim preview. Never signs or sends."""
    fees = position.get("fees") or {}
    fee_sol = float(fees.get("sol") or 0)
    fee_usdc = float(fees.get("usdc") or 0)
    fee_usd = fee_sol * usdc_per_sol + fee_usdc
    pk = escape_html(str(position.get("pubkey") or "?"))
    if fees_are_zero(position):
        return (
            "💸 <b>Комиссии</b>\n"
            f"Позиция <code>{pk}</code>\n\n"
            "Сейчас накопленных комиссий нет — отправлять нечего.\n"
            "Позиция не закрывается этой командой."
        )
    return (
        "💸 <b>Забрать комиссии — проверьте перед подтверждением</b>\n"
        f"Позиция <code>{pk}</code>\n\n"
        f"Накоплено: {fee_sol:.6f} SOL (~${fee_sol * usdc_per_sol:.2f}) + "
        f"{fee_usdc:.4f} USDC\n"
        f"Всего ≈ ${fee_usd:.2f}\n\n"
        "Позиция останется открытой. Деньги — в кошелёк бота "
        "(никуда отдельно не переводятся).\n\n"
        "Подтвердить: <code>/claim confirm</code>"
    )


def format_partial_withdraw_estimate(
    position: dict, pct: int, usdc_per_sol: float
) -> str:
    """Read-only partial withdraw preview from on-hand legs. Never sends."""
    if not (1 <= int(pct) <= 99):
        raise ValueError("pct must be 1..99")
    sol = float(position.get("sol") or 0)
    usdc = float(position.get("usdc") or 0)
    out_sol = sol * pct / 100.0
    out_usdc = usdc * pct / 100.0
    left_sol = sol - out_sol
    left_usdc = usdc - out_usdc
    out_usd = out_sol * usdc_per_sol + out_usdc
    left_usd = left_sol * usdc_per_sol + left_usdc
    total_usd = sol * usdc_per_sol + usdc
    pk = escape_html(str(position.get("pubkey") or "?"))
    return (
        f"📤 <b>Вынуть {pct:d}% — проверьте перед подтверждением</b>\n"
        f"Позиция <code>{pk}</code> · сейчас ≈ ${total_usd:.2f}\n\n"
        f"Выйдет: {out_sol:.6f} SOL (~${out_sol * usdc_per_sol:.2f}) + "
        f"{out_usdc:.4f} USDC ≈ ${out_usd:.2f}\n"
        f"Останется в позиции: {left_sol:.6f} SOL + {left_usdc:.4f} USDC "
        f"≈ ${left_usd:.2f}\n\n"
        "Позиция не закрывается. Рента за аккаунт остаётся запертой, "
        "пока позиция жива. Деньги — в кошелёк бота.\n\n"
        f"Подтвердить: <code>/withdraw {pct:d} confirm</code>"
    )


def claim_fees(
    position: dict,
    *,
    reply: Callable[[str], Any],
) -> dict:
    """Claim swap fees into the bot wallet; leave the position open."""
    if fees_are_zero(position):
        reply("❌ Комиссий нет — транзакцию не отправляю.")
        raise RuntimeError("claim_fees: zero fees")
    pk = position["pubkey"]
    reply("💸 Забираю комиссии…")
    payload = meteora_exec.exec_claim_fees(owner(), pk, **exec_kwargs())
    assert_exec_fully_confirmed(payload)
    reply(format_exec_replies(payload))
    return payload


def withdraw_partial(
    position: dict,
    pct: int,
    *,
    reply: Callable[[str], Any],
) -> dict:
    """Remove ``pct``% liquidity (1..99). Does not close the position account."""
    pct_i = int(pct)
    if not (1 <= pct_i <= 99):
        raise ValueError("withdraw_partial: pct must be 1..99")
    bps = pct_i * 100
    pk = position["pubkey"]
    reply(f"📤 Вынимаю {pct_i}% ликвидности…")
    payload = meteora_exec.exec_withdraw(owner(), pk, bps, **exec_kwargs())
    assert_exec_fully_confirmed(payload)
    reply(format_exec_replies(payload))
    return payload


def close_position_full(
    position: dict,
    *,
    reply: Callable[[str], Any],
    record_cycle: bool = True,
    trigger: str = "manual",
    reopen_after_confirm: dict | None = None,
    price_hint: float | None = None,
) -> None:
    """Full close: removeLiquidity+claim+close, or closePositionIfEmpty if empty.

    Do NOT withdraw separately first on a live position — that leaves an empty
    account; empty accounts must use exec-close-empty, not removeLiquidity.

    If ``reopen_after_confirm`` is set, write reopen_pending immediately after
    confirmation — before Telegram replies or cycle-journal side effects.

    ``price_hint`` skips pool_info (pure read) when the caller already priced
    the position — so a flaky RPC read is not mistaken for an unknown close.
    """
    pk = position["pubkey"]
    if price_hint is not None:
        price = float(price_hint)
    else:
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

    # Flag first — crash window after confirm must not look like "idle".
    if reopen_after_confirm is not None:
        from reopen_pending import ReopenPendingWriteError as _RPWE

        try:
            set_reopen_pending(
                True,
                meta={**reopen_after_confirm, "close_status": "confirmed"},
            )
        except _RPWE as e:
            reply(
                "🚨 <b>CRITICAL: не смог записать reopen_pending после закрытия</b>\n"
                "Позиция уже закрыта, флаг на диске НЕ записан — открытие НЕ начинаю.\n"
                f"Причина: {escape_html(e)}\n"
                "Освободи место на томе / проверь mount, затем /status и осознанный /open."
            )
            raise

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
    """Close → mark reopen_pending → swap/open new range around current activeId.

    reopen_pending is set only after a confirmed close, or when close confirmation
    is unknown (must block). A clean close failure leaves the flag unset.
    """
    import subprocess

    from meteora_ops import MeteoraOpsError

    require_max_position_on_mainnet(reply=reply)
    require_not_frozen(reply=reply, where="перед закрытием при ребалансе")

    pk = position["pubkey"]
    # Pure reads — failures here must NOT set reopen_pending.
    try:
        pool = meteora_ops.pool_info(**ops_kwargs())
        price = float(pool.get("usdcPerSol") or 0)
        close_value = position_value_usd(position, price)
    except (MeteoraOpsError, subprocess.TimeoutExpired, RuntimeError, OSError) as e:
        reply(
            "❌ Не удалось прочитать пул/позицию перед закрытием — "
            "позиция на месте, reopen_pending НЕ ставил. Повторю позже.\n"
            f"Причина: {escape_html(e)}"
        )
        raise

    trigger = "auto" if auto else "manual"
    pending_meta = {
        "closed_position": pk,
        "close_value_usd": close_value,
        "price": price,
        "trigger": trigger,
    }

    try:
        close_position_full(
            position,
            reply=reply,
            record_cycle=True,
            trigger=trigger,
            reopen_after_confirm=pending_meta,
            price_hint=price,
        )
    except (SystemExit, KeyboardInterrupt):
        raise
    except MeteoraOpsError as e:
        # Still in pre-sign prep inside close (pool_info) — position intact.
        reply(
            "❌ Подготовка закрытия не прошла — позиция на месте, "
            "reopen_pending НЕ ставил.\n"
            f"Причина: {escape_html(e)}"
        )
        raise
    except MeteoraExecError as e:
        if _exec_proven_not_sent(e):
            reply(
                "❌ Закрытие не прошло — позиция на месте, reopen_pending НЕ ставил. "
                "Ничего дополнительно делать не нужно, можно повторить позже.\n"
                f"Причина: {escape_html(e)}"
            )
            raise
        sigs = _signatures_from_exec_error(e)
        set_reopen_pending(
            True,
            meta={
                **pending_meta,
                "close_status": "unknown",
                "signatures": sigs,
                "error_type": type(e).__name__,
                "error": str(e)[:400],
                "stage": (e.payload or {}).get("stage"),
            },
        )
        _reply_close_outcome_unknown(reply, signatures=sigs, detail=str(e)[:200])
        raise
    except (subprocess.TimeoutExpired, RuntimeError) as e:
        # run_exec timeout / empty stdout after (or during) signing path.
        set_reopen_pending(
            True,
            meta={
                **pending_meta,
                "close_status": "unknown",
                "signatures": [],
                "error_type": type(e).__name__,
                "error": str(e)[:400],
            },
        )
        _reply_close_outcome_unknown(
            reply,
            signatures=[],
            detail=f"{type(e).__name__}: {e}",
        )
        raise

    # Confirmed close: flag is written inside close_position_full. Ensure it
    # exists even if a test doubles close without honouring reopen_after_confirm.
    from reopen_pending import is_reopen_pending as _pending_now

    if not _pending_now():
        set_reopen_pending(
            True,
            meta={**pending_meta, "close_status": "confirmed"},
        )

    if crash_after_close or os.environ.get("METEORA_CRASH_AFTER_CLOSE") == "1":
        reply("💥 crash_after_close: останавливаюсь с reopen_pending")
        raise SystemExit(42)

    try:
        require_not_frozen(reply=reply, where="после закрытия, перед свопом/открытием")
    except StopRequested:
        reply(
            "🛑 /stop после закрытия: новую позицию не открываю. "
            "Капитал на кошельке, reopen_pending остаётся. "
            "Продолжение: /boevoy, затем /status (убедись что позиции нет) и /open."
        )
        return None

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

    # SOL leg: open_with_budget will swap USDC→SOL when needed. Block only if
    # post-swap SOL still cannot meet needSol (not enough USDC either).
    try:
        sug = suggest_for_budget(budget)
        need_sol = float(sug.get("needSol") or 0)
    except Exception as e:
        reply(
            f"❌ Не удалось оценить needSol для реоткрытия: {escape_html(e)}. "
            "reopen_pending остаётся."
        )
        return None
    if sol_short_after_planned_swap(
        est_sol=usable_sol,
        est_usdc=usdc_ui,
        est_budget=budget,
        need_sol=need_sol,
        price=price,
    ):
        reply(
            f"❌ После закрытия даже со свопом не хватает SOL: "
            f"solAvailableForOpen={usable_sol:.4f}, USDC={usdc_ui:.2f}, "
            f"needSol≈{need_sol:.4f} под бюджет ~${budget:.2f}. Не открываю. "
            f"reopen_pending остаётся — пополни кошелёк, потом /open."
        )
        return None

    reply(f"🔁 Реоткрытие на ~${budget:.2f}…")
    try:
        require_not_frozen(reply=reply, where="перед открытием после закрытия")
        payload = open_with_budget(budget, reply=reply)
    except StopRequested:
        reply(
            "🛑 /stop: открытие не отправлял. Капитал на кошельке, "
            "reopen_pending остаётся. /boevoy затем /status и /open."
        )
        return None
    except SwapOutcomeUnknown:
        # Message already sent; keep pending, do not clear.
        return None
    except OpenMateriallyUndersized as e:
        reply(
            f"❌ Реоткрытие не засчитываю: было бы ${e.actual_usd:.2f} "
            f"вместо ${e.requested_usd:.2f}. reopen_pending остаётся."
        )
        return None
    except SwapFailedOpenAborted as e:
        reply(
            f"❌ Реоткрытие отменено ({e}). reopen_pending остаётся — "
            "выровняй кошелёк; /status затем /open или /rebalance confirm."
        )
        return None
    set_reopen_pending(False)
    reply("✅ Ребаланс завершён — reopen_pending снят.")
    return payload
