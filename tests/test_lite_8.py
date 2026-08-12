#!/usr/bin/env python3
"""LITE_8 display fixes: corridor bins, actual %, shortfall copy, setrange footnote."""
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
from tests.test_lite_4 import _find_bad_angle  # noqa: E402


def _pool(*, bin_step: int = 4, active_id: int = -6422, price: float = 76.67) -> dict:
    return {
        "binStep": bin_step,
        "activeId": active_id,
        "usdcPerSol": price,
        "maxBinsPerPosition": 70,
    }


def _bal(
    *,
    sol: float = 0.2,
    usdc: float = 6.0,
    fee: float = 0.02,
    rent_sol: float = 0.05740608,
) -> dict:
    return {
        "sol": {"ui": sol},
        "usdc": {"ui": usdc},
        "feeReserveSol": fee,
        "solAvailableForOpen": max(0.0, sol - fee - rent_sol),
        "positionRentForDefaultOpen": {"sol": rent_sol, "binCount": 69},
        "ata": {
            "usdc": {"exists": True, "rentLamports": 2_039_280},
            "wsol": {"exists": True, "rentLamports": 2_039_280},
        },
    }


class CorridorAndAskedPctTests(unittest.TestCase):
    def test_status_and_estimate_same_corridor(self) -> None:
        range_state.half_width_bins = 32
        range_state.range_width_pct = 1.3
        bin_step = 4
        half = range_state.current_half_width()
        corridor = range_state.corridor_bins(half)
        actual = range_state.pct_from_half(half, bin_step)
        asked = range_state.asked_pct_note(actual, 1.3)
        status_line = (
            f"<i>Новые позиции: ±{actual:.2f}%{asked} ({corridor} ячеек)</i>"
        )
        with (
            patch.object(bot_config, "MAX_POSITION_USD", None),
            patch("money_ops.meteora_ops.pool_info", return_value=_pool()),
            patch("money_ops.meteora_ops.balances", return_value=_bal()),
            patch(
                "money_ops.suggest_for_budget",
                return_value={
                    "needSol": 0.12,
                    "needUsdc": 4.0,
                    "params": {"usdcPerSol": 76.67},
                    "swapSuggestion": {"side": "usdc-to-sol", "amount": 2.0},
                },
            ),
            patch("money_ops.owner", return_value="Owner"),
            patch("money_ops.ops_kwargs", return_value={}),
        ):
            est = money_ops.build_open_estimate(10.0)
        self.assertIn(f"({corridor} ячеек)", status_line)
        self.assertIn(f"{corridor} ячеек", est)
        self.assertNotIn("(32 ячеек)", status_line)
        self.assertIn("просили 1.3%", status_line)
        self.assertIn("просили 1.3%", est)
        self.assertIsNone(_find_bad_angle(status_line))
        self.assertIsNone(_find_bad_angle(est))

    def test_asked_note_only_when_different(self) -> None:
        actual = range_state.pct_from_half(20, 4)  # ≈0.80%
        self.assertEqual(range_state.asked_pct_note(actual, 0.8), "")
        self.assertIn("просили", range_state.asked_pct_note(actual, 0.85))


class ShortfallCopyTests(unittest.TestCase):
    def test_no_negative_free_no_confirm(self) -> None:
        range_state.half_width_bins = 32
        range_state.range_width_pct = 1.3
        with (
            patch.object(bot_config, "MAX_POSITION_USD", None),
            patch("money_ops.meteora_ops.pool_info", return_value=_pool()),
            patch(
                "money_ops.meteora_ops.balances",
                return_value=_bal(sol=0.0494, usdc=1.0),
            ),
            patch(
                "money_ops.suggest_for_budget",
                return_value={
                    "needSol": 0.0652,
                    "needUsdc": 5.0,
                    "params": {"usdcPerSol": 76.67},
                    "swapSuggestion": None,
                },
            ),
            patch("money_ops.owner", return_value="Owner"),
            patch("money_ops.ops_kwargs", return_value={}),
        ):
            text = money_ops.build_open_estimate(10.0)
        self.assertIn("не хватает", text.lower())
        self.assertNotIn("Подтвердить:", text)
        self.assertNotRegex(text, r"свободно после\s*\$-")
        # No negative dollar amounts anywhere in the message.
        for m in re.finditer(r"\$\s*-", text):
            self.fail(f"negative dollar amount in estimate: {text[m.start():m.start()+20]!r}")
        self.assertIn("запрошено в пул", text)
        # SOL here (0.0494) does not even cover reserves (0.0774), so nothing is
        # openable — USDC alone must not be advertised as an openable amount.
        self.assertIn("Открыть нельзя ничего", text)
        self.assertNotIn("Сейчас хватит на", text)
        self.assertIsNone(_find_bad_angle(text))

    def test_affordable_shown_when_sol_covers_reserves(self) -> None:
        """The other side of the same branch: real headroom → real number."""
        with (
            patch.object(bot_config, "MAX_POSITION_USD", None),
            patch("money_ops.meteora_ops.pool_info", return_value=_pool()),
            patch(
                "money_ops.meteora_ops.balances",
                return_value=_bal(sol=0.20, usdc=1.0),
            ),
            patch(
                "money_ops.suggest_for_budget",
                return_value={
                    "needSol": 0.0652,
                    "needUsdc": 5.0,
                    "params": {"usdcPerSol": 76.67},
                    "swapSuggestion": None,
                },
            ),
            patch("money_ops.owner", return_value="Owner"),
            patch("money_ops.ops_kwargs", return_value={}),
        ):
            text = money_ops.build_open_estimate(10.0)
        self.assertIn("Не хватает", text)
        self.assertIn("Сейчас хватит на", text)
        self.assertNotIn("Открыть нельзя ничего", text)
        self.assertIsNone(_find_bad_angle(text))


