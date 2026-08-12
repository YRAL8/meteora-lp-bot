import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import bot_config
import bot_state
import meteora_cycle_journal as cycle_journal
import meteora_ops
import money_ops
import monitor_timer_state
import range_state
import state_paths
import telegram_commands as tg
import telegram_notify
from reopen_pending import is_reopen_pending, load_reopen_pending
from telegram_notify import (
    escape_html,
    format_position_table,
    format_price_trend,
    format_range_bar,
    format_stamp,
    position_in_range,
    send_telegram_message,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

out_of_range_since: Optional[datetime] = None
last_auto_attempt_at: Optional[datetime] = None
rebalance_blocked_alert_sent = False
rebalance_blocked_last_alert_at: Optional[datetime] = None
_last_monitor_tick_at: Optional[datetime] = None
_monitor_watchdog_alerted = False

# Storm guards — process memory only (LITE_2). Reset on bot restart is intentional.
_last_rebalance_at: Optional[datetime] = None
_rebalance_day_utc: str = ""
_rebalance_count_today: int = 0
_daily_limit_notified_day: Optional[str] = None
_uneconomic_warned_at: Optional[datetime] = None


def _utc_day_key(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _ensure_rebalance_day(now: datetime) -> None:
    global _rebalance_day_utc, _rebalance_count_today
    key = _utc_day_key(now)
    if _rebalance_day_utc != key:
        _rebalance_day_utc = key
        _rebalance_count_today = 0


def _minutes_since_last_rebalance(now: datetime) -> Optional[float]:
    if _last_rebalance_at is None:
        return None
    last = _last_rebalance_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() / 60.0


def _record_rebalance(now: datetime) -> None:
    global _last_rebalance_at, _rebalance_count_today
    _ensure_rebalance_day(now)
    _rebalance_count_today += 1
    _last_rebalance_at = now


def _mark_daily_limit_notified(now: datetime) -> None:
    global _daily_limit_notified_day
    _ensure_rebalance_day(now)
    _daily_limit_notified_day = _rebalance_day_utc


def reset_storm_guards_for_tests() -> None:
    """Clear in-memory storm counters (offline tests / scenario setup)."""
    global _last_rebalance_at, _rebalance_day_utc, _rebalance_count_today
    global _daily_limit_notified_day, _uneconomic_warned_at
    _last_rebalance_at = None
    _rebalance_day_utc = ""
    _rebalance_count_today = 0
    _daily_limit_notified_day = None
    _uneconomic_warned_at = None


# Last successfully read position snapshot for heartbeat (never shown as "current"
# without an age mark when a later read fails — LITE_4 / orca audit 27 Jul).
_last_good_position: Optional[dict] = None
_last_good_position_at: Optional[datetime] = None
_last_good_usdc_per_sol: Optional[float] = None
_last_good_active_id: Optional[int] = None
_last_good_bin_step: Optional[int] = None


def reset_heartbeat_snapshot_for_tests() -> None:
    global _last_good_position, _last_good_position_at
    global _last_good_usdc_per_sol, _last_good_active_id, _last_good_bin_step
    _last_good_position = None
    _last_good_position_at = None
    _last_good_usdc_per_sol = None
    _last_good_active_id = None
    _last_good_bin_step = None


def _store_good_position_snapshot(
    pos: dict,
    *,
    active_id: int,
    usdc_per_sol: float,
    bin_step: int,
    now: datetime,
) -> None:
    global _last_good_position, _last_good_position_at
    global _last_good_usdc_per_sol, _last_good_active_id, _last_good_bin_step
    _last_good_position = dict(pos)
    _last_good_position_at = now
    _last_good_usdc_per_sol = float(usdc_per_sol)
    _last_good_active_id = int(active_id)
    _last_good_bin_step = int(bin_step)


def _clear_good_position_snapshot() -> None:
    global _last_good_position, _last_good_position_at
    global _last_good_usdc_per_sol, _last_good_active_id, _last_good_bin_step
    _last_good_position = None
    _last_good_position_at = None
    _last_good_usdc_per_sol = None
    _last_good_active_id = None
    _last_good_bin_step = None


def _auto_stopped_reasons(*, now: datetime, usable_sol: Optional[float]) -> list[str]:
    """Why automation is not acting — heartbeat always reports these (LITE_4)."""
    reasons: list[str] = []
    if bot_state.bot_paused:
        reasons.append("владелец нажал /pauza")
    if bot_state.bot_frozen:
        reasons.append("владелец нажал /stop")
    if not bot_config.AUTO_REBALANCE:
        reasons.append("AUTO_REBALANCE выключен")
    if is_reopen_pending():
        reasons.append("reopen_pending (ребаланс оборван)")
    try:
        import meteora_exec

        unresolved = meteora_exec.list_unresolved_journal()
    except OSError:
        unresolved = []
    if unresolved:
        reasons.append(f"журнал: {len(unresolved)} незакрытых подписей")
    _ensure_rebalance_day(now)
    if _rebalance_count_today >= bot_config.MAX_REBALANCES_PER_DAY:
        reasons.append(
            f"достигнут дневной предел ребалансов "
            f"({_rebalance_count_today}/{bot_config.MAX_REBALANCES_PER_DAY})"
        )
    if usable_sol is not None and usable_sol < bot_config.MIN_SOL_BALANCE:
        reasons.append(
            f"мало SOL для авто (доступно {usable_sol:.4f} < "
            f"{bot_config.MIN_SOL_BALANCE})"
        )
    return reasons


def format_heartbeat(*, now: datetime | None = None) -> str:
    """Build heartbeat text (same formatters as /status). Never silent when auto stopped."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    mode = "DEMO" if bot_config.DRY_RUN else "БОЕВОЙ"
    net = bot_config.effective_network()
    lines = [
        f"💓 <b>Сердцебиение [{mode}]</b> · {escape_html(net)}",
        format_stamp(now),
    ]

    usable_sol: Optional[float] = None
    read_ok = False
    pos: Optional[dict] = None
    active_id = 0
    bin_step = 1
    usdc_per_sol = 0.0
    sol_ui = 0.0
    usdc_ui = 0.0

    try:
        owner = bot_config.wallet_pubkey()
        kw = money_ops.ops_kwargs()
        bal = meteora_ops.balances(owner, **kw)
        pool = meteora_ops.pool_info(**kw)
        pos = money_ops.get_primary_position()
        read_ok = True
        active_id = int(pool["activeId"])
        bin_step = int(pool["binStep"])
        usdc_per_sol = float(pool.get("usdcPerSol") or 0)
        sol_ui = float((bal.get("sol") or {}).get("ui") or 0)
        usdc_ui = float((bal.get("usdc") or {}).get("ui") or 0)
        sao = bal.get("solAvailableForOpen")
        if sao is not None:
            usable_sol = max(0.0, float(sao))
        else:
            usable_sol = max(0.0, sol_ui - bot_config.MIN_SOL_BALANCE)
        if pos is not None:
            _store_good_position_snapshot(
                pos,
                active_id=active_id,
                usdc_per_sol=usdc_per_sol,
                bin_step=bin_step,
                now=now,
            )
        else:
            _clear_good_position_snapshot()
    except Exception as exc:
        log.warning("heartbeat: position/pool read failed: %s", exc, exc_info=True)
        read_ok = False

    if not read_ok:
        lines.append(
            "❌ <b>Позицию прочитать не удалось</b> (это не «всё в порядке»)."
        )
        if (
            _last_good_position is not None
            and _last_good_position_at is not None
            and _last_good_usdc_per_sol is not None
        ):
            age_h = max(
                0.0, (now - _last_good_position_at).total_seconds() / 3600.0
            )
            lines.append(
                f"Последняя <b>удачная</b> позиция "
                f"(≈{age_h:.1f} ч назад — <b>не текущая</b>):"
            )
            lines.append(
                format_position_table(
                    _last_good_position, _last_good_usdc_per_sol
                )
            )
            if (
                _last_good_active_id is not None
                and _last_good_bin_step is not None
            ):
                lines.append(
                    format_range_bar(
                        int(_last_good_position["lowerBinId"]),
                        int(_last_good_position["upperBinId"]),
                        _last_good_active_id,
                        _last_good_usdc_per_sol,
                        _last_good_bin_step,
                    )
                )
        else:
            lines.append("Удачных чтений позиции в этом процессе ещё не было.")
    elif pos is None:
        trend = format_price_trend(usdc_per_sol, bot_config.POLL_INTERVAL_SEC)
        lines.append("⏳ Открытых позиций нет.")
        lines.append(f"📈 Цена SOL: ${usdc_per_sol:,.2f}{trend}")
        lines.append(f"Кошелёк: {sol_ui:.4f} SOL · {usdc_ui:.2f} USDC")
    else:
        in_rng = position_in_range(pos, active_id)
        status = "✅ в диапазоне" if in_rng else "⚠️ ВНЕ диапазона"
        trend = format_price_trend(usdc_per_sol, bot_config.POLL_INTERVAL_SEC)
        lines.append(format_position_table(pos, usdc_per_sol))
        lines.append(f"📈 Цена SOL: ${usdc_per_sol:,.2f}{trend}")
        lines.append(
            format_range_bar(
                int(pos["lowerBinId"]),
                int(pos["upperBinId"]),
                active_id,
                usdc_per_sol,
                bin_step,
            )
        )
        lines.append(f"   Статус: {status}")
        lines.append(f"Кошелёк: {sol_ui:.4f} SOL · {usdc_ui:.2f} USDC")

    reasons = _auto_stopped_reasons(now=now, usable_sol=usable_sol)
    if reasons:
        lines.append("⏸ <b>Автоматика стоит:</b>")
        for r in reasons:
            lines.append(f"• {escape_html(r)}")
    else:
        lines.append("✅ Авто-ребаланс готов действовать при выходе из диапазона.")

    return "\n".join(lines)


async def heartbeat_loop(
    *,
    sleep_fn=asyncio.sleep,
    now_fn=None,
) -> None:
    """Periodic heartbeat. First beat after one full interval (startup msg is separate)."""
    interval_h = float(bot_config.HEARTBEAT_INTERVAL_HOURS)
    if interval_h <= 0:
        log.info("heartbeat disabled (HEARTBEAT_INTERVAL_HOURS=0)")
        return
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    while True:
        await sleep_fn(interval_h * 3600.0)
        try:
            send_telegram_message(format_heartbeat(now=now_fn()))
        except Exception:
            log.exception("heartbeat send failed — monitor continues")


def median_cycle_hours(cycles: list) -> Optional[float]:
    vals = [
        float(c["duration_hours"])
        for c in cycles
        if c.get("duration_hours") is not None and not c.get("incomplete")
    ]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _format_unresolved_journal_alert(
    unresolved: list[dict], *, where: str
) -> str:
    import meteora_exec

    lines = [
        f"⚠️ <b>Неразрешённые записи в журнале отправок</b> ({where})",
        f"Подписей: {len(unresolved)}. Исход этих tx неизвестен — "
        "не начинай новые денежные операции вслепую.",
        "Что делать: /status journal (опрос сети). Если RPC лежит — подожди узел "
        "или проверь explorer и /status journal-forget &lt;sig&gt; confirm.",
        "",
    ]
    for u in unresolved[:8]:
        sig = escape_html(str(u.get("signature") or "?"))
        st = escape_html(str(u.get("status") or "?"))
        act = escape_html(str(u.get("action") or "?"))
        lines.append(f"• <code>{sig}</code> ({st}, {act})")
    return "\n".join(lines)


def _check_unresolved_journal(*, where: str, now: datetime) -> bool:
    """Return True if unresolved entries exist (and optionally alert)."""
    import meteora_exec

    try:
        unresolved = meteora_exec.list_unresolved_journal()
    except OSError:
        log.warning("cannot read exec journal", exc_info=True)
        return False
    if not unresolved:
        return False
    if _should_send_blocked_reminder(now):
        send_telegram_message(
            _format_unresolved_journal_alert(unresolved, where=where)
        )
        _mark_blocked_reminder_sent(now)
    log.warning(
        "unresolved journal entries=%s where=%s", len(unresolved), where
    )
    return True


def _persist_timers() -> None:
    monitor_timer_state.save_timers(out_of_range_since, last_auto_attempt_at)


def _rpc_host_for_log(rpc_url: str) -> str:
    try:
        return urlparse(rpc_url).hostname or "(rpc)"
    except Exception:
        return "(rpc)"


def _assert_mainnet_rpc_explicit() -> None:
    """Refuse silent public-mainnet RPC (produces unknown outcomes)."""
    if bot_config.effective_network() != "mainnet":
        return
    raw = os.environ.get("SOLANA_RPC_URL", "").strip()
    if not raw:
        raise SystemExit(
            "mainnet requires explicit SOLANA_RPC_URL in .env "
            "(public api.mainnet-beta.solana.com is not allowed)"
        )
    host = (_rpc_host_for_log(raw) or "").lower()
    if host in ("api.mainnet-beta.solana.com", "solana-api.projectserum.com"):
        raise SystemExit(
            f"mainnet SOLANA_RPC_URL points at public host {host} — "
            "set a private/provider URL"
        )


def _reset_rebalance_blocked_state() -> None:
    global rebalance_blocked_alert_sent, rebalance_blocked_last_alert_at
    rebalance_blocked_alert_sent = False
    rebalance_blocked_last_alert_at = None


def _should_send_blocked_reminder(now: datetime) -> bool:
    """First reminder immediately, then every REBALANCE_BLOCKED_REMINDER_HOURS.

    Same discipline as Orca after the 2026-07-28 silent-night incident: a
    one-shot alert left the owner without follow-ups for hours.
    """
    if not rebalance_blocked_alert_sent:
        return True
    if rebalance_blocked_last_alert_at is None:
        return True
    hours = (now - rebalance_blocked_last_alert_at).total_seconds() / 3600.0
    return hours >= bot_config.REBALANCE_BLOCKED_REMINDER_HOURS


def _mark_blocked_reminder_sent(now: datetime) -> None:
    global rebalance_blocked_alert_sent, rebalance_blocked_last_alert_at
    rebalance_blocked_alert_sent = True
    rebalance_blocked_last_alert_at = now


def _price_bounds(
    pos: dict, active_id: int, usdc_per_sol: float, bin_step: int
) -> tuple[float, float]:
    lo = int(pos["lowerBinId"])
    hi = int(pos["upperBinId"])
    return money_ops.bin_prices(active_id, usdc_per_sol, bin_step, lo, hi)


def _maybe_warn_uneconomic(pos: dict, usdc_per_sol: float) -> None:
    """Warn when median cycle life < payback; never blocks the rebalance."""
    global _uneconomic_warned_at
    lookback = bot_config.UNECONOMIC_LOOKBACK_CYCLES
    cycles = cycle_journal.get_default_journal().read_recent_cycles(limit=lookback)
    median = median_cycle_hours(cycles)
    if median is None:
        return
    pos_usd = money_ops.position_value_usd(pos, usdc_per_sol)
    payback = bot_config.effective_payback_hours(pos_usd)
    if median >= payback:
        return
    now = datetime.now(timezone.utc)
    if _uneconomic_warned_at is not None:
        hours = (now - _uneconomic_warned_at).total_seconds() / 3600.0
        if hours < bot_config.REBALANCE_BLOCKED_REMINDER_HOURS:
            return
    pct = range_state.current_range_pct()
    msg = (
        f"⚠️ <b>Диагностика окупаемости</b> (не блокирует ребаланс)\n"
        f"Ширина ±{pct}% на этом пуле не окупает ребалансы: "
        f"медианная жизнь последних {len(cycles)} циклов "
        f"<b>{median:.2f} ч</b> против окупаемости <b>{payback:.1f} ч</b> "
        f"(для позиции ≈${pos_usd:.2f}).\n"
        f"Стоит расширить диапазон (/setrange) или сменить пул на больший binStep.\n"
        f"Позиция сейчас ≈ ${pos_usd:.2f}."
    )
    send_telegram_message(msg)
    log.warning(
        "uneconomic width: median_life=%.2fh payback=%.1fh pos=$%.2f (warn only)",
        median,
        payback,
        pos_usd,
    )
    _uneconomic_warned_at = now


def _tg_reply_collector(buf: list[str]):
    def reply(text: str) -> None:
        buf.append(text)
        send_telegram_message(text)
        log.info("rebalance: %s", text.replace("\n", " ")[:240])

    return reply


async def monitor_position(*, now: datetime | None = None) -> None:
    """One monitoring tick. `now` is injectable for offline tests."""
    global out_of_range_since, last_auto_attempt_at, _last_monitor_tick_at

    _last_monitor_tick_at = datetime.now(timezone.utc)

    if bot_state.bot_paused or bot_state.bot_frozen:
        log.info("Бот на паузе/заморожен — пропускаю тик мониторинга")
        return

    if bot_state.money_lock.locked():
        log.info("Ребаланс/деньги в процессе — пропускаю тик мониторинга")
        return

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    try:
        if _check_unresolved_journal(where="monitor", now=now):
            return

        if is_reopen_pending():
            meta = load_reopen_pending() or {}
            if _should_send_blocked_reminder(now):
                send_telegram_message(
                    "⚠️ <b>reopen_pending</b> — авто-ребаланс не стартует.\n"
                    "Капитал возможно на кошельке после оборванного close→open.\n"
                    f"<code>{escape_html(str(meta)[:400])}</code>\n"
                    "Сначала /status (есть ли позиция). "
                    "Если позиции нет — /open; иначе не открывай вторую. "
                    "Журнал: /status journal."
                )
                _mark_blocked_reminder_sent(now)
            log.warning("reopen_pending — пропускаю авто-ребаланс")
            return

        owner = bot_config.wallet_pubkey()
        kw = money_ops.ops_kwargs()
        pool = meteora_ops.pool_info(**kw)
        active_id = int(pool["activeId"])
        bin_step = int(pool["binStep"])
        usdc_per_sol = float(pool.get("usdcPerSol") or 0)

        telegram_notify.price_history.append(usdc_per_sol)
        cycle_journal.safe_call(
            cycle_journal.get_default_journal().on_monitor_tick, usdc_per_sol
        )

        pos = money_ops.get_primary_position()
        if pos is None:
            _clear_good_position_snapshot()
            log.info("Мониторинг: позиций нет (activeId=%s)", active_id)
            return

        _store_good_position_snapshot(
            pos,
            active_id=active_id,
            usdc_per_sol=usdc_per_sol,
            bin_step=bin_step,
            now=now,
        )
        in_rng = position_in_range(pos, active_id)
        lo_p, hi_p = _price_bounds(pos, active_id, usdc_per_sol, bin_step)
        trend = format_price_trend(usdc_per_sol, bot_config.POLL_INTERVAL_SEC)
        price_line = f"📈 Цена SOL: ${usdc_per_sol:.4f}{trend}"
        range_line = format_range_bar(
            int(pos["lowerBinId"]),
            int(pos["upperBinId"]),
            active_id,
            usdc_per_sol,
            bin_step,
        )

        log.info(
            "Позиция %s: $%.2f bins=[%s,%s] activeId=%s %s",
            pos.get("pubkey"),
            money_ops.position_value_usd(pos, usdc_per_sol),
            pos.get("lowerBinId"),
            pos.get("upperBinId"),
            active_id,
            "в диапазоне" if in_rng else "ВНЕ диапазона",
        )

        # --- in range ---
        if in_rng:
            if out_of_range_since is not None:
                duration = max(0.0, (now - out_of_range_since).total_seconds())
                log.info("Цена вернулась в диапазон (%.0f сек вне)", duration)
                send_telegram_message(
                    f"✅ <b>Вернулись в диапазон</b>\n"
                    f"{format_position_table(pos, usdc_per_sol)}\n"
                    f"{price_line}\n"
                    f"{range_line}"
                )
            out_of_range_since = None
            last_auto_attempt_at = None
            _persist_timers()
            _reset_rebalance_blocked_state()
            return

        # --- out of range ---
        if out_of_range_since is None:
            out_of_range_since = now
            _persist_timers()
            _reset_rebalance_blocked_state()
            since_str = out_of_range_since.replace(microsecond=0).isoformat()
            log.warning(
                "Цена вышла за границу — жду %s мин (AUTO_REBALANCE=%s)",
                bot_config.REBALANCE_DELAY_MIN,
                bot_config.AUTO_REBALANCE,
            )
            auto_hint = (
                f"⏳ Жду {bot_config.REBALANCE_DELAY_MIN} мин перед авто-ребалансом…"
                if bot_config.AUTO_REBALANCE
                else "AUTO_REBALANCE=off — нужен ручной /rebalance."
            )
            send_telegram_message(
                f"⚠️ <b>Цена вне диапазона позиции</b>\n"
                f"{format_position_table(pos, usdc_per_sol)}\n"
                f"{price_line}\n"
                f"{range_line}\n"
                f"Границы ≈ ${lo_p:.4f}–${hi_p:.4f}\n"
                f"С {since_str}. {auto_hint}"
            )
            return

        minutes_out = (now - out_of_range_since).total_seconds() / 60.0
        if minutes_out < bot_config.REBALANCE_DELAY_MIN:
            log.info(
                "Вне диапазона %.1f мин из %s — выдержка",
                minutes_out,
                bot_config.REBALANCE_DELAY_MIN,
            )
            return

        # Delay elapsed.
        if not bot_config.AUTO_REBALANCE:
            if _should_send_blocked_reminder(now):
                send_telegram_message(
                    f"✋ <b>Нужен ручной ребаланс</b> (AUTO_REBALANCE=false)\n"
                    f"Цена вне диапазона уже {minutes_out:.0f} мин "
                    f"(выдержка {bot_config.REBALANCE_DELAY_MIN} мин прошла).\n"
                    f"{price_line}\n"
                    f"{range_line}\n"
                    f"Вызови /rebalance когда будешь готов."
                )
                log.warning(
                    "Вне диапазона >%s мин — ждём ручной /rebalance",
                    bot_config.REBALANCE_DELAY_MIN,
                )
                _mark_blocked_reminder_sent(now)
            return

        # Mainnet auto requires an explicit position ceiling (audit B3B).
        if (
            bot_config.effective_network() == "mainnet"
            and bot_config.MAX_POSITION_USD is None
        ):
            if _should_send_blocked_reminder(now):
                send_telegram_message(
                    "🛑 <b>AUTO_REBALANCE на mainnet без MAX_POSITION_USD</b>\n"
                    "Автоматика отключена, пока не задашь потолок в .env "
                    "(иначе ребаланс втянет почти весь кошелёк).\n"
                    "Ручной /rebalance на mainnet без потолка тоже запрещён."
                )
                _mark_blocked_reminder_sent(now)
            log.error("AUTO_REBALANCE+mainnet without MAX_POSITION_USD — skip")
            return

        # --- AUTO path: re-check price after delay ---
        pool2 = meteora_ops.pool_info(**kw)
        active2 = int(pool2["activeId"])
        price2 = float(pool2.get("usdcPerSol") or 0)
        still_out = not position_in_range(pos, active2)
        if not still_out:
            lo2, hi2 = _price_bounds(pos, active2, price2, int(pool2["binStep"]))
            log.info("Цена вернулась после выдержки — ребаланс отменён")
            out_of_range_since = None
            last_auto_attempt_at = None
            _persist_timers()
            _reset_rebalance_blocked_state()
            send_telegram_message(
                f"✅ <b>Цена вернулась — авто-ребаланс отменён</b>\n"
                f"📈 Цена SOL: ${price2:.4f}\n"
                f"Границы ≈ ${lo2:.4f}–${hi2:.4f}"
            )
            return

        # Storm guard: min interval (in-memory)
        _ensure_rebalance_day(now)
        since_last = _minutes_since_last_rebalance(now)
        if (
            since_last is not None
            and since_last < bot_config.MIN_REBALANCE_INTERVAL_MIN
        ):
            log.info(
                "MIN_REBALANCE_INTERVAL: %.1f < %s мин — жду",
                since_last,
                bot_config.MIN_REBALANCE_INTERVAL_MIN,
            )
            return

        # Storm guard: daily cap (in-memory; one Telegram notice per UTC day)
        if _rebalance_count_today >= bot_config.MAX_REBALANCES_PER_DAY:
            if _daily_limit_notified_day != _rebalance_day_utc:
                send_telegram_message(
                    f"🛑 <b>Лимит авто-ребалансов на сутки исчерпан</b>\n"
                    f"Сегодня уже {_rebalance_count_today} "
                    f"(MAX_REBALANCES_PER_DAY={bot_config.MAX_REBALANCES_PER_DAY}).\n"
                    f"Автоматика приостановлена до конца суток UTC.\n"
                    f"Варианты: расширить диапазон (/setrange), сменить пул, "
                    f"или осознанно поднять MAX_REBALANCES_PER_DAY.\n"
                    f"Ручной /rebalance по-прежнему доступен."
                )
                _mark_daily_limit_notified(now)
                log.warning("MAX_REBALANCES_PER_DAY reached — auto paused for today")
            return

        # Low SOL: compare post-swap availability to needSol. Rebalance always
        # runs the planned USDC↔SOL swap; blocking on pre-swap SOL alone freezes
        # every upward exit (position all USDC).
        bal = meteora_ops.balances(owner, **kw)
        sol_ui = float((bal.get("sol") or {}).get("ui") or 0)
        usdc_ui = float((bal.get("usdc") or {}).get("ui") or 0)
        sao = bal.get("solAvailableForOpen")
        if sao is not None:
            usable_sol = max(0.0, float(sao))
        else:
            usable_sol = max(0.0, sol_ui - bot_config.MIN_SOL_BALANCE)
        pos_sol = float(pos.get("sol") or 0)
        pos_usdc = float(pos.get("usdc") or 0)
        est_sol = usable_sol + pos_sol
        est_usdc = usdc_ui + pos_usdc
        est_budget = (est_sol * price2 + est_usdc) * 0.98
        if bot_config.MAX_POSITION_USD is not None:
            est_budget = min(est_budget, float(bot_config.MAX_POSITION_USD))
        need_sol: float | None = None
        if est_budget >= 0.05:
            try:
                sug = money_ops.suggest_for_budget(est_budget)
                need_sol = float(sug.get("needSol") or 0)
            except Exception:
                log.warning(
                    "suggest-amounts for SOL gate failed — fallback usable_sol>0",
                    exc_info=True,
                )
        short_sol = money_ops.sol_short_after_planned_swap(
            est_sol=est_sol,
            est_usdc=est_usdc,
            est_budget=est_budget,
            need_sol=need_sol,
            price=price2,
        )
        if short_sol:
            if _should_send_blocked_reminder(now):
                need_s = f"{need_sol:.4f}" if need_sol is not None else "?"
                send_telegram_message(
                    f"⚠️ <b>Авто-ребаланс отложен — мало бюджета/SOL даже после свопа</b>\n"
                    f"оценка после close: SOL≈{est_sol:.4f}, USDC≈{est_usdc:.2f}, "
                    f"needSol≈{need_s}, бюджет≈${est_budget:.2f} "
                    f"(сейчас solAvailableForOpen={usable_sol:.4f}).\n"
                    f"Вне диапазона уже {minutes_out:.0f} мин. "
                    f"Таймер не сброшен — повторю на следующем тике."
                )
                _mark_blocked_reminder_sent(now)
            log.warning(
                "Мало SOL даже после свопа est_sol=%.4f est_usdc=%.2f need=%s budget=%.2f — отложен",
                est_sol,
                est_usdc,
                need_sol,
                est_budget,
            )
            return

        # After a failed attempt, wait at least REBALANCE_DELAY_MIN before retry
        # without clearing out_of_range_since (C9).
        if last_auto_attempt_at is not None:
            since_attempt = (now - last_auto_attempt_at).total_seconds() / 60.0
            if since_attempt < bot_config.REBALANCE_DELAY_MIN:
                log.info(
                    "Пауза после неуспешной попытки: %.1f < %s мин",
                    since_attempt,
                    bot_config.REBALANCE_DELAY_MIN,
                )
                if _should_send_blocked_reminder(now):
                    send_telegram_message(
                        f"⏳ Авто-ребаланс ждёт паузу после неудачи "
                        f"({since_attempt:.0f}/{bot_config.REBALANCE_DELAY_MIN} мин). "
                        f"Таймер вне диапазона сохранён."
                    )
                    _mark_blocked_reminder_sent(now)
                return

        # Uneconomic diagnosis — warn only
        _maybe_warn_uneconomic(pos, price2)

        # Act — do NOT clear out_of_range_since until success
        last_auto_attempt_at = now
        _persist_timers()
        lo_now, hi_now = _price_bounds(pos, active2, price2, int(pool2["binStep"]))
        start_msg = (
            f"🤖 <b>Авто-ребаланс</b>\n"
            f"Почему: цена вне диапазона {minutes_out:.0f} мин "
            f"(выдержка {bot_config.REBALANCE_DELAY_MIN} мин).\n"
            f"Сейчас ${price2:.4f}; границы были ≈ ${lo_now:.4f}–${hi_now:.4f} "
            f"(bins [{pos['lowerBinId']},{pos['upperBinId']}], "
            f"activeId={active2}).\n"
            f"Закрываю → своп при необходимости → открываю вокруг текущей цены."
        )
        send_telegram_message(start_msg)
        log.info("Начинаю авто-ребаланс (%.0f мин вне)", minutes_out)

        replies: list[str] = []
        collector = _tg_reply_collector(replies)
        async with bot_state.money_lock:
            # Fresh read inside lock (Orca M1 lesson).
            if bot_state.bot_frozen:
                log.warning("/stop под локом — abort auto до закрытия")
                send_telegram_message(
                    "🛑 Авто-ребаланс не начинал закрытие — бот заморожен (/stop)."
                )
                return
            if is_reopen_pending():
                log.warning("reopen_pending появился под локом — abort auto")
                return
            fresh = money_ops.get_primary_position()
            if fresh is None:
                log.warning("Позиция исчезла под локом — abort auto")
                send_telegram_message(
                    "⚠️ Позиция исчезла перед авто-ребалансом — пропускаю."
                )
                return
            try:
                payload = await asyncio.to_thread(
                    money_ops.rebalance_position,
                    fresh,
                    reply=collector,
                    auto=True,
                )
                from reopen_pending import is_reopen_pending as _pending_now

                if bot_state.bot_frozen:
                    send_telegram_message(
                        "🛑 Авто-ребаланс прерван заморозкой (/stop) — "
                        "успехом не считаю, суточный лимит не засчитан."
                    )
                    log.warning("Авто-ребаланс finished under freeze — not success")
                    return
                if payload is None or _pending_now():
                    meta = load_reopen_pending() or {}
                    send_telegram_message(
                        "🚨 <b>Авто-ребаланс НЕ завершён</b>\n"
                        "Позиция закрыта или капитал на кошельке; "
                        "<code>reopen_pending</code> остаётся.\n"
                        f"meta={escape_html(str(meta)[:400])}\n"
                        "Суточный лимит НЕ засчитан. "
                        "/status (есть ли позиция); /open только если её нет. "
                        "Журнал: /status journal."
                    )
                    log.warning(
                        "Авто-ребаланс aborted (payload=%s pending=%s) — не success",
                        payload is not None,
                        _pending_now(),
                    )
                    return
                out_of_range_since = None
                last_auto_attempt_at = None
                _persist_timers()
                _record_rebalance(now)
                _reset_rebalance_blocked_state()
                send_telegram_message("✅ Авто-ребаланс завершён.")
            except SystemExit:
                raise
            except Exception as e:
                log.exception("Авто-ребаланс упал: %s", e)
                from telegram_notify import format_html_error

                send_telegram_message(
                    format_html_error("❌ Авто-ребаланс ошибка: ", e)
                )

    except Exception:
        log.exception("Ошибка мониторинга (тик пропущен)")


async def main() -> None:
    global out_of_range_since, last_auto_attempt_at

    _assert_mainnet_rpc_explicit()
    state_paths.state_dir().mkdir(parents=True, exist_ok=True)
    out_of_range_since, last_auto_attempt_at = monitor_timer_state.load_timers()

    rpc_url = bot_config.effective_rpc()
    rpc_host = _rpc_host_for_log(rpc_url)

    log.info("=" * 50)
    log.info(
        "Meteora LP-бот | DRY_RUN=%s network=%s AUTO_REBALANCE=%s "
        "DELAY=%smin INTERVAL=%smin MAX/day=%s pool=%s rpc=%s",
        bot_config.DRY_RUN,
        bot_config.effective_network(),
        bot_config.AUTO_REBALANCE,
        bot_config.REBALANCE_DELAY_MIN,
        bot_config.MIN_REBALANCE_INTERVAL_MIN,
        bot_config.MAX_REBALANCES_PER_DAY,
        bot_config.pool_pubkey(),
        rpc_host,
    )
    if out_of_range_since or last_auto_attempt_at:
        log.info(
            "restored timers out_of_range_since=%s last_auto_attempt_at=%s",
            out_of_range_since,
            last_auto_attempt_at,
        )
    log.info("=" * 50)

    if is_reopen_pending():
        meta = load_reopen_pending() or {}
        log.error("REOPEN_PENDING при старте: %s", meta)
        send_telegram_message(
            "⚠️ <b>ВНИМАНИЕ: reopen_pending</b>\n"
            "Прошлый ребаланс оборвался между закрытием и открытием.\n"
            f"<code>{escape_html(str(meta)[:400])}</code>\n"
            "Сначала /status (есть ли позиция). "
            "Если позиции нет — /open; иначе не открывай вторую. "
            "Журнал: /status journal."
        )

    try:
        unresolved = __import__("meteora_exec").list_unresolved_journal()
    except OSError:
        unresolved = []
    if unresolved:
        send_telegram_message(
            _format_unresolved_journal_alert(unresolved, where="startup")
        )
        log.error("unresolved journal at startup: %s", len(unresolved))

    if (
        bot_config.AUTO_REBALANCE
        and bot_config.effective_network() == "mainnet"
        and bot_config.MAX_POSITION_USD is None
    ):
        log.error("AUTO_REBALANCE on mainnet without MAX_POSITION_USD")
        send_telegram_message(
            "🛑 <b>AUTO_REBALANCE на mainnet без MAX_POSITION_USD</b>\n"
            "Автоматика не будет тратить деньги, пока не задашь потолок. "
            "Ручной /rebalance на mainnet без потолка тоже запрещён; "
            "/open с явной суммой — можно."
        )

    async def _monitor_loop() -> None:
        while True:
            await monitor_position()
            await asyncio.sleep(bot_config.POLL_INTERVAL_SEC)

    async def _monitor_watchdog() -> None:
        global _monitor_watchdog_alerted
        # Alert if no tick for ~3 poll intervals + 90s slack.
        limit_sec = max(90.0, bot_config.POLL_INTERVAL_SEC * 3 + 90.0)
        while True:
            await asyncio.sleep(30)
            if _last_monitor_tick_at is None:
                continue
            age = (
                datetime.now(timezone.utc) - _last_monitor_tick_at
            ).total_seconds()
            if age > limit_sec:
                if not _monitor_watchdog_alerted:
                    send_telegram_message(
                        "🚨 <b>Монитор не отвечает</b>\n"
                        f"Последний тик был {age / 60.0:.1f} мин назад "
                        f"(порог {limit_sec / 60.0:.1f} мин).\n"
                        "Команды Telegram могут работать, но слежения за позицией нет. "
                        "Перезапусти процесс бота."
                    )
                    _monitor_watchdog_alerted = True
                    log.error("monitor watchdog: last tick %.0fs ago", age)
            else:
                _monitor_watchdog_alerted = False

    app = None
    can_start_telegram = False
    if not bot_config.is_placeholder(bot_config.TELEGRAM_BOT_TOKEN):
        app = tg.build_telegram_app()
        try:
            me = await app.bot.get_me()
            log.info("Telegram bot: @%s (id=%s)", me.username, me.id)
            can_start_telegram = True
        except Exception:
            log.exception("Telegram токен невалиден — polling отключён")

    mode = "DEMO/devnet" if bot_config.DRY_RUN else "БОЕВОЙ"
    auto_line = (
        "авто-ребаланс ВКЛ"
        if bot_config.AUTO_REBALANCE
        else "только наблюдение (AUTO_REBALANCE=off)"
    )
    send_telegram_message(
        f"🤖 <b>Meteora LP-бот запущен</b>\n"
        f"Режим: {mode} · сеть: {bot_config.effective_network()}\n"
        f"RPC: <code>{escape_html(rpc_host)}</code>\n"
        f"{auto_line} · выдержка {bot_config.REBALANCE_DELAY_MIN} мин\n"
        f"Пул: <code>{bot_config.pool_pubkey()}</code>\n"
        f"Опрос: {bot_config.POLL_INTERVAL_SEC} сек\n"
        f"Кнопки: /start"
    )

    if can_start_telegram and app is not None:
        async with app:
            await app.start()
            try:
                await tg.register_menu_commands(app)
                log.info(
                    "register_menu_commands OK: %s",
                    [c.command for c in tg._MENU_COMMANDS],
                )
            except Exception:
                log.exception("register_menu_commands failed")
            await app.updater.start_polling()
            log.info(
                "Telegram polling: /status /pnl /setrange /rebalance "
                "/addliquidity /open /pauza /stop /boevoy /withdraw"
            )

            monitor_task = asyncio.create_task(_monitor_loop(), name="monitor")
            watchdog_task = asyncio.create_task(
                _monitor_watchdog(), name="monitor-watchdog"
            )
            heartbeat_task = asyncio.create_task(
                heartbeat_loop(), name="heartbeat"
            )

            def _on_monitor_done(task: asyncio.Task) -> None:
                if task.cancelled():
                    return
                exc = task.exception()
                if exc is not None:
                    log.error(
                        "monitor task died: %s",
                        exc,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                    send_telegram_message(
                        "🚨 <b>Задача монитора упала</b>\n"
                        f"{escape_html(type(exc).__name__)}: {escape_html(exc)}\n"
                        "Слежение остановлено — перезапусти бота."
                    )

            monitor_task.add_done_callback(_on_monitor_done)
            try:
                while True:
                    await asyncio.sleep(1)
            except (KeyboardInterrupt, SystemExit):
                log.info("Остановка по сигналу")
            finally:
                monitor_task.cancel()
                watchdog_task.cancel()
                heartbeat_task.cancel()
                for t in (monitor_task, watchdog_task, heartbeat_task):
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                await app.updater.stop()
                await app.stop()
        return

    log.warning("Telegram polling отключён — только мониторинг + heartbeat")
    try:
        await asyncio.gather(_monitor_loop(), heartbeat_loop())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановка по сигналу")


if __name__ == "__main__":
    asyncio.run(main())
