import html
import logging
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

import bot_config

log = logging.getLogger(__name__)

# Лимит Telegram Bot API на длину текста одного сообщения.
TG_MAX_MESSAGE_LEN = 4096

# Скользящее окно цены для тренда в /status — тот же принцип, что в orca-lp-bot
# (telegram_bot.py: deque(maxlen=12)). При POLL_INTERVAL_SEC=300 12 точек — это час;
# пополняется только тиками monitor_position() в main.py, не вызовами /status.
price_history: deque[float] = deque(maxlen=12)


def escape_html(text: object) -> str:
    """Экранировать внешний текст для parse_mode=HTML (<, >, &)."""
    return html.escape(str(text), quote=False)


def mode_label() -> str:
    """Что стоит на кону — деньги или проба. Одно место на все сообщения."""
    return "демо" if bot_config.DRY_RUN else "реальные деньги"


def format_stamp(now: datetime | None = None) -> str:
    """Дата и время для человека: местное, плюс UTC — журнал ведётся в UTC.

    Telegram показывает рядом с сообщением только часы, а день приходится
    угадывать по разделителю в ленте. Сердцебиение приходит и ночью, и через
    сутки молчания — из «12:59» не понять, какого оно числа.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    try:
        local = now.astimezone(ZoneInfo(bot_config.DISPLAY_TIMEZONE))
    except Exception:
        # Нет tzdata в образе или опечатка в поясе — показываем UTC, но никогда
        # не молчим и не падаем: отметка времени не стоит потерянного сообщения.
        return now.strftime("%d.%m.%Y %H:%M UTC")
    # Город, а не аббревиатура пояса: «Berlin» читается, «CEST» надо помнить.
    city = bot_config.DISPLAY_TIMEZONE.rsplit("/", 1)[-1].replace("_", " ")
    return f"{local:%d.%m.%Y %H:%M} {city}"


def format_html_error(prefix: str, err: object) -> str:
    """Префикс + экранированный текст ошибки для HTML-сообщений."""
    return f"{prefix}{escape_html(err)}"


def short_addr(addr: str, head: int = 4, tail: int = 4) -> str:
    """B6EfBviRXRRXW8XdwzGNoLCTiPuB7tshXHtMLkJvedRh -> B6Ef…vedRh.

    Полный адрес в сообщении нечитаем и занимает всю строку; для сверки в
    обозревателе хватает начала и хвоста, они уникальны на практике.
    """
    a = str(addr or "")
    if len(a) <= head + tail + 1:
        return a
    return f"{a[:head]}…{a[-tail:]}"


def format_position_table(position: dict, usdc_per_sol: float) -> str:
    """SOL/USDC composition table for one DLMM position — стиль orca-lp-bot."""
    sol = float(position.get("sol") or 0)
    usdc = float(position.get("usdc") or 0)
    fees = position.get("fees") or {}
    fee_sol = float(fees.get("sol") or 0)
    fee_usdc = float(fees.get("usdc") or 0)

    value_sol_usd = sol * usdc_per_sol
    value_usdc_usd = usdc  # 1 USDC ~= $1
    total_usd = value_sol_usd + value_usdc_usd
    fees_usd = fee_sol * usdc_per_sol + fee_usdc

    table = (
        f"{'':5}{'qty':>10}  {'USD':>8}\n"
        f"{'SOL':5}{sol:>10.4f}  ${value_sol_usd:>7.2f}\n"
        f"{'USDC':5}{usdc:>10.2f}  ${value_usdc_usd:>7.2f}\n"
        f"{'─' * 25}\n"
        f"{'TOTAL':5}{'':10}  ${total_usd:>7.2f}\n"
        f"{'Fees':5}{'':10}  ${fees_usd:>7.2f}"
    )
    return f"💰 <b>Позиция: ${total_usd:.2f}</b>\n<pre>{table}</pre>"


def format_range_bar(
    lower_bin: int,
    upper_bin: int,
    active_bin: int,
    active_price_usd: float,
    bin_step: int,
) -> str:
    """Прогресс-бар диапазона в $ (не в bin-ID) — стиль orca-lp-bot.

    Цены на границах выводятся из активной цены масштабированием на
    (1+binStep/10000)^(bin-active_bin) — тот же геометрический шаг ячейки,
    которым Meteora считает цену внутри пула, без обращения к decimals токенов.
    """
    width = 16
    span = upper_bin - lower_bin
    frac = (active_bin - lower_bin) / span if span > 0 else 0.5
    filled = min(max(round(frac * width), 0), width)
    bar = "█" * filled + "░" * (width - filled)

    growth = 1 + bin_step / 10_000
    lo_price = active_price_usd * (growth ** (lower_bin - active_bin))
    hi_price = active_price_usd * (growth ** (upper_bin - active_bin))

    extra = ""
    if active_bin < lower_bin and lo_price:
        extra = f" ↓ {(lo_price - active_price_usd) / lo_price * 100:.1f}%"
    elif active_bin > upper_bin and hi_price:
        extra = f" ↑ +{(active_price_usd - hi_price) / hi_price * 100:.1f}%"

    return (
        f"<code>{bar}</code>{extra}\n"
        f"L ${lo_price:.2f}  C ${active_price_usd:.2f}  U ${hi_price:.2f}"
    )


def _format_trend_period(seconds: int) -> str:
    minutes = max(1, seconds // 60) if seconds > 0 else 0
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    rem = minutes % 60
    return f"{hours}ч" if rem == 0 else f"{hours}ч {rem}мин"


def format_price_trend(current_price: float, poll_interval_sec: int) -> str:
    """Стрелка и % изменения цены от старейшей точки price_history.

    Пустая строка, пока накопилось меньше 2 отсчётов (сразу после старта бота).
    """
    if len(price_history) < 2:
        return ""
    oldest = price_history[0]
    if oldest <= 0:
        return ""
    change_pct = (current_price - oldest) / oldest * 100
    if change_pct > 0.5:
        arrow = "↗"
    elif change_pct < -0.5:
        arrow = "↘"
    else:
        arrow = "→"
    period = _format_trend_period(len(price_history) * poll_interval_sec)
    sign = "+" if change_pct >= 0 else ""
    return f" ({arrow} {sign}{change_pct:.1f}% за {period})"


def position_in_range(position: dict, active_id: int) -> bool:
    lo = int(position["lowerBinId"])
    hi = int(position["upperBinId"])
    return lo <= active_id <= hi


def format_exec_replies(payload: dict) -> str:
    """Format exec.ts JSON with explorer links."""
    sends = payload.get("sends") or []
    if sends:
        lines = ["✅ <b>Транзакция отправлена</b>"]
        for s in sends:
            sig = escape_html(s.get("signature", ""))
            explorer = str(s.get("explorer") or "")
            # Только http(s) — иначе href ломает HTML или уводит куда не надо.
            if explorer.startswith("https://") or explorer.startswith("http://"):
                href = html.escape(explorer, quote=True)
                lines.append(f'<a href="{href}">{sig}</a>')
            else:
                lines.append(f"<code>{sig}</code>")
        return "\n".join(lines)
    sigs = payload.get("signatures") or []
    if sigs:
        cluster = bot_config.explorer_cluster()
        q = "?cluster=devnet" if cluster == "devnet" else ""
        lines = ["✅ <b>Транзакция отправлена</b>"]
        for sig in sigs:
            safe = escape_html(sig)
            href = html.escape(
                f"https://explorer.solana.com/tx/{sig}{q}", quote=True
            )
            lines.append(f'<a href="{href}">{safe}</a>')
        return "\n".join(lines)
    return "✅ Готово (подпись не найдена в ответе)"


def send_telegram_message(text: str) -> None:
    """Best-effort Telegram notification; never raises."""
    token = bot_config.TELEGRAM_BOT_TOKEN
    chat_id = bot_config.TELEGRAM_CHAT_ID

    if bot_config.is_placeholder(token) or bot_config.is_placeholder(chat_id):
        return
    if not text:
        return
    if len(text) > TG_MAX_MESSAGE_LEN:
        # Грубая обрезка — вызывающий код (например /pnl) должен укладываться сам.
        text = text[: TG_MAX_MESSAGE_LEN - 3] + "..."

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        log.warning("Telegram sendMessage failed (request error).", exc_info=True)
        return

    if resp.status_code != 200:
        body = (resp.text or "").strip()
        if len(body) > 500:
            body = body[:500] + "..."
        log.warning(
            "Telegram sendMessage failed (HTTP %s). Response: %s",
            resp.status_code,
            body,
        )
