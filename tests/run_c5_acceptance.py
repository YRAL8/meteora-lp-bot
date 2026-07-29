#!/usr/bin/env python3
"""C5 acceptance: button text → same handlers as commands; capture reply text.

Opens a tiny position so Rebalance/Withdraw confirmation prompts are real,
then closes it. Does not enable AUTO_REBALANCE.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "WALLET_KEYPAIR_PATH",
    os.path.expanduser("~/.config/solana/meteora-devnet.json"),
)

import money_ops  # noqa: E402
import range_state  # noqa: E402
import telegram_commands as tg  # noqa: E402
import telegram_keyboard as kb  # noqa: E402
from reopen_pending import set_reopen_pending  # noqa: E402


class _FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


def _ctx(args: list[str] | None = None):
    ctx = MagicMock()
    ctx.args = list(args or [])
    return ctx


async def press(label: str) -> list[str]:
    msg = _FakeMessage(label)
    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = MagicMock()
    update.effective_chat.id = 1
    await tg.keyboard_button_handler(update, _ctx())
    return msg.replies


async def cmd(name: str, args: list[str] | None = None) -> list[str]:
    msg = _FakeMessage("")
    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = MagicMock()
    update.effective_chat.id = 1
    await getattr(tg, f"{name}_command")(update, _ctx(args))
    return msg.replies


def show(title: str, replies: list[str]) -> None:
    print(f"\n=== {title} ===")
    for r in replies:
        print(r[:1500])
        print("---")


async def main() -> None:
    set_reopen_pending(False)
    print(f"owner={money_ops.owner()} pool={money_ops.ops_kwargs().get('pool')}")

    show("кнопка Справка", await press(kb.BTN_HELP))

    r_btn = await press(kb.BTN_STATUS)
    r_cmd = await cmd("status")
    show("кнопка Статус", r_btn)
    show("команда /status", r_cmd)
    assert r_btn and r_cmd

    show("кнопка Открыть (подсказка)", await press(kb.BTN_OPEN))
    show("кнопка Диапазон", await press(kb.BTN_RANGE))

    # Ensure a live position so confirm prompts are meaningful.
    if money_ops.get_primary_position() is None:
        print("\n(opening tiny position for confirm demos…)")
        range_state.half_width_bins = 2
        money_ops.open_with_budget(0.3, reply=lambda m: print("open>>", m[:160]))

    r_reb = await press(kb.BTN_REBALANCE)
    show("кнопка Ребаланс (ожидаем /rebalance confirm)", r_reb)
    assert any("confirm" in x for x in r_reb)
    assert not any("Начинаю ручной ребаланс" in x for x in r_reb)

    r_wd = await press(kb.BTN_WITHDRAW)
    show("кнопка Закрыть позицию (ожидаем /withdraw confirm)", r_wd)
    assert any("confirm" in x for x in r_wd)

    # Cleanup via confirmed withdraw (real tx)
    print("\n(cleanup /withdraw confirm…)")
    r_close = await cmd("withdraw", ["confirm"])
    show("cleanup withdraw confirm", r_close)

    print("\nC5 acceptance done.")


if __name__ == "__main__":
    asyncio.run(main())
