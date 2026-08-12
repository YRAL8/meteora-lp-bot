#!/usr/bin/env python3
"""LITE_7: /claim fees + partial /withdraw without closing."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot_state  # noqa: E402
import money_ops  # noqa: E402
import telegram_commands as tg  # noqa: E402
from tests.test_lite_4 import _find_bad_angle  # noqa: E402


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


def _update(args: list[str]):
    msg = _FakeMessage()
    update = MagicMock()
    update.effective_message = msg
    ctx = MagicMock()
    ctx.args = args
    return update, msg, ctx


def _pos(
    *,
    sol: float = 0.1,
    usdc: float = 10.0,
    fee_sol: float = 0.001,
    fee_usdc: float = 0.25,
    pubkey: str = "Pos111",
) -> dict:
    return {
        "pubkey": pubkey,
        "sol": sol,
        "usdc": usdc,
        "lowerBinId": 100,
        "upperBinId": 140,
        "fees": {"sol": fee_sol, "usdc": fee_usdc},
    }


class MessageCopyTests(unittest.TestCase):
    def test_claim_and_partial_html_safe(self) -> None:
        pos = _pos(pubkey="Pos<hostile>&x")
        claim = money_ops.format_claim_estimate(pos, 100.0)
        partial = money_ops.format_partial_withdraw_estimate(pos, 25, 100.0)
        zero = money_ops.format_claim_estimate(
            _pos(fee_sol=0.0, fee_usdc=0.0, pubkey="Pos<z>"), 100.0
        )
        for text in (claim, partial, zero):
            bad = _find_bad_angle(text)
            self.assertIsNone(bad, f"bad angle {bad!r} in {text[:80]!r}")
            self.assertNotRegex(text, r"\$\s*-")


class ClaimCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        bot_state.bot_frozen = False

    async def test_claim_estimate_does_not_exec(self) -> None:
        update, msg, ctx = _update([])
        with (
            patch("money_ops.get_primary_position", return_value=_pos()),
            patch(
                "meteora_ops.pool_info",
                return_value={"usdcPerSol": 100.0},
            ),
            patch("money_ops.claim_fees") as mock_claim,
            patch("meteora_exec.exec_claim_fees") as mock_exec,
        ):
            await tg.claim_command(update, ctx)
            mock_claim.assert_not_called()
            mock_exec.assert_not_called()
        self.assertTrue(any("/claim confirm" in r for r in msg.replies))

    async def test_claim_confirm_calls_once(self) -> None:
        update, msg, ctx = _update(["confirm"])
        with (
            patch("money_ops.get_primary_position", return_value=_pos()),
            patch(
                "meteora_ops.pool_info",
                return_value={"usdcPerSol": 100.0},
            ),
            patch("money_ops.claim_fees", return_value={"ok": True}) as mock_claim,
        ):
            await tg.claim_command(update, ctx)
            mock_claim.assert_called_once()

    async def test_claim_zero_fees_does_not_exec(self) -> None:
        update, msg, ctx = _update(["confirm"])
        with (
            patch(
                "money_ops.get_primary_position",
                return_value=_pos(fee_sol=0.0, fee_usdc=0.0),
            ),
            patch(
                "meteora_ops.pool_info",
                return_value={"usdcPerSol": 100.0},
            ),
            patch("money_ops.claim_fees") as mock_claim,
            patch("meteora_exec.exec_claim_fees") as mock_exec,
        ):
            await tg.claim_command(update, ctx)
            mock_claim.assert_not_called()
            mock_exec.assert_not_called()
        self.assertTrue(any("нет" in r.lower() or "Комиссий" in r for r in msg.replies))

    async def test_claim_frozen_and_lock(self) -> None:
        bot_state.bot_frozen = True
        try:
            update, msg, ctx = _update(["confirm"])
            with patch("money_ops.claim_fees") as mock_claim:
                await tg.claim_command(update, ctx)
                mock_claim.assert_not_called()
            self.assertTrue(any("заморожен" in r for r in msg.replies))
        finally:
            bot_state.bot_frozen = False

        await bot_state.money_lock.acquire()
        try:
            update, msg, ctx = _update([])
            with patch("money_ops.claim_fees") as mock_claim:
                await tg.claim_command(update, ctx)
                mock_claim.assert_not_called()
            self.assertTrue(any("подожди" in r.lower() or "Идёт" in r for r in msg.replies))
        finally:
            bot_state.money_lock.release()


class WithdrawSpellingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        bot_state.bot_frozen = False

    async def test_spellings_matrix(self) -> None:
        """Each spelling: which money fn may fire (assert_not_called where none)."""
        # (args, expect_close, expect_partial)
        cases: list[tuple[list[str], bool, bool]] = [
            ([], False, False),
            (["confirm"], True, False),
            (["25"], False, False),
            (["25", "confirm"], False, True),
            (["0"], False, False),
            (["100"], False, False),
            (["-5"], False, False),
            (["25.5"], False, False),
            (["abc"], False, False),
            (["confirm", "25"], False, False),
        ]
        for args, expect_close, expect_partial in cases:
            with self.subTest(args=args):
                update, msg, ctx = _update(args)
                with (
                    patch("money_ops.get_primary_position", return_value=_pos()),
                    patch(
                        "meteora_ops.pool_info",
                        return_value={"usdcPerSol": 100.0, "activeId": 120, "binStep": 4},
                    ),
                    patch(
                        "money_ops.close_position_full", return_value=None
                    ) as mock_close,
                    patch(
                        "money_ops.withdraw_partial", return_value={"ok": True}
                    ) as mock_partial,
                    patch("meteora_exec.exec_withdraw") as mock_exec_w,
                    patch("meteora_exec.exec_close") as mock_exec_c,
                ):
                    await tg.withdraw_command(update, ctx)
                    if expect_close:
                        mock_close.assert_called_once()
                    else:
                        mock_close.assert_not_called()
                    if expect_partial:
                        mock_partial.assert_called_once()
                        self.assertEqual(mock_partial.call_args.args[1], 25)
                    else:
                        mock_partial.assert_not_called()
                    mock_exec_w.assert_not_called()
                    mock_exec_c.assert_not_called()

    async def test_withdraw_100_points_to_full_close(self) -> None:
        update, msg, ctx = _update(["100"])
        with (
            patch("money_ops.close_position_full") as mock_close,
            patch("money_ops.withdraw_partial") as mock_partial,
        ):
            await tg.withdraw_command(update, ctx)
            mock_close.assert_not_called()
            mock_partial.assert_not_called()
        self.assertTrue(any("/withdraw confirm" in r for r in msg.replies))
        self.assertTrue(any("100%" in r or "рент" in r.lower() for r in msg.replies))

    async def test_partial_frozen_and_lock(self) -> None:
        bot_state.bot_frozen = True
        try:
            update, msg, ctx = _update(["25", "confirm"])
            with patch("money_ops.withdraw_partial") as mock_partial:
                await tg.withdraw_command(update, ctx)
                mock_partial.assert_not_called()
            self.assertTrue(any("заморожен" in r for r in msg.replies))
        finally:
            bot_state.bot_frozen = False

        await bot_state.money_lock.acquire()
        try:
            update, msg, ctx = _update(["25"])
            with patch("money_ops.withdraw_partial") as mock_partial:
                await tg.withdraw_command(update, ctx)
                mock_partial.assert_not_called()
            self.assertTrue(any("подожди" in r.lower() or "Идёт" in r for r in msg.replies))
        finally:
            bot_state.money_lock.release()

    async def test_full_confirm_still_works_when_frozen(self) -> None:
        """Emergency /withdraw confirm must work under /stop."""
        bot_state.bot_frozen = True
        try:
            update, msg, ctx = _update(["confirm"])
            with (
                patch("money_ops.get_primary_position", return_value=_pos()),
                patch(
                    "meteora_ops.pool_info",
                    return_value={"usdcPerSol": 100.0},
                ),
                patch("money_ops.close_position_full") as mock_close,
                patch("reopen_pending.set_reopen_pending"),
            ):
                await tg.withdraw_command(update, ctx)
                mock_close.assert_called_once()
        finally:
            bot_state.bot_frozen = False


class BreakProbeTests(unittest.TestCase):
    """Prove the zero-fee guard test would catch a regression."""

    def test_zero_fee_guard_is_what_blocks_send(self) -> None:
        pos = _pos(fee_sol=0.0, fee_usdc=0.0)
        self.assertTrue(money_ops.fees_are_zero(pos))
        # If someone removed the guard in claim_command, confirm would call claim_fees.
        # We assert the predicate itself — the command test uses it for assert_not_called.
        pos2 = _pos(fee_sol=0.001, fee_usdc=0.0)
        self.assertFalse(money_ops.fees_are_zero(pos2))


if __name__ == "__main__":
    unittest.main()
