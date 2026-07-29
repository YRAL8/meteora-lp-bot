"""Telegram command handlers — menu parity with orca-lp-bot."""
from __future__ import annotations

import json
import logging
from typing import Any

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

import bot_config
import bot_state
import meteora_cycle_journal as cycle_journal
import meteora_ops
import money_ops
import range_state
from meteora_exec import MeteoraExecError
from meteora_ops import MeteoraOpsError
from reopen_pending import is_reopen_pending, load_reopen_pending
from telegram_notify import (
    format_position_table,
    format_price_trend,
    format_range_bar,
    position_in_range,
)

log = logging.getLogger(__name__)

# Exactly Orca's owner command set (no /close, no /swap as Telegram commands).
_OWNER_COMMANDS = (
    "status",
    "pnl",
    "setrange",
    "rebalance",
    "addliquidity",
    "open",
    "pauza",
    "stop",
    "boevoy",
    "withdraw",
)

# Same order and labels as orca_bot/telegram_bot.py _MENU_COMMANDS.
_MENU_COMMANDS = [
    BotCommand("status", "Статус позиции и баланс"),
    BotCommand("pnl", "PnL по циклам (журнал)"),
    BotCommand("rebalance", "Ребаланс прямо сейчас"),
    BotCommand("addliquidity", "Долить ликвидность (нужна сумма)"),
    BotCommand("open", "Открыть новую позицию (нужна сумма)"),
    BotCommand("setrange", "Изменить ширину диапазона (нужен %)"),
    BotCommand("pauza", "Пауза автоматики"),
    BotCommand("stop", "Полная заморозка"),
    BotCommand("boevoy", "Вернуть в боевой режим"),
    BotCommand("withdraw", "Закрыть позицию (нужно подтверждение)"),
]

_EFF_SIDEWAYS_MAX = 0.3
_EFF_TREND_MIN = 0.7


def _owner_chat_id() -> int | None:
    if not bot_config.TELEGRAM_CHAT_ID or bot_config.is_placeholder(
        bot_config.TELEGRAM_CHAT_ID
    ):
        return None
    try:
        return int(bot_config.TELEGRAM_CHAT_ID)
    except ValueError:
        log.error("TELEGRAM_CHAT_ID не число — команды не зарегистрированы")
        return None


def _exec_kwargs() -> dict[str, Any]:
    return money_ops.exec_kwargs()


async def _reply(update: Update, text: str, **kwargs: Any) -> None:
    if update.effective_message is not None:
        await update.effective_message.reply_text(text, **kwargs)


def _reply_sync_collector(replies: list[str]):
    def _inner(text: str) -> None:
        replies.append(text)

    return _inner


async def unauthorized_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat = update.effective_chat
    chat_id = chat.id if chat is not None else None
    text = update.effective_message.text if update.effective_message else None
    command = text.split()[0] if text else "?"
    log.warning(
        "Ignored Telegram command %s from unauthorized chat_id=%s", command, chat_id
    )


