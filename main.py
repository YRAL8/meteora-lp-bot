import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import auto_rebalance_limits as ar_limits
import bot_config
import bot_state
import meteora_cycle_journal as cycle_journal
import meteora_ops
import money_ops
import range_state
import telegram_commands as tg
import telegram_notify
from reopen_pending import is_reopen_pending, load_reopen_pending
from telegram_notify import (
    escape_html,
    format_position_table,
    format_price_trend,
    format_range_bar,
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
    lookback = bot_config.UNECONOMIC_LOOKBACK_CYCLES
    cycles = cycle_journal.get_default_journal().read_recent_cycles(limit=lookback)
    median = ar_limits.median_cycle_hours(cycles)
    if median is None:
        return
    payback = bot_config.REBALANCE_PAYBACK_HOURS
    if median >= payback:
        return
    st = ar_limits.load_state(now=datetime.now(timezone.utc))
    if not ar_limits.should_warn_uneconomic(
        st, reminder_hours=bot_config.REBALANCE_BLOCKED_REMINDER_HOURS
    ):
        return
    pct = range_state.current_range_pct()
    msg = (
        f"⚠️ <b>Диагностика окупаемости</b> (не блокирует ребаланс)\n"
        f"Ширина ±{pct}% на этом пуле не окупает ребалансы: "
        f"медианная жизнь последних {len(cycles)} циклов "
        f"<b>{median:.2f} ч</b> против окупаемости <b>{payback:.1f} ч</b>.\n"
        f"Стоит расширить диапазон (/setrange) или сменить пул на больший binStep.\n"
        f"Позиция сейчас ≈ ${money_ops.position_value_usd(pos, usdc_per_sol):.2f}."
    )
    send_telegram_message(msg)
    log.warning(
        "uneconomic width: median_life=%.2fh payback=%.1fh (warn only)",
        median,
        payback,
    )
    ar_limits.mark_uneconomic_warned(st)


def _tg_reply_collector(buf: list[str]):
    def reply(text: str) -> None:
        buf.append(text)
        send_telegram_message(text)
        log.info("rebalance: %s", text.replace("\n", " ")[:240])

    return reply


async def monitor_position(*, now: datetime | None = None) -> None:
    """One monitoring tick. `now` is injectable for offline tests."""
    global out_of_range_since, last_auto_attempt_at

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
        if is_reopen_pending():
            meta = load_reopen_pending() or {}
            if _should_send_blocked_reminder(now):
                send_telegram_message(
                    "⚠️ <b>reopen_pending</b> — авто-ребаланс не стартует.\n"
                    "Капитал на кошельке после оборванного close→open.\n"
                    f"<code>{escape_html(str(meta)[:400])}</code>\n"
                    "Дожми /open вручную или /status journal."
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
            log.info("Мониторинг: позиций нет (activeId=%s)", active_id)
            return

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
            _reset_rebalance_blocked_state()
            return

        # --- out of range ---
        if out_of_range_since is None:
            out_of_range_since = now
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
            _reset_rebalance_blocked_state()
            send_telegram_message(
                f"✅ <b>Цена вернулась — авто-ребаланс отменён</b>\n"
                f"📈 Цена SOL: ${price2:.4f}\n"
                f"Границы ≈ ${lo2:.4f}–${hi2:.4f}"
            )
            return

        # Storm guard: min interval
        lim = ar_limits.load_state(now=now)
        lim.ensure_day(now)
        since_last = ar_limits.minutes_since_last(lim, now)
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

        # Storm guard: daily cap
        if lim.count_today >= bot_config.MAX_REBALANCES_PER_DAY:
            if lim.daily_limit_notified_day != lim.day_utc:
                send_telegram_message(
                    f"🛑 <b>Лимит авто-ребалансов на сутки исчерпан</b>\n"
                    f"Сегодня уже {lim.count_today} "
                    f"(MAX_REBALANCES_PER_DAY={bot_config.MAX_REBALANCES_PER_DAY}).\n"
                    f"Автоматика приостановлена до конца суток UTC.\n"
                    f"Варианты: расширить диапазон (/setrange), сменить пул, "
                    f"или осознанно поднять MAX_REBALANCES_PER_DAY.\n"
                    f"Ручной /rebalance по-прежнему доступен."
                )
                ar_limits.mark_daily_limit_notified(lim, now)
                log.warning("MAX_REBALANCES_PER_DAY reached — auto paused for today")
            return

        # Low SOL: same number open uses (rent + fee reserve already subtracted)
        bal = meteora_ops.balances(owner, **kw)
        sol_ui = float((bal.get("sol") or {}).get("ui") or 0)
        sao = bal.get("solAvailableForOpen")
        if sao is not None:
            usable_sol = max(0.0, float(sao))
        else:
            usable_sol = max(0.0, sol_ui - bot_config.MIN_SOL_BALANCE)
        if usable_sol <= 0:
            if _should_send_blocked_reminder(now):
                send_telegram_message(
                    f"⚠️ <b>Авто-ребаланс отложен — мало SOL для открытия</b>\n"
                    f"solAvailableForOpen={usable_sol:.4f} "
                    f"(баланс {sol_ui:.4f}, MIN_SOL_BALANCE="
                    f"{bot_config.MIN_SOL_BALANCE}).\n"
                    f"Вне диапазона уже {minutes_out:.0f} мин. "
                    f"Таймер не сброшен — повторю на следующем тике."
                )
                _mark_blocked_reminder_sent(now)
            log.warning(
                "Мало SOL для открытия usable=%.4f sol=%.4f — ребаланс отложен",
                usable_sol,
                sol_ui,
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
                        "Дожми /open вручную или /status journal."
                    )
                    log.warning(
                        "Авто-ребаланс aborted (payload=%s pending=%s) — не success",
                        payload is not None,
                        _pending_now(),
                    )
                    return
                out_of_range_since = None
                last_auto_attempt_at = None
                ar_limits.record_rebalance(lim, now)
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
    log.info("=" * 50)
    log.info(
        "Meteora LP-бот | DRY_RUN=%s network=%s AUTO_REBALANCE=%s "
        "DELAY=%smin INTERVAL=%smin MAX/day=%s pool=%s",
        bot_config.DRY_RUN,
        bot_config.effective_network(),
        bot_config.AUTO_REBALANCE,
        bot_config.REBALANCE_DELAY_MIN,
        bot_config.MIN_REBALANCE_INTERVAL_MIN,
        bot_config.MAX_REBALANCES_PER_DAY,
        bot_config.pool_pubkey(),
    )
    log.info("=" * 50)

    if is_reopen_pending():
        meta = load_reopen_pending() or {}
        log.error("REOPEN_PENDING при старте: %s", meta)
        send_telegram_message(
            "⚠️ <b>ВНИМАНИЕ: reopen_pending</b>\n"
            "Прошлый ребаланс оборвался между закрытием и открытием.\n"
            f"<code>{escape_html(str(meta)[:400])}</code>\n"
            "Капитал на кошельке. Дожми /open или /status journal — "
            "бот НЕ будет молча продолжать как ни в чём не бывало."
        )

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

            monitor_task = asyncio.create_task(_monitor_loop())
            try:
                while True:
                    await asyncio.sleep(1)
            except (KeyboardInterrupt, SystemExit):
                log.info("Остановка по сигналу")
            finally:
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass
                await app.updater.stop()
                await app.stop()
        return

    log.warning("Telegram polling отключён — только мониторинг")
    try:
        await _monitor_loop()
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановка по сигналу")


if __name__ == "__main__":
    asyncio.run(main())
