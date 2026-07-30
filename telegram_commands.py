"""Telegram command handlers — menu parity with orca-lp-bot + C5 keyboard."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import bot_config
import bot_state
import meteora_cycle_journal as cycle_journal
import meteora_exec
import meteora_ops
import money_ops
import range_state
import telegram_keyboard as kb
from meteora_exec import MeteoraExecError
from meteora_ops import MeteoraOpsError
from money_ops import SwapFailedOpenAborted
from reopen_pending import is_reopen_pending, load_reopen_pending
from telegram_notify import (
    TG_MAX_MESSAGE_LEN,
    escape_html,
    format_position_table,
    format_price_trend,
    format_range_bar,
    position_in_range,
    short_addr,
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
# /start and keyboard help are NOT part of this ten-command menu.
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


async def _run_money(fn, /, *args, **kwargs):
    """Run blocking money/RPC work off the asyncio event loop (C8)."""
    return await asyncio.to_thread(fn, *args, **kwargs)


async def _handle_journal_block(update: Update, err: BaseException) -> bool:
    """If exec failed due to unresolved journal, try resolve and explain in chat."""
    text = str(err)
    if "journal has unresolved" not in text.lower() and "unresolved entries" not in text.lower():
        return False
    await _reply(
        update,
        "⚠️ Журнал транзакций заблокировал денежную операцию. "
        "Опрашиваю подписи заново…",
    )
    try:
        kw = {
            "network": money_ops.exec_kwargs().get("network", "devnet"),
            "rpc": money_ops.exec_kwargs().get("rpc"),
            "pool": money_ops.exec_kwargs().get("pool"),
            "extra_env": money_ops.exec_kwargs().get("extra_env"),
        }
        result = await _run_money(meteora_exec.resolve_journal, timeout_ms=60_000, **kw)
    except Exception as e:
        await _reply(update, f"❌ Не удалось опросить журнал: {e}")
        return True
    still = result.get("stillUnresolved") or []
    cleared = int(result.get("cleared") or 0)
    if not still:
        await _reply(
            update,
            f"✅ Журнал разрешён (снято записей: {cleared}). Повтори команду.",
        )
        return True
    lines = [
        f"⚠️ После опроса всё ещё неясно ({len(still)} подпис.). "
        f"Снято: {cleared}.",
        "Проверь в обозревателе, затем:",
        "• /status journal — опросить ещё раз",
        "• /status journal-forget &lt;подпись&gt; confirm — снять ОДНУ "
        "запись после опроса сети",
        "",
    ]
    for u in still[:5]:
        sig = escape_html(u.get("signature", "?"))
        st = escape_html(u.get("status", "?"))
        exp = u.get("explorer") or ""
        if exp:
            lines.append(f"• <code>{sig}</code> ({st}) — {escape_html(exp)}")
        else:
            lines.append(f"• <code>{sig}</code> ({st})")
    await _reply(update, "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
    return True


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
    args = [a.lower() for a in (context.args or [])]
    if args and args[0] in ("journal", "journal-forget"):
        await _journal_status_command(update, context)
        return
    try:
        owner = money_ops.owner()
        kw = money_ops.ops_kwargs()
        bal = await _run_money(meteora_ops.balances, owner, **kw)
        pool = await _run_money(meteora_ops.pool_info, **kw)
        positions = await _run_money(money_ops.list_open_positions)
        mode = "DEMO" if bot_config.DRY_RUN else "БОЕВОЙ"
        net = bot_config.effective_network()
        sol_ui = (bal.get("sol") or {}).get("ui", 0)
        usdc_ui = (bal.get("usdc") or {}).get("ui", 0)
        active_id = int(pool["activeId"])
        bin_step = int(pool["binStep"])
        usdc_per_sol = float(pool.get("usdcPerSol") or 0)
        trend = format_price_trend(usdc_per_sol, bot_config.POLL_INTERVAL_SEC)
        price_line = f"📈 Цена SOL: ${usdc_per_sol:,.2f}{trend}"

        # Порядок строк как в /status боевого orca-lp-bot: заголовок -> позиция ->
        # цена -> диапазон -> статус -> балансы. Технические подробности (адреса,
        # ячейки, binStep) уходят одной сноской в конец, чтобы не забивать экран.
        lines = [f"📊 <b>Статус [{mode}]</b>"]

        if is_reopen_pending():
            lines.append("⚠️ <b>Ребаланс оборвался</b> между закрытием и открытием!")

        if not positions:
            lines.append("⏳ Открытых позиций нет.")
            lines.append(price_line)
        else:
            for pos in positions:
                in_rng = position_in_range(pos, active_id)
                status = "✅ в диапазоне" if in_rng else "⚠️ ВНЕ диапазона"
                lines.append(format_position_table(pos, usdc_per_sol))
                lines.append(price_line)
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

        cap = getattr(bot_config, "MAX_POSITION_USD", None)
        cap_note = f" · потолок ${cap:g}" if cap else ""
        # Показываем ширину, посчитанную из фактических ячеек и binStep этого пула,
        # а не сохранённый процент: при старте они расходятся (пришедшие из env 5%
        # против дефолтных 34 ячеек = ±0.34% на binStep=1), и цифра врала бы.
        half = range_state.current_half_width()
        actual_pct = ((1.0 + bin_step / 10_000.0) ** half - 1.0) * 100.0
        lines.append(
            f"<i>Новые позиции: ±{actual_pct:.2f}% ({half} ячеек){cap_note}</i>"
        )
        lines.append(
            f"<i>{net} · кошелёк {short_addr(owner)} · пул "
            f"{short_addr(bot_config.pool_pubkey())}</i>"
        )
        await _reply(update, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        log.exception("Ошибка /status")
        await _reply(update, f"❌ Ошибка /status: {e}")


async def _journal_status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Chat unlock for stuck exec_journal — no new BotCommand (uses /status args)."""
    args = [a.lower() for a in (context.args or [])]
    forget = bool(args) and args[0] == "journal-forget"
    if forget:
        # /status journal-forget <signature> confirm
        if len(args) < 3 or args[-1] != "confirm":
            await _reply(
                update,
                "Снять одну запись журнала:\n"
                "/status journal-forget &lt;подпись&gt; confirm\n"
                "Бот сам опросит сеть. Массового снятия больше нет. "
                "Аварийный выход: /status journal или /withdraw confirm.",
                parse_mode="HTML",
            )
            return
        signature = context.args[1].strip() if context.args and len(context.args) > 1 else ""
        if not signature or signature.lower() == "confirm":
            await _reply(
                update,
                "❌ Нужна подпись: /status journal-forget &lt;подпись&gt; confirm",
                parse_mode="HTML",
            )
            return
        net = money_ops.exec_kwargs().get("network", "devnet")
        rpc = money_ops.exec_kwargs().get("rpc")
        result = await _run_money(
            meteora_exec.forget_unresolved_journal,
            signature=signature,
            rpc=rpc,
            network=net,
        )
        if not result.get("ok"):
            refuse = result.get("refuse")
            if refuse == "confirmed":
                import html as _html

                cluster = bot_config.explorer_cluster()
                q = "?cluster=devnet" if cluster == "devnet" else ""
                exp = f"https://explorer.solana.com/tx/{signature}{q}"
                href = _html.escape(exp, quote=True)
                await _reply(
                    update,
                    "❌ Подпись <b>подтверждена</b> в сети — забывать нельзя.\n"
                    "Разреши через /status journal или смотри "
                    f'<a href="{href}">{escape_html(signature)}</a>.',
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return
            await _reply(
                update,
                f"❌ Не снял: {escape_html(result.get('error') or result)}",
                parse_mode="HTML",
            )
            return
        outcome = result.get("outcome")
        if outcome == "not_found_expired":
            await _reply(
                update,
                f"🔓 Считал подпись <code>{escape_html(signature)}</code> "
                f"непрошедшей (не найдена после окна подтверждения). "
                f"Снято: {result.get('cleared', 0)}.",
                parse_mode="HTML",
            )
        else:
            await _reply(
                update,
                f"🔓 Журнал: снято записей: {result.get('cleared', 0)} "
                f"(outcome={escape_html(outcome)}).",
                parse_mode="HTML",
            )
        return

    await _reply(update, "🔎 Опрашиваю неподтверждённые подписи в журнале…")
    try:
        kw = {
            "network": money_ops.exec_kwargs().get("network", "devnet"),
            "rpc": money_ops.exec_kwargs().get("rpc"),
            "pool": money_ops.exec_kwargs().get("pool"),
            "extra_env": money_ops.exec_kwargs().get("extra_env"),
        }
        result = await _run_money(meteora_exec.resolve_journal, timeout_ms=60_000, **kw)
    except Exception as e:
        await _reply(update, f"❌ /status journal: {e}")
        return
    still = result.get("stillUnresolved") or []
    cleared = int(result.get("cleared") or 0)
    if not still:
        await _reply(
            update,
            f"✅ Журнал чист (разрешено за этот опрос: {cleared}).",
        )
        return
    lines = [
        f"⚠️ Неразрешённых записей: {len(still)} (снято сейчас: {cleared}).",
        "Повтори /status journal позже или сними ОДНУ подпись:",
        "/status journal-forget &lt;подпись&gt; confirm",
        "",
    ]
    for u in still[:8]:
        sig = escape_html(u.get("signature", "?"))
        st = escape_html(u.get("status", "?"))
        exp = u.get("explorer") or ""
        lines.append(
            f"• <code>{sig}</code> ({st})"
            + (f"\n  {escape_html(exp)}" if exp else "")
        )
    await _reply(update, "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


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


def _render_pnl_message(
    *,
    cycles: list[dict],
    recent_limit: int = 10,
    include_width_detail: bool = True,
) -> str:
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
    if not include_width_detail:
        return "\n".join(lines)
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


def render_pnl_message(*, cycles: list[dict], recent_limit: int = 10) -> str:
    """Собрать /pnl и уложить в лимит Telegram (4096), укорачивая список циклов."""
    text = _render_pnl_message(cycles=cycles, recent_limit=recent_limit)
    if len(text) <= TG_MAX_MESSAGE_LEN:
        return text
    for limit in range(min(recent_limit, len(cycles)) - 1, 0, -1):
        text = _render_pnl_message(cycles=cycles, recent_limit=limit)
        if len(text) <= TG_MAX_MESSAGE_LEN:
            note = f"\n<i>… показано последних {limit} из {len(cycles)} циклов</i>"
            if len(text) + len(note) <= TG_MAX_MESSAGE_LEN:
                return text + note
            return text
    text = _render_pnl_message(
        cycles=cycles, recent_limit=1, include_width_detail=False
    )
    if len(text) <= TG_MAX_MESSAGE_LEN:
        return text
    cut = TG_MAX_MESSAGE_LEN - 20
    return text[:cut] + "\n… (обрезано)"


async def pnl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        j = cycle_journal.get_default_journal()
        cycles = j.read_all_cycles()
        text = render_pnl_message(cycles=cycles, recent_limit=10)
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
            await _run_money(money_ops.open_with_budget, usdc_amount, reply=collector)
            await flush()
        except SwapFailedOpenAborted as e:
            await flush()
            await _reply(update, f"❌ Открытие отменено: {e}")
        except MeteoraExecError as e:
            await flush()
            if await _handle_journal_block(update, e):
                return
            await _reply(update, f"❌ {e}")
        except Exception as e:
            log.exception("/open failed")
            await flush()
            if await _handle_journal_block(update, e):
                return
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
            pool = await _run_money(meteora_ops.pool_info, **money_ops.ops_kwargs())
            active_id = int(pool["activeId"])
            if not position_in_range(position, active_id):
                collector(
                    "⚠️ Позиция сейчас ВНЕ диапазона — доливка ляжет в основном в один токен."
                )
            half = range_state.current_half_width()
            if half > 34:
                collector(
                    f"⚠️ Широкий диапазон (half={half}): долив может потребовать "
                    "нескольких транзакций. Поздние части могут сорваться на "
                    "просроченном blockhash — часть денег войдёт, часть нет. "
                    "Узкий диапазон (/setrange) безопаснее."
                )
            await _run_money(
                money_ops.add_with_budget, position, usdc_amount, reply=collector
            )
            await flush()
        except SwapFailedOpenAborted:
            # add_with_budget уже отправил одно понятное сообщение с причиной —
            # второе «Доливка отменена: swap failed on add» было техническим дублем.
            await flush()
        except MeteoraExecError as e:
            await flush()
            if await _handle_journal_block(update, e):
                return
            await _reply(update, f"❌ {e}")
        except Exception as e:
            log.exception("/addliquidity failed")
            await flush()
            if await _handle_journal_block(update, e):
                return
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

    # Clear reopen_pending only after a confirmed close — never before the lock
    # or the exec (audit C10: early clear left capital invisible).
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
            await _run_money(
                money_ops.close_position_full,
                position,
                reply=collector,
                record_cycle=True,
                trigger="manual",
            )
            await flush()
            from reopen_pending import set_reopen_pending

            try:
                set_reopen_pending(False)
            except Exception:
                log.exception("Не удалось снять reopen_pending после /withdraw")
            await _reply(
                update,
                "✅ Позиция закрыта — средства в кошельке бота. "
                "reopen_pending снят (если был).",
            )
        except MeteoraExecError as e:
            await flush()
            if await _handle_journal_block(update, e):
                return
            await _reply(update, f"❌ {e}")
        except Exception as e:
            log.exception("/withdraw failed")
            await flush()
            if await _handle_journal_block(update, e):
                return
            await _reply(update, f"❌ Ошибка: {e}")


async def rebalance_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Manual rebalance. Blocked by /stop (bot_frozen), NOT by /pauza —
    /pauza only stops the automatic monitor tick; a manual Telegram call is an
    intentional owner action (same principle as Orca).

    Bare /rebalance (and the keyboard button) only shows a confirmation prompt.
    Run with `/rebalance confirm`.
    """
    if bot_state.bot_frozen:
        await _reply(update, "🛑 Бот заморожен (/stop) — сначала /boevoy.")
        return
    if bot_state.money_lock.locked():
        await _reply(update, "⏳ Ребаланс уже выполняется — подожди.")
        return
    if is_reopen_pending():
        meta = load_reopen_pending() or {}
        meta_s = escape_html(json.dumps(meta, ensure_ascii=False)[:400])
        await _reply(
            update,
            "⚠️ <b>reopen_pending уже стоит</b> — прошлый ребаланс оборвался между "
            f"close и open.\nmeta={meta_s}\n"
            "Сначала дожми вручную (/open) или /status journal.",
            parse_mode="HTML",
        )
        return

    args = [a.lower() for a in (context.args or [])]
    # Crash hooks removed from Telegram (C8) — use METEORA_CRASH_AFTER_CLOSE=1
    # from tests/run_rebalance_crash.py only.
    if args and args[0] in ("crash-after-close", "crash"):
        await _reply(
            update,
            "❌ Тестовый крючок /rebalance crash из чата убран. "
            "Обычный путь: /rebalance confirm.",
        )
        return
    confirmed = bool(args) and args[0] == "confirm"
    if not confirmed:
        try:
            position = await _run_money(money_ops.get_primary_position)
        except Exception as e:
            await _reply(update, f"❌ Не удалось загрузить позицию: {e}")
            return
        if position is None:
            await _reply(update, "❌ Нет открытой позиции для ребаланса.")
            return
        if (
            bot_config.effective_network() == "mainnet"
            and bot_config.MAX_POSITION_USD is None
        ):
            await _reply(
                update,
                "❌ На mainnet без MAX_POSITION_USD ручной /rebalance "
                "запрещён (иначе откроет ≈ весь кошелёк). Задай потолок в .env.",
            )
            return
        pool = await _run_money(meteora_ops.pool_info, **money_ops.ops_kwargs())
        price = float(pool.get("usdcPerSol") or 0)
        val = money_ops.position_value_usd(position, price)
        cost_est = val * 0.0002  # ~0.02% of position
        half = range_state.current_half_width()
        pct = range_state.current_range_pct()
        await _reply(
            update,
            f"🔄 <b>Ребаланс</b>\n"
            f"Сейчас: ${val:.2f} · bins "
            f"[{position['lowerBinId']},{position['upperBinId']}]\n"
            f"Будет: закрыть → при необходимости свопнуть → открыть вокруг "
            f"текущей цены (±{pct}% / half={half}).\n"
            f"Ориентир стоимости перестановки: ~${cost_est:.4f} "
            f"(≈0.02% от позиции).\n\n"
            f"Подтвердить: /rebalance confirm",
            parse_mode="HTML",
        )
        return

    if (
        bot_config.effective_network() == "mainnet"
        and bot_config.MAX_POSITION_USD is None
    ):
        await _reply(
            update,
            "❌ На mainnet без MAX_POSITION_USD ручной /rebalance "
            "запрещён (иначе откроет ≈ весь кошелёк). Задай потолок в .env.",
        )
        return

    async with bot_state.money_lock:
        position = await _run_money(money_ops.get_primary_position)
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
            payload = await _run_money(
                money_ops.rebalance_position, position, reply=collector
            )
            await flush()
            if payload is None or is_reopen_pending():
                await _reply(
                    update,
                    "🚨 Ребаланс НЕ завершён — reopen_pending остаётся. "
                    "/status (есть ли позиция), затем /open только если позиции нет. "
                    "Журнал: /status journal.",
                )
        except SwapFailedOpenAborted as e:
            await flush()
            await _reply(update, f"❌ Ребаланс: реоткрытие отменено: {e}")
        except money_ops.SwapOutcomeUnknown as e:
            await flush()
            await _reply(
                update,
                f"⚠️ Своп с неясным исходом — дальше не шёл. {e}",
            )
        except money_ops.OpenMateriallyUndersized as e:
            await flush()
            await _reply(
                update,
                f"❌ Реоткрытие урезано слишком сильно "
                f"(${e.actual_usd:.2f} vs ${e.requested_usd:.2f}) — не успех.",
            )
        except money_ops.StopRequested:
            await flush()
            await _reply(
                update,
                "🛑 Ребаланс остановлен по /stop — новые tx не отправлял. "
                "Если close уже прошёл, капитал на кошельке; см. reopen_pending.",
            )
        except MeteoraExecError as e:
            await flush()
            if await _handle_journal_block(update, e):
                return
            await _reply(update, f"❌ Ошибка ребаланса: {e}")
        except Exception as e:
            log.exception("/rebalance failed")
            await flush()
            if await _handle_journal_block(update, e):
                return
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
        "🛑 Полная заморозка — автоматика и /rebalance, /addliquidity, /open "
        "отключены.\n"
        "Новые транзакции не отправляются; уже отправленная доходит до конца.\n"
        "/withdraw confirm по-прежнему работает (аварийный выход).\n"
        "Вернуть всё: /boevoy",
    )


async def boevoy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_state.bot_paused = False
    bot_state.bot_frozen = False
    await _reply(
        update,
        "⚔️ Боевой режим — автоматика и все ручные команды снова работают.",
        reply_markup=kb.build_main_keyboard(),
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show persistent keyboard. Not part of the ten-command BotFather menu."""
    await _reply(
        update,
        "🤖 <b>Meteora LP-бот</b>\n"
        "Кнопки внизу — то же, что команды меню. "
        "Нажми «❓ Справка», если непонятно, что делает кнопка.",
        parse_mode="HTML",
        reply_markup=kb.build_main_keyboard(),
    )


async def help_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(
        update,
        kb.format_help_message(),
        parse_mode="HTML",
        reply_markup=kb.build_main_keyboard(),
    )


async def open_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keyboard «Открыть»: context + clickable /open N suggestions from balance."""
    try:
        owner = money_ops.owner()
        kw = money_ops.ops_kwargs()
        bal = meteora_ops.balances(owner, **kw)
        pool = meteora_ops.pool_info(**kw)
        price = float(pool.get("usdcPerSol") or 0)
        sol_ui = float((bal.get("sol") or {}).get("ui") or 0)
        usdc_ui = float((bal.get("usdc") or {}).get("ui") or 0)
        wallet_usd = sol_ui * price + usdc_ui
        amounts = kb.suggest_open_amounts_usd(wallet_usd)
        half = range_state.current_half_width()
        # Процент считаем из фактических ячеек и binStep пула, а не из сохранённого
        # range_width_pct: при старте тот приходит из env (5%) и не совпадает с
        # дефолтными 34 ячейками (±0.34% при binStep=1) — подсказка бы врала.
        bin_step = int(pool["binStep"])
        pct = ((1.0 + bin_step / 10_000.0) ** half - 1.0) * 100.0
        existing = money_ops.get_primary_position()
        if existing is not None:
            val = money_ops.position_value_usd(existing, price)
            await _reply(
                update,
                f"🆕 Открыть позицию\n"
                f"Уже есть открытая (~${val:.2f}) — сначала /rebalance или "
                f"/withdraw confirm.\n"
                f"На кошельке: {sol_ui:.4f} SOL + {usdc_ui:.2f} USDC "
                f"(≈${wallet_usd:.0f})",
            )
            return
        if not amounts:
            await _reply(
                update,
                f"🆕 Открыть позицию\n"
                f"На кошельке: {sol_ui:.4f} SOL + {usdc_ui:.2f} USDC "
                f"(≈${wallet_usd:.2f}) — мало для открытия.\n"
                f"Пополни кошелёк или укажи сумму вручную: /open &lt;USDC&gt;",
                parse_mode="HTML",
            )
            return
        examples = "     ".join(f"/open {a:g}" for a in amounts)
        await _reply(
            update,
            f"🆕 <b>Открыть позицию</b>\n"
            f"На кошельке: {sol_ui:.4f} SOL + {usdc_ui:.2f} USDC "
            f"(≈${wallet_usd:.0f})\n"
            f"Диапазон сейчас: ±{pct:.2f}% ({half} ячеек)\n\n"
            f"Отправь сумму в USDC-эквиваленте (нажми пример):\n"
            f"{examples}",
            parse_mode="HTML",
        )
    except Exception as e:
        log.exception("open_prompt failed")
        await _reply(update, f"❌ Не удалось подготовить подсказку /open: {e}")


async def addliquidity_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    try:
        pos = money_ops.get_primary_position()
        if pos is None:
            await _reply(
                update,
                "➕ Долить\nОткрытой позиции нет — сначала «🆕 Открыть» или /open.",
            )
            return
        owner = money_ops.owner()
        kw = money_ops.ops_kwargs()
        bal = meteora_ops.balances(owner, **kw)
        pool = meteora_ops.pool_info(**kw)
        price = float(pool.get("usdcPerSol") or 0)
        sol_ui = float((bal.get("sol") or {}).get("ui") or 0)
        usdc_ui = float((bal.get("usdc") or {}).get("ui") or 0)
        wallet_usd = sol_ui * price + usdc_ui
        try:
            mx = money_ops.compute_max_addliquidity_usdc(pos)
        except Exception:
            mx = wallet_usd * 0.5
        cap = max(0.0, min(wallet_usd * 0.95, mx if mx > 0 else wallet_usd * 0.95))
        amounts = kb.suggest_open_amounts_usd(cap)
        val = money_ops.position_value_usd(pos, price)
        if not amounts:
            await _reply(
                update,
                f"➕ Долить в позицию (~${val:.2f})\n"
                f"Свободно мало. Можно: /addliquidity max",
            )
            return
        examples = "     ".join(f"/addliquidity {a:g}" for a in amounts)
        await _reply(
            update,
            f"➕ <b>Долить</b> в позицию (~${val:.2f})\n"
            f"На кошельке ≈${wallet_usd:.0f}; безопасно до ~${cap:.2f}.\n"
            f"Границы позиции не меняются.\n\n"
            f"Отправь сумму или нажми пример:\n"
            f"{examples}     /addliquidity max",
            parse_mode="HTML",
        )
    except Exception as e:
        log.exception("addliquidity_prompt failed")
        await _reply(update, f"❌ Не удалось подготовить подсказку: {e}")


async def setrange_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        pool = meteora_ops.pool_info(**money_ops.ops_kwargs())
        bin_step = int(pool["binStep"])
        max_bins = int(pool["maxBinsPerPosition"])
        max_pct = range_state.max_pct_for_pool(bin_step, max_bins)
        max_half = range_state.max_half_width_bins(max_bins)
        cur = range_state.current_range_pct()
        half = range_state.current_half_width()
        opts = kb.suggest_range_pcts(
            current_pct=cur,
            max_pct=max_pct,
            min_pct=range_state.MIN_RANGE_PCT,
        )
        examples = "     ".join(f"/setrange {p:g}" for p in opts)
        await _reply(
            update,
            f"📐 <b>Диапазон</b>\n"
            f"Сейчас: ±{cur:g}% (half={half} ячеек)\n"
            f"Максимум на этом пуле (binStep={bin_step}): ±{max_pct:.4f}% "
            f"(half ≤ {max_half}, ≤{max_bins} ячеек в позиции)\n"
            f"Минимум ввода: {range_state.MIN_RANGE_PCT}%\n\n"
            f"Отправь процент или нажми пример:\n"
            f"{examples}",
            parse_mode="HTML",
        )
    except Exception as e:
        log.exception("setrange_prompt failed")
        await _reply(update, f"❌ Не удалось подготовить подсказку /setrange: {e}")


async def keyboard_button_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Map reply-keyboard labels to the same handlers as slash commands."""
    msg = update.effective_message
    if msg is None or not msg.text:
        return
    text = msg.text.strip()
    context.args = []

    if text == kb.BTN_STATUS:
        await status_command(update, context)
    elif text == kb.BTN_PNL:
        await pnl_command(update, context)
    elif text == kb.BTN_REBALANCE:
        await rebalance_command(update, context)
    elif text == kb.BTN_ADD:
        await addliquidity_prompt(update, context)
    elif text == kb.BTN_OPEN:
        await open_prompt(update, context)
    elif text == kb.BTN_RANGE:
        await setrange_prompt(update, context)
    elif text == kb.BTN_PAUSE:
        await pauza_command(update, context)
    elif text == kb.BTN_COMBAT:
        await boevoy_command(update, context)
    elif text == kb.BTN_STOP:
        await stop_command(update, context)
    elif text == kb.BTN_WITHDRAW:
        await withdraw_command(update, context)
    elif text == kb.BTN_HELP:
        await help_button(update, context)


async def unauthorized_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat = update.effective_chat
    chat_id = chat.id if chat is not None else None
    text = update.effective_message.text if update.effective_message else None
    log.warning(
        "Ignored Telegram message from unauthorized chat_id=%s text=%r",
        chat_id,
        (text or "")[:80],
    )


def build_telegram_app() -> Application:
    app = Application.builder().token(bot_config.TELEGRAM_BOT_TOKEN).build()
    owner_id = _owner_chat_id()
    if owner_id is None:
        log.error("TELEGRAM_CHAT_ID не задан — команды не зарегистрированы")
        return app
    owner_chat = filters.Chat(chat_id=owner_id)
    for name, handler in (
        ("start", start_command),
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
    button_filter = filters.TEXT & ~filters.COMMAND & owner_chat
    app.add_handler(MessageHandler(button_filter, keyboard_button_handler))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~owner_chat,
            unauthorized_message,
        )
    )
    return app
