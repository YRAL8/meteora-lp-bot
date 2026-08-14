#!/usr/bin/env python3
"""Offline tests for C5 Telegram reply keyboard."""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot_config  # noqa: E402
import telegram_commands as tg  # noqa: E402
import telegram_keyboard as kb  # noqa: E402


class _FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[str] = []
        self.kwargs: list[dict] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)
        self.kwargs.append(kwargs)


def _update_with_text(text: str, *, chat_id: int = 42):
    msg = _FakeMessage(text)
    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    ctx = MagicMock()
    ctx.args = []
    return update, msg, ctx


def _update_with_args(args: list[str]):
    msg = _FakeMessage("")
    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = MagicMock()
    update.effective_chat.id = 42
    ctx = MagicMock()
    ctx.args = args
    return update, msg, ctx


class KeyboardConsistencyTests(unittest.TestCase):
    def test_help_covers_every_button(self) -> None:
        labels = kb.all_button_labels()
        self.assertEqual(len(labels), len(set(labels)))
        for label in labels:
            self.assertIn(label, kb.BUTTON_HELP, f"missing help for {label!r}")
        # Extra help entries without a button also fail the contract.
        for label in kb.BUTTON_HELP:
            self.assertIn(label, labels, f"help for unknown button {label!r}")

    def test_help_message_lists_all(self) -> None:
        text = kb.format_help_message()
        for label in kb.all_button_labels():
            self.assertIn(label, text)

    def test_rebalance_help_follows_model_for_position_cap(self) -> None:
        for cap in (12.0, 50.0, 400.0):
            with self.subTest(cap=cap):
                with patch.object(bot_config, "MAX_POSITION_USD", cap):
                    text = kb.format_help_message()
                    line = next(
                        ln for ln in text.splitlines() if kb.BTN_REBALANCE in ln
                    )
                    pct = bot_config.effective_rebalance_cost_frac(cap) * 100.0
                    hours = bot_config.effective_payback_hours(cap)
                    self.assertIn(f"{pct:.2f}%", line)
                    self.assertIn(f"{hours:.1f} ч", line)
                    self.assertIn(f"${cap:g}", line)
                    self.assertNotIn("3 часа", line)

    def test_rebalance_help_without_cap_names_no_cost_figures(self) -> None:
        with patch.object(bot_config, "MAX_POSITION_USD", None):
            text = kb.format_help_message()
            line = next(ln for ln in text.splitlines() if kb.BTN_REBALANCE in ln)
            self.assertIn("зависит от размера позиции", line)
            self.assertNotIn("0.02%", line)
            self.assertNotIn("3 часа", line)
            self.assertNotIn("%", line)
            self.assertNotIn(" ч.", line)

    def test_menu_lists_owner_commands(self) -> None:
        names = [c.command for c in tg._MENU_COMMANDS]
        self.assertEqual(
            names,
            [
                "status",
                "pnl",
                "rebalance",
                "addliquidity",
                "open",
                "claim",
                "setrange",
                "pauza",
                "stop",
                "boevoy",
                "withdraw",
            ],
        )
        self.assertNotIn("start", names)
        self.assertNotIn("help", names)


class OpenSuggestionsTests(unittest.TestCase):
    def test_suggestions_from_balance_not_hardcoded(self) -> None:
        small = kb.suggest_open_amounts_usd(8.0)
        self.assertTrue(small)
        self.assertTrue(all(a <= 8.0 * 0.95 + 1e-9 for a in small))
        self.assertNotIn(100, small)
        self.assertNotIn(50, small)

        mid = kb.suggest_open_amounts_usd(64.0)
        self.assertTrue(mid)
        self.assertTrue(all(a <= 64.0 * 0.95 + 1e-9 for a in mid))
        self.assertIn(50, mid)  # ladder value that fits
        self.assertTrue(max(mid) <= 64 * 0.95)

        tiny = kb.suggest_open_amounts_usd(0.8)
        self.assertTrue(tiny)
        self.assertTrue(all(a <= 0.8 for a in tiny))


class ButtonRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_safe_button_calls_matching_handler(self) -> None:
        mapping = {
            kb.BTN_STATUS: "status_command",
            kb.BTN_PNL: "pnl_command",
            kb.BTN_PAUSE: "pauza_command",
            kb.BTN_STOP: "stop_command",
            kb.BTN_COMBAT: "boevoy_command",
            kb.BTN_HELP: "help_button",
        }
        for label, handler_name in mapping.items():
            with self.subTest(label=label):
                update, msg, ctx = _update_with_text(label)
                with patch(f"telegram_commands.{handler_name}", autospec=True) as h:
                    h.return_value = None
                    await tg.keyboard_button_handler(update, ctx)
                    h.assert_called_once()

    async def test_open_add_range_use_prompts(self) -> None:
        for label, prompt in (
            (kb.BTN_OPEN, "open_prompt"),
            (kb.BTN_ADD, "addliquidity_prompt"),
            (kb.BTN_RANGE, "setrange_prompt"),
        ):
            with self.subTest(label=label):
                update, msg, ctx = _update_with_text(label)
                with patch(f"telegram_commands.{prompt}", autospec=True) as h:
                    h.return_value = None
                    await tg.keyboard_button_handler(update, ctx)
                    h.assert_called_once()

    async def test_rebalance_and_withdraw_call_commands(self) -> None:
        for label, handler_name in (
            (kb.BTN_REBALANCE, "rebalance_command"),
            (kb.BTN_WITHDRAW, "withdraw_command"),
        ):
            update, msg, ctx = _update_with_text(label)
            with patch(f"telegram_commands.{handler_name}", autospec=True) as h:
                h.return_value = None
                await tg.keyboard_button_handler(update, ctx)
                h.assert_called_once()

    async def test_rebalance_button_does_not_exec_without_confirm(self) -> None:
        text = await self._rebalance_preview(sol=0.1, usdc=1.0, price=100.0)
        self.assertIn("/rebalance confirm", text)
        val = 0.1 * 100.0 + 1.0
        cost = bot_config.effective_rebalance_cost_usd(val)
        frac_pct = bot_config.effective_rebalance_cost_frac(val) * 100.0
        hours = bot_config.effective_payback_hours(val)
        self.assertIn(f"${cost:.4f}", text)
        self.assertIn(f"{frac_pct:.3f}%", text)
        self.assertIn(f"{hours:.1f} ч", text)
        self.assertNotIn("0.02%", text)

    async def test_rebalance_confirm_at_12_usd_names_working_payback(self) -> None:
        text = await self._rebalance_preview(sol=0.1, usdc=2.0, price=100.0)
        val = 12.0
        cost = bot_config.effective_rebalance_cost_usd(val)
        frac_pct = bot_config.effective_rebalance_cost_frac(val) * 100.0
        hours = bot_config.effective_payback_hours(val)
        self.assertAlmostEqual(hours, 8.7, places=1)
        self.assertIn("8.7 ч", text)
        self.assertIn(f"${cost:.4f}", text)
        self.assertIn(f"{frac_pct:.3f}%", text)
        self.assertNotIn("3 часа", text)
        self.assertNotIn("0.02%", text)

    async def _rebalance_preview(
        self, *, sol: float, usdc: float, price: float
    ) -> str:
        update, msg, ctx = _update_with_text(kb.BTN_REBALANCE)
        fake_pos = {
            "pubkey": "X",
            "sol": sol,
            "usdc": usdc,
            "lowerBinId": 1,
            "upperBinId": 3,
            "fees": {"sol": 0, "usdc": 0},
        }
        with patch("money_ops.get_primary_position", return_value=fake_pos):
            with patch(
                "meteora_ops.pool_info",
                return_value={"usdcPerSol": price, "activeId": 2, "binStep": 1},
            ):
                with patch("money_ops.rebalance_position") as reb:
                    await tg.keyboard_button_handler(update, ctx)
                    reb.assert_not_called()
        return "\n".join(msg.replies)

    async def test_unauthorized_message_ignored(self) -> None:
        update, msg, ctx = _update_with_text(kb.BTN_STATUS, chat_id=999)
        with self.assertLogs(level=logging.WARNING) as cm:
            await tg.unauthorized_message(update, ctx)
        self.assertTrue(any("unauthorized" in r.lower() for r in cm.output))
        self.assertEqual(msg.replies, [])


class StartKeyboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_attaches_keyboard(self) -> None:
        update, msg, ctx = _update_with_args([])
        await tg.start_command(update, ctx)
        self.assertTrue(msg.replies)
        self.assertTrue(msg.kwargs)
        markup = msg.kwargs[0].get("reply_markup")
        self.assertIsNotNone(markup)
        rows = [[b.text for b in row] for row in markup.keyboard]
        self.assertEqual(rows, kb.KEYBOARD_ROWS)


if __name__ == "__main__":
    unittest.main()
