"""Persistent Telegram reply keyboard + help copy (C5).

Buttons only invoke the existing ten owner commands — no new BotCommands.
"""
from __future__ import annotations

from telegram import KeyboardButton, ReplyKeyboardMarkup

# --- Button labels (must match MessageHandler routing exactly) ---
BTN_STATUS = "📊 Статус"
BTN_PNL = "📈 PnL"
BTN_REBALANCE = "🔄 Ребаланс"
BTN_ADD = "➕ Долить"
BTN_OPEN = "🆕 Открыть"
BTN_RANGE = "📐 Диапазон"
BTN_PAUSE = "⏸ Пауза"
BTN_COMBAT = "⚔️ Боевой"
BTN_STOP = "🛑 Стоп"
BTN_WITHDRAW = "❌ Закрыть позицию"
BTN_HELP = "❓ Справка"

# Layout: sense groups; dangerous actions on the bottom row.
KEYBOARD_ROWS: list[list[str]] = [
    [BTN_STATUS, BTN_PNL],
    [BTN_REBALANCE, BTN_ADD],
    [BTN_OPEN, BTN_RANGE],
    [BTN_PAUSE, BTN_COMBAT],
    [BTN_STOP, BTN_WITHDRAW],
    [BTN_HELP],
]

# One help line per button — tests fail if a keyboard button lacks a description.
BUTTON_HELP: dict[str, str] = {
    BTN_STATUS: (
        "сколько денег в позиции, где цена и в диапазоне ли она сейчас"
    ),
    BTN_PNL: (
        "сколько заработано комиссий по прошлым циклам и во что обошлись "
        "перестановки позиции"
    ),
    BTN_REBALANCE: (
        "закрыть позицию и открыть заново вокруг текущей цены. Стоит около "
        "0.02% от позиции, окупается примерно за 3 часа работы в диапазоне. "
        "Спросит подтверждение"
    ),
    BTN_ADD: (
        "добавить денег в уже открытую позицию — границы диапазона не меняются"
    ),
    BTN_OPEN: (
        "новая позиция вокруг текущей цены. Работает, только если открытой "
        "позиции ещё нет"
    ),
    BTN_RANGE: (
        "ширина будущих позиций в процентах. Шире — реже перестановки, но "
        "меньше комиссий; уже — наоборот"
    ),
    BTN_PAUSE: (
        "бот перестаёт сам следить и ребалансировать. Ручные кнопки работают"
    ),
    BTN_COMBAT: "снять паузу или полную заморозку — снова полный режим",
    BTN_STOP: (
        "полная заморозка: новые транзакции не отправляются; уже отправленная "
        "дойдёт до конца. Закрытие позиции всё ещё доступно"
    ),
    BTN_WITHDRAW: (
        "вывести всё из позиции в кошелёк бота. Спросит подтверждение. "
        "Работает даже при заморозке"
    ),
    BTN_HELP: "этот список — что делает каждая кнопка",
}


def all_button_labels() -> list[str]:
    return [btn for row in KEYBOARD_ROWS for btn in row]


def build_main_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура, которую владелец может свернуть.

    is_persistent намеренно НЕ включён: с ним Telegram запрещает прятать
    клавиатуру, и она занимает половину экрана поверх переписки. Без него
    рядом с полем ввода появляется значок клавиатуры — свернул и развернул
    когда нужно, а раскладка при этом не теряется.
    """
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label) for label in row] for row in KEYBOARD_ROWS],
        resize_keyboard=True,
        is_persistent=False,
    )


def format_help_message() -> str:
    lines = [
        "❓ <b>Справка по кнопкам</b>",
        "Нажимай кнопку внизу экрана — то же, что команда из меню.",
        "",
    ]
    for label in all_button_labels():
        desc = BUTTON_HELP[label]
        lines.append(f"• <b>{label}</b> — {desc}.")
    lines.append("")
    lines.append(
        "Команды с суммой или процентом (Открыть, Долить, Диапазон) сначала "
        "покажут подсказку с кликабельными примерами — отправь одну из них."
    )
    return "\n".join(lines)


def suggest_open_amounts_usd(wallet_usd: float) -> list[float]:
    """Pick up to 3 open sizes from real wallet USD, never above ~95% of it."""
    if wallet_usd <= 0:
        return []
    cap = wallet_usd * 0.95
    ladder = [1, 2, 5, 10, 15, 25, 50, 75, 100, 150, 200, 300, 500]
    picks = [float(x) for x in ladder if x <= cap]
    if not picks:
        # Tiny wallet: 25% / 50% / 90% of available.
        fracs = [0.25, 0.5, 0.9]
        picks = []
        for f in fracs:
            v = round(wallet_usd * f, 2)
            if v >= 0.01 and v not in picks:
                picks.append(v)
        return picks[:3]
    # Prefer spread: small / mid / near-max from those that fit.
    if len(picks) <= 3:
        return picks
    return [picks[0], picks[len(picks) // 2], picks[-1]]


def suggest_range_pcts(
    *, current_pct: float, max_pct: float, min_pct: float
) -> list[float]:
    """A few legal setrange values within [min_pct, max_pct]."""
    if max_pct < min_pct:
        return []
    raw = [
        min_pct,
        round(max_pct * 0.5, 4),
        round(max_pct * 0.9, 4),
        round(max_pct, 4),
        round(current_pct, 4),
    ]
    out: list[float] = []
    for v in raw:
        v = max(min_pct, min(max_pct, float(v)))
        # Avoid duplicates at 4 decimal places.
        if any(abs(v - x) < 1e-6 for x in out):
            continue
        out.append(v)
    return out[:4]
