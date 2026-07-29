import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import bot_config
import bot_state
import meteora_cycle_journal as cycle_journal
import meteora_ops
import telegram_commands as tg
import telegram_notify
from reopen_pending import is_reopen_pending, load_reopen_pending
from telegram_notify import (
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


async def monitor_position() -> None:
    global out_of_range_since

    if bot_state.bot_paused or bot_state.bot_frozen:
        log.info("Бот на паузе/заморожен — пропускаю тик мониторинга")
        return

    try:
        owner = bot_config.wallet_pubkey()
        kw = {
            "pool": bot_config.pool_pubkey(),
            "rpc": bot_config.effective_rpc(),
        }
        pool = meteora_ops.pool_info(**kw)
        positions_payload = meteora_ops.list_positions(owner, **kw)
        positions = positions_payload.get("positions") or []
        active_id = int(pool["activeId"])
        bin_step = int(pool["binStep"])
        usdc_per_sol = float(pool["usdcPerSol"])

        telegram_notify.price_history.append(usdc_per_sol)
        cycle_journal.safe_call(
            cycle_journal.get_default_journal().on_monitor_tick, usdc_per_sol
        )

        if not positions:
            log.info("Мониторинг: позиций нет (activeId=%s)", active_id)
            return

        for pos in positions:
            in_rng = position_in_range(pos, active_id)
            status = "в диапазоне" if in_rng else "ВНЕ диапазона"
            log.info(
                "Позиция %s: SOL=%s USDC=%s bins=[%s,%s] %s",
                pos.get("pubkey"),
                pos.get("sol"),
                pos.get("usdc"),
                pos.get("lowerBinId"),
                pos.get("upperBinId"),
                status,
            )

            now = datetime.now(timezone.utc)
            trend = format_price_trend(usdc_per_sol, bot_config.POLL_INTERVAL_SEC)
            price_line = f"📈 Цена SOL: ${usdc_per_sol:.2f}{trend}"
            range_line = format_range_bar(
                int(pos["lowerBinId"]),
                int(pos["upperBinId"]),
                active_id,
                usdc_per_sol,
                bin_step,
            )
            if not in_rng and out_of_range_since is None:
                out_of_range_since = now
                since_str = out_of_range_since.replace(microsecond=0).isoformat()
                log.warning(
                    "СОБЫТИЕ: activeId=%s вне bins позиции %s",
                    active_id,
                    pos.get("pubkey"),
                )
                send_telegram_message(
                    f"⚠️ <b>Цена вне диапазона позиции</b>\n"
                    f"{format_position_table(pos, usdc_per_sol)}\n"
                    f"{price_line}\n"
                    f"{range_line}\n"
                    f"С {since_str} UTC. Авто-ребаланса нет — только наблюдение."
                )
            elif in_rng and out_of_range_since is not None:
                duration = max(0.0, (now - out_of_range_since).total_seconds())
                out_of_range_since = None
                log.info("СОБЫТИЕ: вернулись в диапазон (%.0f сек вне)", duration)
                send_telegram_message(
                    f"✅ <b>Вернулись в диапазон</b>\n"
                    f"{format_position_table(pos, usdc_per_sol)}\n"
                    f"{price_line}\n"
                    f"{range_line}"
                )
                break

    except Exception:
        log.exception("Ошибка мониторинга (тик пропущен)")


async def main() -> None:
    log.info("=" * 50)
    log.info(
        "Meteora LP-бот | DRY_RUN=%s network=%s pool=%s",
        bot_config.DRY_RUN,
        bot_config.effective_network(),
        bot_config.pool_pubkey(),
    )
    log.info("=" * 50)

    if is_reopen_pending():
        meta = load_reopen_pending() or {}
        log.error("REOPEN_PENDING при старте: %s", meta)
        send_telegram_message(
            "⚠️ <b>ВНИМАНИЕ: reopen_pending</b>\n"
            "Прошлый /rebalance оборвался между закрытием и открытием.\n"
            f"<code>{meta}</code>\n"
            "Капитал на кошельке. Дожми /open или разберись вручную — "
            "бот НЕ будет молча продолжать как ни в чём не бывало."
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
    send_telegram_message(
        f"🤖 <b>Meteora LP-бот запущен</b>\n"
        f"Режим: {mode} · сеть: {bot_config.effective_network()}\n"
        f"Пул: <code>{bot_config.pool_pubkey()}</code>\n"
        f"Опрос: {bot_config.POLL_INTERVAL_SEC} сек"
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
