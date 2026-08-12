#!/usr/bin/env python3
"""Offline tests for C_FIX: range ceiling + empty close branch."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import money_ops  # noqa: E402
import range_state  # noqa: E402
import telegram_commands as tg  # noqa: E402


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


class MaxPctTableTests(unittest.TestCase):
    """Control values from TASK_C_FIX §1b (max_half=34 → maxBins=70)."""

    MAX_BINS = 70

    def test_max_pct_table(self) -> None:
        cases = {
            1: 0.3406,
            4: 1.3690,
            10: 3.4567,
            100: 40.2577,
        }
        for bin_step, expected in cases.items():
            with self.subTest(bin_step=bin_step):
                got = range_state.max_pct_for_pool(bin_step, self.MAX_BINS)
                self.assertAlmostEqual(got, expected, places=4)
                half = range_state.pct_to_half_width_bins(expected, bin_step)
                self.assertEqual(half, 34)

    def test_max_half_from_sdk_constant(self) -> None:
        self.assertEqual(range_state.max_half_width_bins(70), 34)
        self.assertEqual(range_state.max_half_width_bins(71), 35)


class SetrangeCeilingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._prev_half = range_state.half_width_bins
        self._prev_pct = range_state.range_width_pct
        range_state.half_width_bins = 10
        range_state.range_width_pct = 0.1

    async def asyncTearDown(self) -> None:
        range_state.half_width_bins = self._prev_half
        range_state.range_width_pct = self._prev_pct

    async def test_setrange_refuses_over_max_and_keeps_state(self) -> None:
        update, msg, ctx = _update_with_args(["5"])
        with patch(
            "meteora_ops.pool_info",
            return_value={
                "binStep": 1,
                "maxBinsPerPosition": 70,
                "usdcPerSol": 100.0,
                "activeId": 0,
            },
        ):
            with patch("money_ops.get_primary_position", return_value=None):
                await tg.setrange_command(update, ctx)
        self.assertEqual(range_state.current_half_width(), 10)
        self.assertAlmostEqual(range_state.current_range_pct(), 0.1)
        text = "\n".join(msg.replies)
        self.assertIn("больше максимума", text)
        self.assertIn("0.3406", text)
        self.assertIn("НЕ изменено", text)

    async def test_setrange_accepts_within_limit(self) -> None:
        update, msg, ctx = _update_with_args(["0.3"])
        with patch(
            "meteora_ops.pool_info",
            return_value={
                "binStep": 1,
                "maxBinsPerPosition": 70,
                "usdcPerSol": 100.0,
                "activeId": 1000,
            },
        ):
            with patch("money_ops.get_primary_position", return_value=None):
                await tg.setrange_command(update, ctx)
        half = range_state.pct_to_half_width_bins(0.3, 1)
        self.assertEqual(range_state.current_half_width(), half)
        self.assertAlmostEqual(range_state.current_range_pct(), 0.3)
        text = "\n".join(msg.replies)
        self.assertIn("Диапазон:", text)
        self.assertIn(f"{range_state.corridor_bins(half)} ячеек", text)
        self.assertNotIn("действует до перезапуска", text)


class OpenCeilingTests(unittest.TestCase):
    def test_open_with_budget_rejects_oversize_half(self) -> None:
        replies: list[str] = []
        with patch(
            "meteora_ops.pool_info",
            return_value={"maxBinsPerPosition": 70, "binStep": 1},
        ):
            with patch("meteora_ops.suggest_amounts") as sug:
                with self.assertRaises(range_state.HalfWidthTooWideError):
                    money_ops.open_with_budget(
                        5.0, half_width=35, reply=replies.append
                    )
                sug.assert_not_called()
        self.assertTrue(any("превышает потолок" in r for r in replies))


class EmptyCloseBranchTests(unittest.TestCase):
    def test_empty_position_uses_close_empty(self) -> None:
        empty_pos = {
            "pubkey": "Ghost111",
            "sol": 0.0,
            "usdc": 0.0,
            "fees": {"sol": 0, "usdc": 0},
            "raw": {"totalXAmount": "0", "totalYAmount": "0"},
        }
        replies: list[str] = []
        with patch(
            "meteora_ops.pool_info",
            return_value={"usdcPerSol": 100.0},
        ):
            with patch("meteora_exec.exec_close_empty") as close_empty:
                with patch("meteora_exec.exec_close") as close_full:
                    close_empty.return_value = {
                        "ok": True,
                        "signatures": ["sigEmpty"],
                        "sends": [],
                    }
                    with patch("money_ops.clear_last_position"):
                        with patch("money_ops.format_exec_replies", return_value="ok"):
                            money_ops.close_position_full(
                                empty_pos, reply=replies.append, record_cycle=False
                            )
                    close_empty.assert_called_once()
                    close_full.assert_not_called()
        self.assertTrue(any("closePositionIfEmpty" in r for r in replies))

    def test_live_position_uses_remove_liquidity_close(self) -> None:
        live_pos = {
            "pubkey": "Live111",
            "sol": 0.1,
            "usdc": 1.0,
            "fees": {"sol": 0, "usdc": 0},
            "raw": {"totalXAmount": "100", "totalYAmount": "200"},
        }
        replies: list[str] = []
        with patch(
            "meteora_ops.pool_info",
            return_value={"usdcPerSol": 100.0},
        ):
            with patch("meteora_exec.exec_close_empty") as close_empty:
                with patch("meteora_exec.exec_close") as close_full:
                    close_full.return_value = {
                        "ok": True,
                        "signatures": ["sigFull"],
                        "sends": [],
                    }
                    with patch("money_ops.clear_last_position"):
                        with patch("money_ops.format_exec_replies", return_value="ok"):
                            with patch("money_ops.cycle_journal.safe_call"):
                                money_ops.close_position_full(
                                    live_pos, reply=replies.append, record_cycle=True
                                )
                    close_full.assert_called_once()
                    close_empty.assert_not_called()
        self.assertTrue(any("withdraw 100%" in r for r in replies))


if __name__ == "__main__":
    unittest.main()
