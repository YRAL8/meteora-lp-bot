#!/usr/bin/env python3
"""Offline tests for C6: total-USD budget, MAX_POSITION_USD, swap-fail abort."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot_config  # noqa: E402
import money_ops  # noqa: E402


def _suggest(total: float, *, sol_frac: float = 0.5, price: float = 100.0) -> dict:
    need_usdc = total * (1.0 - sol_frac)
    need_sol = (total * sol_frac) / price
    return {
        "needSol": need_sol,
        "needUsdc": need_usdc,
        "targetSolFraction": sol_frac,
        "params": {
            "minBinId": 1,
            "maxBinId": 5,
            "usdcPerSol": price,
        },
        "swapSuggestion": None,
    }


class SuggestTotalUsdTests(unittest.TestCase):
    def test_open_n_equals_total_position(self) -> None:
        price = 139.0
        for n in (1.0, 2.0, 5.0):
            with self.subTest(n=n):
                with patch(
                    "money_ops.meteora_ops.suggest_amounts",
                    return_value=_suggest(n, price=price),
                ) as sug:
                    out = money_ops.suggest_for_budget(n, half_width=30)
                    # Call used budget_usdc=n only (total semantics in TS).
                    kwargs = sug.call_args.kwargs
                    self.assertEqual(kwargs.get("budget_usdc"), n)
                    self.assertIsNone(kwargs.get("budget_sol"))
                    total = float(out["needSol"]) * price + float(out["needUsdc"])
                    self.assertAlmostEqual(total, n, places=6)


class MaxPositionCapTests(unittest.TestCase):
    def test_cap_announces_and_cuts(self) -> None:
        replies: list[str] = []
        with patch.object(bot_config, "MAX_POSITION_USD", 50.0):
            got = money_ops.apply_max_position_cap(
                128.0, reply=replies.append, action="открываю"
            )
        self.assertEqual(got, 50.0)
        self.assertTrue(any("MAX_POSITION_USD=$50" in r for r in replies))
        self.assertTrue(any("вместо $128" in r for r in replies))

    def test_no_cap_unchanged(self) -> None:
        replies: list[str] = []
        with patch.object(bot_config, "MAX_POSITION_USD", None):
            got = money_ops.apply_max_position_cap(128.0, reply=replies.append)
        self.assertEqual(got, 128.0)
        self.assertEqual(replies, [])

    def test_open_with_budget_uses_cap(self) -> None:
        replies: list[str] = []
        with patch.object(bot_config, "MAX_POSITION_USD", 10.0):
            with patch(
                "money_ops.meteora_ops.pool_info",
                return_value={
                    "maxBinsPerPosition": 70,
                    "usdcPerSol": 100.0,
                    "activeId": 0,
                    "binStep": 1,
                },
            ):
                with patch(
                    "money_ops.suggest_for_budget",
                    side_effect=lambda usd, **k: _suggest(usd, price=100.0),
                ) as sug:
                    with patch("money_ops.apply_swap_suggestion", return_value=True):
                        with patch(
                            "money_ops._wallet_balances",
                            return_value=(1.0, 100.0, 0.95),
                        ):
                            with patch(
                                "money_ops.meteora_exec.exec_open",
                                return_value={
                                    "ok": True,
                                    "params": {"positionPubkey": "P"},
                                    "signatures": ["sig"],
                                },
                            ):
                                with patch("money_ops.save_last_position"):
                                    with patch(
                                        "money_ops.list_open_positions",
                                        return_value=[],
                                    ):
                                        with patch(
                                            "money_ops.format_exec_replies",
                                            return_value="ok",
                                        ):
                                            with patch(
                                                "money_ops.cycle_journal.safe_call"
                                            ):
                                                money_ops.open_with_budget(
                                                    128.0, half_width=2, reply=replies.append
                                                )
                    # First suggest call must be for capped 10, not 128.
                    self.assertAlmostEqual(sug.call_args_list[0].args[0], 10.0)
        self.assertTrue(any("MAX_POSITION_USD" in r for r in replies))
        self.assertTrue(any("на ~$10.00" in r for r in replies))


class SwapFailAbortTests(unittest.TestCase):
    def test_failed_swap_does_not_open_crooked(self) -> None:
        replies: list[str] = []
        with patch.object(bot_config, "MAX_POSITION_USD", None):
            with patch(
                "money_ops.meteora_ops.pool_info",
                return_value={
                    "maxBinsPerPosition": 70,
                    "usdcPerSol": 100.0,
                    "activeId": 0,
                    "binStep": 1,
                },
            ):
                with patch(
                    "money_ops.suggest_for_budget",
                    return_value={
                        **_suggest(10.0, price=100.0),
                        "swapSuggestion": {"side": "sol-to-usdc", "amount": 0.01},
                    },
                ):
                    with patch(
                        "money_ops.apply_swap_suggestion", return_value=False
                    ):
                        # Wallet cannot fund a proper mix after failed swap.
                        with patch(
                            "money_ops._wallet_balances",
                            return_value=(0.06, 0.0, 0.01),
                        ):
                            with patch("money_ops.meteora_exec.exec_open") as op:
                                with self.assertRaises(money_ops.SwapFailedOpenAborted):
                                    money_ops.open_with_budget(
                                        10.0, half_width=2, reply=replies.append
                                    )
                                op.assert_not_called()
        self.assertTrue(any("крив" in r.lower() or "не открываю" in r.lower() for r in replies))

    def test_failed_swap_reopens_proportionally_when_wallet_allows(self) -> None:
        replies: list[str] = []
        calls: list[float] = []

        def sug(usd, **k):
            calls.append(usd)
            return _suggest(usd, price=100.0)

        with patch.object(bot_config, "MAX_POSITION_USD", None):
            with patch(
                "money_ops.meteora_ops.pool_info",
                return_value={
                    "maxBinsPerPosition": 70,
                    "usdcPerSol": 100.0,
                    "activeId": 0,
                    "binStep": 1,
                },
            ):
                with patch("money_ops.suggest_for_budget", side_effect=sug):
                    with patch(
                        "money_ops.apply_swap_suggestion", return_value=False
                    ):
                        with patch(
                            "money_ops._wallet_balances",
                            return_value=(0.2, 10.0, 0.15),
                        ):
                            with patch(
                                "money_ops.meteora_exec.exec_open",
                                return_value={
                                    "ok": True,
                                    "params": {"positionPubkey": "P"},
                                },
                            ) as op:
                                with patch("money_ops.save_last_position"):
                                    with patch(
                                        "money_ops.list_open_positions",
                                        return_value=[],
                                    ):
                                        with patch(
                                            "money_ops.format_exec_replies",
                                            return_value="ok",
                                        ):
                                            with patch(
                                                "money_ops.cycle_journal.safe_call"
                                            ):
                                                money_ops.open_with_budget(
                                                    10.0,
                                                    half_width=2,
                                                    reply=replies.append,
                                                )
                                                op.assert_called_once()
        self.assertTrue(any("пропорцию" in r.lower() for r in replies))
        self.assertGreaterEqual(len(calls), 2)


class AddBudgetTests(unittest.TestCase):
    def test_add_uses_total_usd(self) -> None:
        replies: list[str] = []
        pos = {
            "pubkey": "Pos",
            "lowerBinId": 1,
            "upperBinId": 5,
            "sol": 0.05,
            "usdc": 5.0,
        }
        with patch.object(bot_config, "MAX_POSITION_USD", None):
            with patch(
                "money_ops.meteora_ops.pool_info",
                return_value={"usdcPerSol": 100.0},
            ):
                with patch(
                    "money_ops.suggest_for_budget",
                    side_effect=lambda usd, **k: _suggest(usd, price=100.0),
                ) as sug:
                    with patch("money_ops.apply_swap_suggestion", return_value=True):
                        with patch(
                            "money_ops._wallet_balances",
                            return_value=(1.0, 50.0, 0.95),
                        ):
                            with patch(
                                "money_ops.meteora_exec.exec_add",
                                return_value={"ok": True},
                            ):
                                with patch(
                                    "money_ops.format_exec_replies",
                                    return_value="ok",
                                ):
                                    with patch("money_ops.cycle_journal.safe_call"):
                                        money_ops.add_with_budget(
                                            pos, 3.0, reply=replies.append
                                        )
                    self.assertAlmostEqual(sug.call_args.args[0], 3.0)
        self.assertTrue(any("~$3.00" in r for r in replies))


class ComputeMaxTests(unittest.TestCase):
    def test_max_uses_both_legs(self) -> None:
        pos = {"lowerBinId": 1, "upperBinId": 5, "sol": 0.1, "usdc": 10.0}
        with patch.object(bot_config, "MAX_POSITION_USD", None):
            with patch(
                "money_ops.meteora_ops.balances",
                return_value={"sol": {"ui": 0.15}, "usdc": {"ui": 20.0}},
            ):
                with patch(
                    "money_ops.meteora_ops.pool_info",
                    return_value={"usdcPerSol": 100.0},
                ):
                    # $1 total → 0.005 SOL + 0.5 USDC
                    with patch(
                        "money_ops.suggest_for_budget",
                        return_value=_suggest(1.0, sol_frac=0.5, price=100.0),
                    ):
                        mx = money_ops.compute_max_addliquidity_usdc(pos)
        # usable_sol = 0.15 - MIN_SOL_BALANCE (0.08) = 0.07
        usable = 0.15 - money_ops.MIN_SOL_BALANCE
        self.assertAlmostEqual(mx, usable / 0.005 * 0.98, places=4)


if __name__ == "__main__":
    unittest.main()