async def register_menu_commands(app: Application) -> None:
    await app.bot.set_my_commands(_MENU_COMMANDS)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        owner = money_ops.owner()
        kw = money_ops.ops_kwargs()
        bal = meteora_ops.balances(owner, **kw)
        pool = meteora_ops.pool_info(**kw)
        positions = money_ops.list_open_positions()
        mode = "DEMO/devnet" if bot_config.DRY_RUN else "БОЕВОЙ"
        net = bot_config.effective_network()
        sol_ui = (bal.get("sol") or {}).get("ui", 0)
        usdc_ui = (bal.get("usdc") or {}).get("ui", 0)
        active_id = int(pool["activeId"])
        bin_step = int(pool["binStep"])
        usdc_per_sol = float(pool.get("usdcPerSol") or 0)
        trend = format_price_trend(usdc_per_sol, bot_config.POLL_INTERVAL_SEC)
        lines = [
            f"📊 <b>Статус [{mode}, {net}]</b>",
            f"Кошелёк: <code>{owner}</code>",
            f"SOL: {sol_ui:.6f} · USDC: {usdc_ui:.2f}",
            f"Пул: <code>{bot_config.pool_pubkey()}</code>",
            f"📈 Цена SOL: ${usdc_per_sol:.4f}{trend}",
            f"Диапазон по умолчанию: ±{range_state.current_range_pct():g}% "
            f"(half={range_state.current_half_width()} bins, binStep={bin_step})",
        ]
        if is_reopen_pending():
            lines.append("⚠️ <b>reopen_pending</b> — ребаланс оборван между close и open!")
        if not positions:
            lines.append("\n⏳ Открытых позиций нет.")
        else:
            for pos in positions:
                in_rng = position_in_range(pos, active_id)
                status = "✅ в диапазоне" if in_rng else "⚠️ ВНЕ диапазона"
                lines.append(format_position_table(pos, usdc_per_sol))
                lines.append(
                    format_range_bar(
                        int(pos["lowerBinId"]),
                        int(pos["upperBinId"]),
                        active_id,
                        usdc_per_sol,
                        bin_step,
                    )
                )
                lines.append(f"Статус: {status}")
        await _reply(update, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        log.exception("Ошибка /status")
        await _reply(update, f"❌ Ошибка /status: {e}")


def _market_bucket(efficiency: float | None) -> str:
    if efficiency is None:
        return "смешанный"
    if efficiency < _EFF_SIDEWAYS_MAX:
        return "боковик"
    if efficiency > _EFF_TREND_MIN:
        return "тренд"
    return "смешанный"


def _fmt_money(x: float) -> str:
    sign = "+" if x >= 0 else "−"
    return f"{sign}${abs(x):.2f}"


def _render_pnl_message(*, cycles: list[dict], recent_limit: int = 10) -> str:
    if not cycles:
        return (
            "📈 <b>PnL по циклам</b>\n"
            "Данных пока нет — журнал пуст.\n"
            "Совет: накопи хотя бы несколько ребалансов, потом вернись к /pnl."
        )
    recent = cycles[-recent_limit:]
    lines: list[str] = ["📈 <b>PnL по циклам</b>", "", f"Последние {len(recent)} циклов:"]
    for c in reversed(recent):
        close_t = str(c.get("close_time_utc", ""))[:16].replace("T", " ")
        width = float(c.get("range_width_pct", 0.0))
        dur_h = float(c.get("duration_hours", 0.0))
        fees_usd = float(c.get("fees_usd", 0.0))
        div_usd = float(c.get("divergence_usd", 0.0))
        pnl = float(c.get("pnl_usd", 0.0))
        eff = c.get("efficiency", None)
        eff_s = "None" if eff is None else f"{float(eff):.2f}"
        mark = " ⚠️" if bool(c.get("incomplete", False)) else ""
        lines.append(
            f"{close_t}Z | ±{width:.1f}% | {dur_h:.2f}h | fees ${fees_usd:.2f} | "
            f"div {_fmt_money(div_usd)} | итог {_fmt_money(pnl)} | eff {eff_s}{mark}"
        )
    complete = [c for c in cycles if not bool(c.get("incomplete", False))]
    lines.append("")
    lines.append("<b>Сводка (без неполных циклов)</b>")
    if not complete:
        lines.append("Нет полных циклов для сводки.")
        return "\n".join(lines)
    total_fees = sum(float(c.get("fees_usd", 0.0)) for c in complete)
    total_div = sum(float(c.get("divergence_usd", 0.0)) for c in complete)
    lines.append(
        f"Циклов: {len(complete)} · fees ${_fmt_money(total_fees).lstrip('+')} · "
        f"div {_fmt_money(total_div)}"
    )
    by_width: dict[float, list[dict]] = {}
    for c in complete:
        w = float(c.get("range_width_pct", 0.0))
        by_width.setdefault(w, []).append(c)
    for w in sorted(by_width.keys()):
        group = by_width[w]
        lines.append(f"")
        lines.append(f"Ширина ±{w:.1f}%:")
        buckets: dict[str, list[dict]] = {"боковик": [], "тренд": [], "смешанный": []}
        for c in group:
            eff = c.get("efficiency", None)
            eff_v = None if eff is None else float(eff)
            buckets[_market_bucket(eff_v)].append(c)
        for name in ("боковик", "тренд", "смешанный"):
            items = buckets[name]
            if not items:
                lines.append(f"  {name:<8} — нет данных")
                continue
            total_pnl = sum(float(i.get("pnl_usd", 0.0)) for i in items)
            total_h = sum(max(0.0, float(i.get("duration_hours", 0.0))) for i in items)
            avg_per_h = (total_pnl / total_h) if total_h > 0 else 0.0
            lines.append(
                f"  {name:<8} — {len(items)} циклов, итого {_fmt_money(total_pnl)}, "
                f"в среднем {_fmt_money(avg_per_h)}/ч"
            )
    return "\n".join(lines)


async def pnl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        j = cycle_journal.get_default_journal()
        cycles = j.read_all_cycles()
        text = _render_pnl_message(cycles=cycles, recent_limit=10)
        await _reply(update, text, parse_mode="HTML")
    except Exception as e:
        log.exception("Ошибка /pnl")
        await _reply(update, f"❌ Ошибка /pnl: {e}")


async def setrange_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _reply(
            update,
            "Использование: /setrange &lt;процент&gt;\n"
            "Пример: /setrange 0.3 или /setrange 1.2\n"
            f"Допустимо: от {range_state.MIN_RANGE_PCT} до {range_state.MAX_RANGE_PCT}\n"
            "(нижняя граница 0.05% — осознанное расхождение с Orca: "
            "в DLMM узкие диапазоны штатны; потолок сверху зависит от binStep пула)",
            parse_mode="HTML",
        )
        return
    try:
        pct = float(context.args[0])
    except ValueError:
        await _reply(update, "❌ Нужно число, например: /setrange 0.3 или /setrange 1.2")
        return
    if not (range_state.MIN_RANGE_PCT <= pct <= range_state.MAX_RANGE_PCT):
        await _reply(
            update,
            f"❌ Процент должен быть от {range_state.MIN_RANGE_PCT} до "
            f"{range_state.MAX_RANGE_PCT} (включительно).\n"
            "Нижняя граница 0.05% (не 1% как у Orca): у DLMM мелкие ячейки, "
            "узкие диапазоны — штатный режим концентрации ликвидности.",
        )
        return
    try:
        pool = meteora_ops.pool_info(**money_ops.ops_kwargs())
        bin_step = int(pool["binStep"])
        max_bins = int(pool["maxBinsPerPosition"])
        half = range_state.apply_setrange(
            pct, bin_step, max_bins_per_position=max_bins
        )
        price = float(pool.get("usdcPerSol") or 0)
        active_id = int(pool["activeId"])
        lo = active_id - half
        hi = active_id + half
        lo_p, hi_p = money_ops.bin_prices(active_id, price, bin_step, lo, hi)
        pos = money_ops.get_primary_position()
        if pos:
            range_block = (
                f"Текущая открытая позиция НЕ меняется: "
                f"bins [{pos['lowerBinId']},{pos['upperBinId']}]\n"
                f"Ориентировочно при следующем /rebalance или /open: "
                f"~${lo_p:.4f}–${hi_p:.4f} (bins [{lo},{hi}], half={half})\n"
            )
        else:
            range_block = (
                f"При следующем /open: bins [{lo},{hi}] "
                f"(~${lo_p:.4f}–${hi_p:.4f}), half={half}\n"
            )
        await _reply(
            update,
            f"✅ <b>RANGE_WIDTH_PCT = ±{pct}%</b>\n"
            f"binStep={bin_step} → half_width_bins={half}\n"
            f"📈 Цена SOL: ${price:.4f}\n"
            f"{range_block}"
            f"Изменение действует до перезапуска бота.",
            parse_mode="HTML",
        )
    except range_state.RangeTooWideError as e:
        await _reply(
            update,
            f"❌ Запрошено ±{e.requested_pct}% — больше максимума "
            f"±{e.max_pct:.4f}% на этом пуле (binStep={e.bin_step}).\n"
            f"Одна обычная позиция вмещает не больше {e.max_bins_per_position} "
            f"ячеек (SDK DEFAULT_BIN_PER_POSITION), то есть half ≤ "
            f"{e.max_half_width}.\n"
            "Состояние диапазона НЕ изменено.\n"
            "Что делать: взять пул с бо́льшим binStep (шире % на ячейку) "
            "или оставить узкий диапазон.",
        )
    except Exception as e:
        log.exception("/setrange failed")
        await _reply(update, f"❌ Ошибка /setrange: {e}")


async def open_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if bot_state.bot_frozen:
        await _reply(update, "🛑 Бот заморожен (/stop) — сначала /boevoy.")
        return
    if bot_state.money_lock.locked():
        await _reply(update, "⏳ Идёт ребаланс/открытие — подожди.")
        return
    if not context.args:
        await _reply(
            update,
            "Использование: /open &lt;сумма USDC&gt;\nПример: /open 5",
            parse_mode="HTML",
        )
        return
    try:
        usdc_amount = float(context.args[0])
    except ValueError:
        await _reply(update, "❌ Нужно число, например: /open 5")
        return
    if usdc_amount <= 0:
        await _reply(update, "❌ Сумма должна быть больше 0.")
        return

    async with bot_state.money_lock:
        existing = money_ops.get_primary_position()
        if existing is not None:
            price = float(
                meteora_ops.pool_info(**money_ops.ops_kwargs()).get("usdcPerSol") or 0
            )
            val = money_ops.position_value_usd(existing, price)
            await _reply(
                update,
                f"❌ Позиция уже открыта (${val:.2f}) — "
                "используй /rebalance или /addliquidity, а не /open.",
            )
            return
        replies: list[str] = []
        collector = _reply_sync_collector(replies)

        async def flush() -> None:
            for r in replies:
                await _reply(update, r, parse_mode="HTML", disable_web_page_preview=True)
            replies.clear()

        try:
            collector(f"🆕 Открываю новую позицию на ${usdc_amount:.2f} USDC…")
            await flush()
            money_ops.open_with_budget(usdc_amount, reply=collector)
            await flush()
        except MeteoraExecError as e:
            await _reply(update, f"❌ {e}")
        except Exception as e:
            log.exception("/open failed")
            await _reply(update, f"❌ Ошибка: {e}")


async def addliquidity_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if bot_state.bot_frozen:
        await _reply(update, "🛑 Бот заморожен (/stop) — сначала /boevoy.")
        return
    if bot_state.money_lock.locked():
        await _reply(update, "⏳ Идёт ребаланс — подожди, потом долей.")
        return

    want_max = bool(context.args) and context.args[0].lower() in (
        "max",
        "все",
        "всё",
        "весь",
    )
    if not context.args:
        pos = money_ops.get_primary_position()
        suggestion = ""
        if pos is not None:
            try:
                mx = money_ops.compute_max_addliquidity_usdc(pos)
                suggestion = f"\nМаксимум безопасно сейчас: ~${mx:.2f} (/addliquidity max)"
            except Exception:
                pass
        await _reply(
            update,
            "Использование: /addliquidity &lt;сумма USDC&gt;\n"
            f"Пример: /addliquidity 5{suggestion}",
            parse_mode="HTML",
        )
        return

    if want_max:
        usdc_amount: float | None = None
    else:
        try:
            usdc_amount = float(context.args[0])
        except ValueError:
            await _reply(
                update, "❌ Нужно число, например: /addliquidity 5, или /addliquidity max"
            )
            return
        if usdc_amount <= 0:
            await _reply(update, "❌ Сумма должна быть больше 0.")
            return

    async with bot_state.money_lock:
        position = money_ops.get_primary_position()
        if position is None:
            await _reply(update, "❌ Нет открытой позиции для доливки.")
            return
        if want_max:
            usdc_amount = money_ops.compute_max_addliquidity_usdc(position)
            if usdc_amount <= 0:
                await _reply(update, "❌ Нечем доливать — свободного USDC или SOL нет.")
                return
        assert usdc_amount is not None
        replies: list[str] = []
        collector = _reply_sync_collector(replies)

        async def flush() -> None:
            for r in replies:
                await _reply(update, r, parse_mode="HTML", disable_web_page_preview=True)
            replies.clear()

        try:
            pool = meteora_ops.pool_info(**money_ops.ops_kwargs())
            active_id = int(pool["activeId"])
            if not position_in_range(position, active_id):
                collector(
                    "⚠️ Позиция сейчас ВНЕ диапазона — доливка ляжет в основном в один токен."
                )
            collector(f"💧 Доливаю ${usdc_amount:.2f} USDC в текущую позицию...")
            await flush()
            money_ops.add_with_budget(position, usdc_amount, reply=collector)
            await flush()
        except MeteoraExecError as e:
            await _reply(update, f"❌ {e}")
        except Exception as e:
            log.exception("/addliquidity failed")
            await _reply(update, f"❌ Ошибка: {e}")


async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Full close with confirm. Works even under /stop (emergency exit, like Orca)."""
    confirmed = bool(context.args) and context.args[0].lower() == "confirm"
    try:
        position = money_ops.get_primary_position()
    except Exception as e:
        await _reply(update, f"❌ Не удалось загрузить позицию: {e}")
        return
    if position is None:
        await _reply(update, "❌ Открытой реальной позиции нет — закрывать нечего.")
        return

    pool = meteora_ops.pool_info(**money_ops.ops_kwargs())
    price = float(pool.get("usdcPerSol") or 0)
    val = money_ops.position_value_usd(position, price)
    if not confirmed:
        await _reply(
            update,
            f"⚠️ <b>Закрыть позицию:</b> ${val:.2f}\n"
            f"bins [{position['lowerBinId']},{position['upperBinId']}]\n\n"
            f"Деньги останутся в кошельке бота — никуда отдельно не переводятся.\n"
            f"Подтвердить: /withdraw confirm",
            parse_mode="HTML",
        )
        return

    # Clear reopen_pending — intentional withdraw, do not auto-reopen.
    from reopen_pending import set_reopen_pending

    try:
        set_reopen_pending(False)
    except Exception:
        log.exception("Не удалось снять reopen_pending при /withdraw")

    if bot_state.money_lock.locked():
        await _reply(update, "⏳ Идёт другая денежная операция — подожди.")
        return

    async with bot_state.money_lock:
        replies: list[str] = []
        collector = _reply_sync_collector(replies)

        async def flush() -> None:
            for r in replies:
                await _reply(update, r, parse_mode="HTML", disable_web_page_preview=True)
            replies.clear()

        try:
            collector("🔒 Закрываю позицию...")
            await flush()
            money_ops.close_position_full(position, reply=collector, record_cycle=True)
            await flush()
            await _reply(
                update, "✅ Позиция закрыта — средства теперь в кошельке бота."
            )
        except MeteoraExecError as e:
            await _reply(update, f"❌ {e}")
        except Exception as e:
            log.exception("/withdraw failed")
            await _reply(update, f"❌ Ошибка: {e}")


async def rebalance_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Manual rebalance. Blocked by /stop (bot_frozen), NOT by /pauza —
    /pauza only stops the automatic monitor tick; a manual Telegram call is an
    intentional owner action (same principle as Orca).
    """
    if bot_state.bot_frozen:
        await _reply(update, "🛑 Бот заморожен (/stop) — сначала /boevoy.")
        return
    if bot_state.money_lock.locked():
        await _reply(update, "⏳ Ребаланс уже выполняется — подожди.")
        return
    if is_reopen_pending():
        meta = load_reopen_pending() or {}
        await _reply(
            update,
            "⚠️ <b>reopen_pending уже стоит</b> — прошлый ребаланс оборвался между "
            f"close и open.\nmeta={json.dumps(meta, ensure_ascii=False)[:400]}\n"
            "Сначала дожми вручную (/open) или сними флаг осознанно.",
            parse_mode="HTML",
        )
        return

    crash = bool(context.args) and context.args[0].lower() in (
        "crash-after-close",
        "crash",
    )

    async with bot_state.money_lock:
        position = money_ops.get_primary_position()
        if position is None:
            await _reply(update, "❌ Нет открытой позиции для ребаланса.")
            return
        replies: list[str] = []
        collector = _reply_sync_collector(replies)

        async def flush() -> None:
            for r in replies:
                await _reply(update, r, parse_mode="HTML", disable_web_page_preview=True)
            replies.clear()

        try:
            collector("🔄 Начинаю ручной ребаланс...")
            await flush()
            money_ops.rebalance_position(
                position, reply=collector, crash_after_close=crash
            )
            await flush()
        except SystemExit:
            await flush()
            raise
        except MeteoraExecError as e:
            await _reply(update, f"❌ Ошибка ребаланса: {e}")
        except Exception as e:
            log.exception("/rebalance failed")
            await _reply(update, f"❌ Ошибка ребаланса: {e}")


async def pauza_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_state.bot_paused = True
    await _reply(
        update,
        "⏸ Автоматика приостановлена — авто-мониторинг не работает.\n"
        "Ручные команды (/rebalance, /addliquidity) по-прежнему доступны.\n"
        "Вернуть всё: /boevoy",
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_state.bot_frozen = True
    await _reply(
        update,
        "🛑 Полная заморозка — автоматика и /rebalance, /addliquidity, /open отключены.\n"
        "/withdraw confirm по-прежнему работает (аварийный выход).\n"
        "Вернуть всё: /boevoy",
    )


async def boevoy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_state.bot_paused = False
    bot_state.bot_frozen = False
    await _reply(
        update, "⚔️ Боевой режим — автоматика и все ручные команды снова работают."
    )


def build_telegram_app() -> Application:
    app = Application.builder().token(bot_config.TELEGRAM_BOT_TOKEN).build()
    owner_id = _owner_chat_id()
    if owner_id is None:
        log.error("TELEGRAM_CHAT_ID не задан — команды не зарегистрированы")
        return app
    owner_chat = filters.Chat(chat_id=owner_id)
    for name, handler in (
        ("status", status_command),
        ("pnl", pnl_command),
        ("setrange", setrange_command),
        ("rebalance", rebalance_command),
        ("addliquidity", addliquidity_command),
        ("open", open_command),
        ("pauza", pauza_command),
        ("stop", stop_command),
        ("boevoy", boevoy_command),
        ("withdraw", withdraw_command),
    ):
        app.add_handler(CommandHandler(name, handler, filters=owner_chat))
    app.add_handler(
        CommandHandler(
            list(_OWNER_COMMANDS),
            unauthorized_command,
            filters=~owner_chat,
        )
    )
    return app
