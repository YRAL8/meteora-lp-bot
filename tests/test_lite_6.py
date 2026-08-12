#!/usr/bin/env python3
"""LITE_6: /open shows estimate; confirm required to spend."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot_state  # noqa: E402
import money_ops  # noqa: E402
import range_state  # noqa: E402
import telegram_commands as tg  # noqa: E402
from tests.test_lite_4 import _find_bad_angle  # noqa: E402


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


def _bal(
    *,
    sol: float = 0.2,
    usdc: float = 6.0,
    fee: float = 0.02,
    rent_sol: float = 0.05740608,
    usdc_ata: bool = True,
    wsol_ata: bool = True,
) -> dict:
    return {
        "sol": {"ui": sol},
        "usdc": {"ui": usdc},
        "feeReserveSol": fee,
        "solAvailableForOpen": max(0.0, sol - fee - rent_sol),
        "positionRentForDefaultOpen": {"sol": rent_sol, "binCount": 69},
        "ata": {
            "usdc": {
                "exists": usdc_ata,
                "rentLamports": None if not usdc_ata else 2_039_280,
            },
            "wsol": {
                "exists": wsol_ata,
                "rentLamports": None if not wsol_ata else 2_039_280,
            },
        },
    }


def _pool(*, bin_step: int = 4, active_id: int = 0, price: float = 76.67) -> dict:
    return {
        "binStep": bin_step,
        "activeId": active_id,
        "usdcPerSol": price,
        "maxBinsPerPosition": 70,
    }


def _suggest(
    *,
    need_sol: float = 0.05,
    need_usdc: float = 6.0,
    price: float = 76.67,
    swap: dict | None = None,
) -> dict:
    return {
        "needSol": need_sol,
        "needUsdc": need_usdc,
        "params": {"usdcPerSol": price, "minBinId": -25, "maxBinId": 25},
        "swapSuggestion": swap,
    }


class OpenEstimateUnitTests(unittest.TestCase):
    def test_estimate_html_and_ata_and_swap(self) -> None:
        import bot_config

        with (
            patch.object(bot_config, "MAX_POSITION_USD", None),
            patch("money_ops.meteora_ops.pool_info", return_value=_pool()),
            patch(
                "money_ops.meteora_ops.balances",
                return_value=_bal(usdc_ata=False, wsol_ata=False),
            ),
            patch(
                "money_ops.suggest_for_budget",
                return_value=_suggest(
                    need_sol=0.12,
                    need_usdc=4.0,
                    swap={"side": "usdc-to-sol", "amount": 2.0},
                ),
            ),
            patch("money_ops.owner", return_value="Owner"),
            patch("money_ops.ops_kwargs", return_value={}),
            patch.object(range_state, "half_width_bins", 34),
            patch.object(range_state, "range_width_pct", 1.3),
        ):
            text = money_ops.build_open_estimate(10.0)

        self.assertIn("проверьте перед подтверждением", text)
        self.assertIn("вернётся при закрытии", text)
        self.assertIn("залог ATA", text)
        self.assertIn("Нужен обмен: 2.00 USDC → SOL", text)
        self.assertIn("/open 10 confirm", text)
        self.assertIn("в пул          $10.00", text)
        self.assertIsNone(
            _find_bad_angle(text), f"bad angle in: {_find_bad_angle(text)!r}"
        )

    def test_estimate_max_cap_announced(self) -> None:
        import bot_config

        with (
            patch.object(bot_config, "MAX_POSITION_USD", 5.0),
            patch("money_ops.meteora_ops.pool_info", return_value=_pool()),
            patch("money_ops.meteora_ops.balances", return_value=_bal()),
            patch("money_ops.suggest_for_budget", return_value=_suggest()) as sug,
            patch("money_ops.owner", return_value="Owner"),
            patch("money_ops.ops_kwargs", return_value={}),
            patch.object(range_state, "half_width_bins", 34),
            patch.object(range_state, "range_width_pct", 1.3),
        ):
            text = money_ops.build_open_estimate(10.0)
            # Cap applied before suggest.
            sug.assert_called_once()
            self.assertAlmostEqual(sug.call_args.args[0], 5.0)

        self.assertIn("MAX_POSITION_USD", text)
        self.assertIn("$5.00", text)

    def test_estimate_shortfall_no_confirm(self) -> None:
        with (
            patch("money_ops.meteora_ops.pool_info", return_value=_pool(price=100.0)),
            patch(
                "money_ops.meteora_ops.balances",
                return_value=_bal(sol=0.05, usdc=1.0, fee=0.02, rent_sol=0.057),
            ),
            patch(
                "money_ops.suggest_for_budget",
                return_value=_suggest(
                    need_sol=0.08, need_usdc=5.0, price=100.0, swap=None
                ),
            ),
            patch("money_ops.owner", return_value="Owner"),
            patch("money_ops.ops_kwargs", return_value={}),
            patch.object(range_state, "half_width_bins", 10),
            patch.object(range_state, "range_width_pct", 0.4),
        ):
            text = money_ops.build_open_estimate(10.0)

        # Смысл, а не написание: слово может стоять внутри предложения.
        self.assertIn("не хватает", text.lower())
        self.assertNotIn("Подтвердить:", text)


class OpenCommandLite6Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        bot_state.bot_frozen = False

    async def test_open_10_does_not_call_money(self) -> None:
        update, msg, ctx = _update_with_args(["10"])
        with (
            patch("money_ops.open_with_budget") as mock_open,
            patch("money_ops.get_primary_position", return_value=None),
            patch(
                "money_ops.build_open_estimate",
                return_value="🆕 смета\nПодтвердить: /open 10 confirm",
            ) as est,
        ):
            await tg.open_command(update, ctx)
            mock_open.assert_not_called()
            est.assert_called_once()
            self.assertAlmostEqual(est.call_args.args[0], 10.0)
            self.assertIsNone(est.call_args.kwargs.get("width_pct"))
        self.assertTrue(any("confirm" in r.lower() or "смета" in r.lower() for r in msg.replies))

    async def test_open_10_confirm_calls_money_once(self) -> None:
        update, msg, ctx = _update_with_args(["10", "confirm"])
        with (
            patch("money_ops.open_with_budget") as mock_open,
            patch("money_ops.get_primary_position", return_value=None),
            patch("money_ops.build_open_estimate") as est,
        ):
            mock_open.return_value = {"ok": True}
            await tg.open_command(update, ctx)
            mock_open.assert_called_once()
            est.assert_not_called()
            self.assertAlmostEqual(mock_open.call_args.args[0], 10.0)
            self.assertIsNone(mock_open.call_args.kwargs.get("half_width"))

    async def test_open_width_confirm_passes_half_not_saved(self) -> None:
        prev_half = range_state.half_width_bins
        prev_pct = range_state.range_width_pct
        range_state.half_width_bins = 34
        range_state.range_width_pct = 1.3
        try:
            update, msg, ctx = _update_with_args(["10", "0.8", "confirm"])
            with (
                patch("money_ops.open_with_budget") as mock_open,
                patch("money_ops.get_primary_position", return_value=None),
                patch(
                    "meteora_ops.pool_info",
                    return_value=_pool(bin_step=4),
                ),
            ):
                mock_open.return_value = {"ok": True}
                await tg.open_command(update, ctx)
                mock_open.assert_called_once()
                half = mock_open.call_args.kwargs.get("half_width")
                expected = range_state.half_width_for_pct(
                    0.8, 4, max_bins_per_position=70
                )
                self.assertEqual(half, expected)
                self.assertNotEqual(half, 34)
            self.assertEqual(range_state.half_width_bins, 34)
            self.assertEqual(range_state.range_width_pct, 1.3)
        finally:
            range_state.half_width_bins = prev_half
            range_state.range_width_pct = prev_pct

    async def test_open_width_estimate_does_not_mutate_range_state(self) -> None:
        prev_half = range_state.half_width_bins
        prev_pct = range_state.range_width_pct
        range_state.half_width_bins = 34
        range_state.range_width_pct = 1.3
        try:
            update, msg, ctx = _update_with_args(["10", "0.8"])
            with (
                patch("money_ops.open_with_budget") as mock_open,
                patch("money_ops.get_primary_position", return_value=None),
                patch(
                    "money_ops.build_open_estimate",
                    return_value="смета width",
                ) as est,
            ):
                await tg.open_command(update, ctx)
                mock_open.assert_not_called()
                self.assertEqual(est.call_args.kwargs.get("width_pct"), 0.8)
            self.assertEqual(range_state.half_width_bins, 34)
            self.assertEqual(range_state.range_width_pct, 1.3)
        finally:
            range_state.half_width_bins = prev_half
            range_state.range_width_pct = prev_pct

    async def test_spellings_matrix(self) -> None:
        """Each spelling: assert money open called or not."""
        cases: list[tuple[list[str], bool]] = [
            ([], False),
            (["10"], False),
            (["10", "confirm"], True),
            (["10", "0.8"], False),
            (["10", "0.8", "confirm"], True),
            (["10", "confirm", "0.8"], False),
            (["0"], False),
            (["-5"], False),
            (["10", "99"], False),
            (["10", "abc"], False),
            (["abc"], False),
        ]
        for args, should_call in cases:
            with self.subTest(args=args, should_call=should_call):
                update, msg, ctx = _update_with_args(args)
                with (
                    patch("money_ops.open_with_budget") as mock_open,
                    patch("money_ops.get_primary_position", return_value=None),
                    patch(
                        "money_ops.build_open_estimate",
                        return_value="смета ok",
                    ),
                    patch(
                        "meteora_ops.pool_info",
                        return_value=_pool(bin_step=4),
                    ),
                ):
                    # binStep=4 → max ≈±1.37%; 0.8% OK; 99% rejected by MIN/MAX parse.
                    mock_open.return_value = {"ok": True}
                    await tg.open_command(update, ctx)
                    if should_call:
                        mock_open.assert_called_once()
                    else:
                        mock_open.assert_not_called()

    async def test_wide_width_refuses_without_open(self) -> None:
        # 5% is within MIN/MAX_RANGE_PCT but exceeds binStep=1 pool max (~0.34%).
        update, msg, ctx = _update_with_args(["10", "5"])
        with (
            patch("money_ops.open_with_budget") as mock_open,
            patch("money_ops.get_primary_position", return_value=None),
            patch(
                "money_ops.build_open_estimate",
                side_effect=range_state.RangeTooWideError(
                    requested_pct=5.0,
                    max_pct=0.3406,
                    max_half_width=34,
                    max_bins_per_position=70,
                    bin_step=1,
                ),
            ),
        ):
            await tg.open_command(update, ctx)
            mock_open.assert_not_called()
        self.assertTrue(any("максимума" in r or "больше" in r for r in msg.replies))

    async def test_shortfall_estimate_does_not_open(self) -> None:
        update, msg, ctx = _update_with_args(["10"])
        text = (
            "🆕 смета\n❌ Не хватает средств: 0.0500 SOL (~$5.00). "
            "Дошли и снова /open — смета ничего не отправляет."
        )
        with (
            patch("money_ops.open_with_budget") as mock_open,
            patch("money_ops.get_primary_position", return_value=None),
            patch("money_ops.build_open_estimate", return_value=text),
        ):
            await tg.open_command(update, ctx)
            mock_open.assert_not_called()
        self.assertTrue(any("Не хватает" in r for r in msg.replies))


if __name__ == "__main__":
    unittest.main()