class SetrangeCopyTests(unittest.IsolatedAsyncioTestCase):
    async def test_setrange_human_first_footnote_last(self) -> None:
        class Msg:
            def __init__(self) -> None:
                self.replies: list[str] = []

            async def reply_text(self, text: str, **kwargs) -> None:
                self.replies.append(text)

        msg = Msg()
        update = MagicMock()
        update.effective_message = msg
        ctx = MagicMock()
        ctx.args = ["0.8"]
        range_state.half_width_bins = 32
        range_state.range_width_pct = 1.3
        with (
            patch(
                "meteora_ops.pool_info",
                return_value=_pool(active_id=-6422),
            ),
            patch("money_ops.get_primary_position", return_value=None),
            patch("money_ops.ops_kwargs", return_value={}),
            patch("range_width_state.save_range_width"),
        ):
            await tg.setrange_command(update, ctx)
        text = msg.replies[0]
        self.assertIn("Диапазон:", text)
        self.assertIn("от $", text)
        self.assertIn("<i>", text)
        self.assertIn("binStep=", text)
        self.assertNotIn("действует до перезапуска", text)
        self.assertNotIn("half_width_bins", text)
        self.assertIsNone(_find_bad_angle(text))


class RangePersistTests(unittest.TestCase):
    def test_save_restore_roundtrip(self) -> None:
        import os
        import tempfile

        import range_width_state

        prev_half = range_state.half_width_bins
        prev_pct = range_state.range_width_pct
        with tempfile.TemporaryDirectory() as td:
            os.environ["METEORA_STATE_DIR"] = td
            # Re-bind path after env change (module holds STATE_PATH at import).
            range_width_state.STATE_PATH = __import__(
                "state_paths"
            ).path("range_width.json")
            range_state.apply_setrange(0.8, 4, max_bins_per_position=70)
            self.assertTrue(range_width_state.STATE_PATH.is_file())
            range_state.half_width_bins = 34
            range_state.range_width_pct = 5.0
            result = range_width_state.restore_at_startup(
                bin_step=4, max_bins_per_position=70
            )
            self.assertEqual(result.source, "state")
            self.assertIsNone(result.warning)
            self.assertAlmostEqual(result.pct, 0.8)
            self.assertEqual(result.half, range_state.pct_to_half_width_bins(0.8, 4))
            self.assertEqual(range_state.current_range_pct(), 0.8)
        range_state.half_width_bins = prev_half
        range_state.range_width_pct = prev_pct

    def test_missing_file_keeps_env(self) -> None:
        import os
        import tempfile

        import range_width_state

        prev_half = range_state.half_width_bins
        prev_pct = range_state.range_width_pct
        range_state.half_width_bins = 34
        range_state.range_width_pct = 5.0
        with tempfile.TemporaryDirectory() as td:
            os.environ["METEORA_STATE_DIR"] = td
            range_width_state.STATE_PATH = __import__("state_paths").path(
                "range_width.json"
            )
            result = range_width_state.restore_at_startup(
                bin_step=4, max_bins_per_position=70
            )
            self.assertEqual(result.source, "env")
            self.assertIsNone(result.warning)
            self.assertEqual(range_state.current_half_width(), 34)
            self.assertAlmostEqual(range_state.current_range_pct(), 5.0)
        range_state.half_width_bins = prev_half
        range_state.range_width_pct = prev_pct

    def test_corrupt_file_falls_back_and_warns(self) -> None:
        import os
        import tempfile

        import range_width_state

        prev_half = range_state.half_width_bins
        prev_pct = range_state.range_width_pct
        range_state.half_width_bins = 34
        range_state.range_width_pct = 5.0
        with tempfile.TemporaryDirectory() as td:
            os.environ["METEORA_STATE_DIR"] = td
            range_width_state.STATE_PATH = __import__("state_paths").path(
                "range_width.json"
            )
            range_width_state.STATE_PATH.write_text("{not-json", encoding="utf-8")
            result = range_width_state.restore_at_startup(
                bin_step=4, max_bins_per_position=70
            )
            self.assertEqual(result.source, "env")
            self.assertIsNotNone(result.warning)
            self.assertIn(".env", result.warning or "")
            self.assertEqual(range_state.current_half_width(), 34)
            self.assertIsNone(_find_bad_angle(f"⚠️ {result.warning}"))
        range_state.half_width_bins = prev_half
        range_state.range_width_pct = prev_pct

    def test_too_wide_for_pool_falls_back_and_warns(self) -> None:
        import os
        import tempfile

        import range_width_state

        prev_half = range_state.half_width_bins
        prev_pct = range_state.range_width_pct
        range_state.half_width_bins = 34
        range_state.range_width_pct = 5.0
        with tempfile.TemporaryDirectory() as td:
            os.environ["METEORA_STATE_DIR"] = td
            range_width_state.STATE_PATH = __import__("state_paths").path(
                "range_width.json"
            )
            # 5% is fine for binStep=4 but not for binStep=1 (~0.34% max).
            range_width_state.save_range_width(5.0, 34)
            result = range_width_state.restore_at_startup(
                bin_step=1, max_bins_per_position=70
            )
            self.assertEqual(result.source, "env")
            self.assertIsNotNone(result.warning)
            self.assertIn(".env", result.warning or "")
            self.assertEqual(range_state.current_half_width(), 34)
        range_state.half_width_bins = prev_half
        range_state.range_width_pct = prev_pct


if __name__ == "__main__":
    unittest.main()
