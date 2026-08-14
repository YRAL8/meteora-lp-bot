#!/usr/bin/env python3
"""LITE_14 part 1: width ceiling is POSITION_MAX_LENGTH; rent follows bin count."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot_config  # noqa: E402
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


def _pool(*, bin_step: int = 4, active_id: int = -6422, price: float = 76.67, max_bins: int = 1400) -> dict:
    return {
        "binStep": bin_step,
        "activeId": active_id,
        "usdcPerSol": price,
        "maxBinsPerPosition": max_bins,
        "defaultBinsPerPosition": 70,
        "maxBinLengthAllowedInOneTx": 26,
    }


def _bal(*, sol: float = 2.0, usdc: float = 80.0, rent_sol: float = 0.05740608) -> dict:
    return {
        "sol": {"ui": sol},
        "usdc": {"ui": usdc},
        "feeReserveSol": 0.02,
        "solAvailableForOpen": max(0.0, sol - 0.02 - rent_sol),
        "positionRentForDefaultOpen": {
            "sol": rent_sol,
            "binCount": 69,
            "lamports": int(rent_sol * 1e9),
            "onChainSizeBytes": money_ops.position_onchain_size_bytes(69),
        },
        "ata": {
            "usdc": {"exists": True, "rentLamports": 2_039_280},
            "wsol": {"exists": True, "rentLamports": 2_039_280},
        },
    }


def _suggest(*, price: float = 76.67) -> dict:
    return {
        "needSol": 0.05,
        "needUsdc": 6.0,
        "params": {"usdcPerSol": price},
        "swapSuggestion": None,
    }


def _rent_from_estimate(text: str) -> float:
    m = re.search(r"залог\s+([0-9.]+)\s+SOL", text)
    if not m:
        raise AssertionError(f"no rent line in estimate:\n{text}")
    return float(m.group(1))


class PositionRentFormulaTests(unittest.TestCase):
    def test_size_flat_until_default_then_grows(self) -> None:
        self.assertEqual(
            money_ops.position_onchain_size_bytes(26),
            money_ops.position_onchain_size_bytes(70),
        )
        self.assertGreater(
            money_ops.position_onchain_size_bytes(91),
            money_ops.position_onchain_size_bytes(70),
        )
        extra = 91 - 70
        self.assertEqual(
            money_ops.position_onchain_size_bytes(91)
            - money_ops.position_onchain_size_bytes(70),
            extra * 112,
        )

    def test_rent_matches_measured_probe_at_91_and_140(self) -> None:
        info = {
            "sol": 0.05740608,
            "binCount": 69,
            "lamports": 57_406_080,
            "onChainSizeBytes": money_ops.position_onchain_size_bytes(69),
        }
        # Live probe (getMinimumBalanceForRentExemption on the built size).
        self.assertAlmostEqual(
            money_ops.position_rent_sol_for_bins(91, info), 0.07377600, places=8
        )
        self.assertAlmostEqual(
            money_ops.position_rent_sol_for_bins(140, info), 0.11197248, places=8
        )


class EstimateRentFollowsWidthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._half = range_state.half_width_bins
        self._pct = range_state.range_width_pct
        range_state.half_width_bins = 34
        range_state.range_width_pct = 1.37

    def tearDown(self) -> None:
        range_state.half_width_bins = self._half
        range_state.range_width_pct = self._pct

    def _estimate(self, width_pct: float | None) -> str:
        with (
            patch.object(bot_config, "MAX_POSITION_USD", None),
            patch("money_ops.meteora_ops.pool_info", return_value=_pool()),
            patch("money_ops.meteora_ops.balances", return_value=_bal()),
            patch("money_ops.suggest_for_budget", return_value=_suggest()),
            patch("money_ops.owner", return_value="Owner"),
            patch("money_ops.ops_kwargs", return_value={}),
        ):
            return money_ops.build_open_estimate(10.0, width_pct=width_pct)

    def test_wide_quote_uses_more_rent_than_narrow(self) -> None:
        narrow = self._estimate(None)  # saved 34 half → 69 bins
        wide = self._estimate(2.0)  # ~101 bins on binStep=4
        r_n = _rent_from_estimate(narrow)
        r_w = _rent_from_estimate(wide)
        self.assertNotEqual(r_n, r_w)
        self.assertGreater(r_w, r_n)
        half = range_state.pct_to_half_width_bins(2.0, 4)
        bins = range_state.corridor_bins(half)
        self.assertGreater(bins, 70)
        need = money_ops.position_rent_sol_for_bins(
            bins, _bal()["positionRentForDefaultOpen"]
        )
        self.assertGreaterEqual(r_w, round(need, 4) - 1e-9)


class SetrangeCeilingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._half = range_state.half_width_bins
        self._pct = range_state.range_width_pct
        range_state.half_width_bins = 10
        range_state.range_width_pct = 0.1

    async def asyncTearDown(self) -> None:
        range_state.half_width_bins = self._half
        range_state.range_width_pct = self._pct

    async def test_accepts_width_above_old_70_bin_cap(self) -> None:
        update, msg, ctx = _update_with_args(["1.5"])
        with (
            patch("meteora_ops.pool_info", return_value=_pool(bin_step=4, max_bins=1400)),
            patch("money_ops.get_primary_position", return_value=None),
            patch("money_ops.ops_kwargs", return_value={}),
            patch("range_width_state.save_range_width"),
        ):
            await tg.setrange_command(update, ctx)
        self.assertAlmostEqual(range_state.current_range_pct(), 1.5)
        self.assertIn("Диапазон:", "\n".join(msg.replies))

    async def test_refuses_past_position_max_length_with_real_reason(self) -> None:
        update, msg, ctx = _update_with_args(["8"])
        with (
            patch(
                "meteora_ops.pool_info",
                return_value=_pool(bin_step=1, max_bins=1400, price=100.0, active_id=0),
            ),
            patch("money_ops.get_primary_position", return_value=None),
            patch("money_ops.ops_kwargs", return_value={}),
            patch("range_width_state.save_range_width"),
        ):
            await tg.setrange_command(update, ctx)
        self.assertEqual(range_state.current_half_width(), 10)
        text = "\n".join(msg.replies)
        self.assertIn("больше максимума", text)
        self.assertIn("POSITION_MAX_LENGTH", text)
        self.assertIn("1400", text)
        self.assertNotIn("DEFAULT_BIN_PER_POSITION", text)
        self.assertIn("несколько транзакций", text)


if __name__ == "__main__":
    unittest.main()
