#!/usr/bin/env python3
"""Offline tests for C_PARITY Telegram handlers."""
from __future__ import annotations

import asyncio
import math
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot_config  # noqa: E402
import bot_state  # noqa: E402
import range_state  # noqa: E402
import telegram_commands as tg  # noqa: E402
from meteora_cycle_journal import (  # noqa: E402
    compute_divergence_usd,
    compute_would_be_value_usd,
)


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


def _update_with_args(args: list[str]):
    msg = _FakeMessage()
    update = MagicMock()
    update.effective_message = msg
    ctx = MagicMock()
    ctx.args = args
    return update, msg, ctx


class RangeConversionTests(unittest.TestCase):
    def test_pct_to_bins_binstep_1(self) -> None:
        # ln(1.05)/ln(1.0001) ≈ 487.9 → 488
        half = range_state.pct_to_half_width_bins(5.0, 1)
        expected = int(round(math.log(1.05) / math.log(1.0001)))
        self.assertEqual(half, expected)
        self.assertEqual(half, 488)

    def test_pct_to_bins_binstep_100(self) -> None:
        # ln(1.05)/ln(1.01) ≈ 4.89 → 5
        half = range_state.pct_to_half_width_bins(5.0, 100)
        expected = int(round(math.log(1.05) / math.log(1.01)))
        self.assertEqual(half, expected)
        self.assertEqual(half, 5)

    def test_pct_1_binstep_1(self) -> None:
        half = range_state.pct_to_half_width_bins(1.0, 1)
        expected = int(round(math.log(1.01) / math.log(1.0001)))
        self.assertEqual(half, expected)


class JournalMathTests(unittest.TestCase):
    def test_would_be_and_divergence(self) -> None:
        would = compute_would_be_value_usd(
            open_sol_qty=1.0, open_usdc_qty=10.0, close_price=100.0
        )
        self.assertEqual(would, 110.0)
        div = compute_divergence_usd(
            open_sol_qty=1.0,
            open_usdc_qty=10.0,
            close_price=100.0,
            close_position_value_usd=105.0,
        )
        self.assertEqual(div, -5.0)


class TelegramCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_without_args_rejects(self) -> None:
        update, msg, ctx = _update_with_args([])
        with patch("money_ops.open_with_budget") as mock_open:
            await tg.open_command(update, ctx)
            mock_open.assert_not_called()
        self.assertTrue(any("Использование" in r for r in msg.replies))

    async def test_open_negative_rejects(self) -> None:
        update, msg, ctx = _update_with_args(["-1"])
        with patch("money_ops.open_with_budget") as mock_open:
            await tg.open_command(update, ctx)
            mock_open.assert_not_called()
        self.assertTrue(any("больше 0" in r for r in msg.replies))

    async def test_open_refuses_when_position_exists(self) -> None:
        update, msg, ctx = _update_with_args(["5"])
        fake_pos = {"pubkey": "X", "sol": 0.1, "usdc": 1.0, "lowerBinId": 1, "upperBinId": 2}
        with patch("money_ops.get_primary_position", return_value=fake_pos):
            with patch(
                "meteora_ops.pool_info",
                return_value={"usdcPerSol": 100.0, "activeId": 0, "binStep": 1},
            ):
                with patch("money_ops.open_with_budget") as mock_open:
                    await tg.open_command(update, ctx)
                    mock_open.assert_not_called()
        self.assertTrue(any("уже открыта" in r for r in msg.replies))

    async def test_withdraw_without_confirm_does_not_exec(self) -> None:
        update, msg, ctx = _update_with_args([])
        fake_pos = {
            "pubkey": "X",
            "sol": 0.1,
            "usdc": 1.0,
            "lowerBinId": 1,
            "upperBinId": 2,
            "fees": {"sol": 0, "usdc": 0},
        }
        with patch("money_ops.get_primary_position", return_value=fake_pos):
            with patch(
                "meteora_ops.pool_info",
                return_value={"usdcPerSol": 100.0, "activeId": 0, "binStep": 1},
            ):
                with patch("money_ops.close_position_full") as mock_close:
                    await tg.withdraw_command(update, ctx)
                    mock_close.assert_not_called()
        self.assertTrue(any("confirm" in r for r in msg.replies))

    async def test_money_lock_blocks_parallel_open(self) -> None:
        await bot_state.money_lock.acquire()
        try:
            update, msg, ctx = _update_with_args(["5"])
            with patch("money_ops.open_with_budget") as mock_open:
                await tg.open_command(update, ctx)
                mock_open.assert_not_called()
            self.assertTrue(any("подожди" in r.lower() or "Идёт" in r for r in msg.replies))
        finally:
            bot_state.money_lock.release()

    async def test_dry_run_forces_devnet(self) -> None:
        with patch.object(bot_config, "DRY_RUN", True):
            with patch.dict(os.environ, {"METEORA_ALLOW_MAINNET": "1"}):
                kw = tg._exec_kwargs()
        self.assertEqual(kw["network"], "devnet")

    async def test_menu_matches_orca(self) -> None:
        names = [c.command for c in tg._MENU_COMMANDS]
        self.assertEqual(
            names,
            [
                "status",
                "pnl",
                "rebalance",
                "addliquidity",
                "open",
                "setrange",
                "pauza",
                "stop",
                "boevoy",
                "withdraw",
            ],
        )
        self.assertNotIn("close", names)
        self.assertNotIn("swap", names)


if __name__ == "__main__":
    unittest.main()
