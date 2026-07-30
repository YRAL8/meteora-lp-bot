#!/usr/bin/env python3
"""Offline tests for C10 — real fail() shapes, stage-based not-sent, swap block."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot_config  # noqa: E402
import bot_state  # noqa: E402
import money_ops  # noqa: E402
from meteora_exec import MeteoraExecError  # noqa: E402
from reopen_pending import is_reopen_pending, load_reopen_pending, set_reopen_pending  # noqa: E402


# Exact shapes printed by ts fail() after C10 (see build_lib.fail / exec.sendBuildResult).
FAIL_SEND_NO_SIGS_LEGACY = {
    "ok": False,
    "action": "exec-close",
    "error": "send failed tx[0]: fetch failed",
    "stage": "send",
}
FAIL_SEND_WITH_SIGS = {
    "ok": False,
    "action": "exec-close",
    "error": "send failed tx[0]: fetch failed",
    "stage": "send",
    "confirmationUnknown": True,
    "signatures": ["SigSend111"],
    "sends": [
        {
            "index": 0,
            "signature": "SigSend111",
            "status": "unknown",
            "slot": None,
            "error": "fetch failed",
        }
    ],
}
FAIL_BUILD = {
    "ok": False,
    "action": "exec-close",
    "error": "build exploded",
    "stage": "build",
}
FAIL_CONFIRM_ONCHAIN = {
    "ok": False,
    "action": "exec-close",
    "error": "tx[0] failed on-chain: InstructionError",
    "stage": "confirm",
    "signatures": ["SigFail222"],
    "sends": [
        {"index": 0, "signature": "SigFail222", "status": "failed", "error": "x"}
    ],
}


class ProvenNotSentStageTests(unittest.TestCase):
    def test_send_stage_without_signatures_is_unknown(self) -> None:
        err = MeteoraExecError(FAIL_SEND_NO_SIGS_LEGACY)
        self.assertFalse(money_ops._exec_proven_not_sent(err))

    def test_build_stage_without_signatures_is_clean(self) -> None:
        err = MeteoraExecError(FAIL_BUILD)
        self.assertTrue(money_ops._exec_proven_not_sent(err))

    def test_send_with_signatures_is_unknown(self) -> None:
        err = MeteoraExecError(FAIL_SEND_WITH_SIGS)
        self.assertFalse(money_ops._exec_proven_not_sent(err))
        self.assertEqual(
            money_ops._signatures_from_exec_error(err), ["SigSend111"]
        )


class RebalanceFailShapes(unittest.TestCase):
    def tearDown(self) -> None:
        set_reopen_pending(False)
        bot_state.bot_frozen = False

    def _run_close_error(self, payload: dict) -> list[str]:
        set_reopen_pending(False)
        pos = {"pubkey": "P", "sol": 0.1, "usdc": 1.0, "fees": {}}
        replies: list[str] = []
        with patch.object(bot_config, "effective_network", return_value="devnet"):
            with patch.object(bot_config, "MAX_POSITION_USD", 10.0):
                with patch(
                    "money_ops.meteora_ops.pool_info",
                    return_value={"usdcPerSol": 100.0, "activeId": 0, "binStep": 1},
                ):
                    with patch(
                        "money_ops.close_position_full",
                        side_effect=MeteoraExecError(payload),
                    ):
                        with self.assertRaises(MeteoraExecError):
                            money_ops.rebalance_position(pos, reply=replies.append)
        return replies

    def test_legacy_send_fail_sets_pending(self) -> None:
        replies = self._run_close_error(FAIL_SEND_NO_SIGS_LEGACY)
        self.assertTrue(is_reopen_pending())
        meta = load_reopen_pending() or {}
        self.assertEqual(meta.get("close_status"), "unknown")
        self.assertTrue(any("НЕЯСЕН" in r for r in replies))

    def test_build_fail_does_not_set_pending(self) -> None:
        replies = self._run_close_error(FAIL_BUILD)
        self.assertFalse(is_reopen_pending())
        self.assertTrue(any("на месте" in r for r in replies))

    def test_confirm_failed_with_sigs_sets_pending(self) -> None:
        replies = self._run_close_error(FAIL_CONFIRM_ONCHAIN)
        self.assertTrue(is_reopen_pending())
        meta = load_reopen_pending() or {}
        self.assertIn("SigFail222", meta.get("signatures") or [])


class SwapUnknownBlocks(unittest.TestCase):
    def test_swap_unknown_raises(self) -> None:
        replies: list[str] = []
        with patch(
            "money_ops.meteora_exec.exec_swap",
            side_effect=MeteoraExecError(FAIL_SEND_WITH_SIGS),
        ):
            with self.assertRaises(money_ops.SwapOutcomeUnknown):
                money_ops.apply_swap_suggestion(
                    {"side": "sol-to-usdc", "amount": 0.01},
                    replies.append,
                )
        self.assertTrue(any("НЕЯСЕН" in r for r in replies))

    def test_swap_build_fail_returns_false(self) -> None:
        replies: list[str] = []
        with patch(
            "money_ops.meteora_exec.exec_swap",
            side_effect=MeteoraExecError(FAIL_BUILD),
        ):
            ok = money_ops.apply_swap_suggestion(
                {"side": "sol-to-usdc", "amount": 0.01},
                replies.append,
            )
        self.assertFalse(ok)


class WithdrawClearsFlagOnlyAfterClose(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        set_reopen_pending(False)
        bot_state.bot_frozen = False
        if bot_state.money_lock.locked():
            bot_state.money_lock.release()

    async def test_busy_lock_keeps_pending(self) -> None:
        import telegram_commands as tg
        from tests.test_telegram_commands import _update_with_args

        set_reopen_pending(True, meta={"close_status": "confirmed"})
        await bot_state.money_lock.acquire()
        try:
            with patch(
                "telegram_commands.money_ops.get_primary_position",
                return_value={"pubkey": "P", "lowerBinId": 1, "upperBinId": 3},
            ):
                with patch(
                    "telegram_commands.meteora_ops.pool_info",
                    return_value={"usdcPerSol": 100.0},
                ):
                    with patch.object(tg, "WITHDRAW_LOCK_WAIT_SEC", 0.15):
                        update, msg, ctx = _update_with_args(["confirm"])
                        await tg.withdraw_command(update, ctx)
            self.assertTrue(is_reopen_pending())
            self.assertTrue(
                any("Не дождался" in r or "замка" in r for r in msg.replies)
            )
        finally:
            bot_state.money_lock.release()


class StopBeforeClose(unittest.TestCase):
    def tearDown(self) -> None:
        set_reopen_pending(False)
        bot_state.bot_frozen = False

    def test_frozen_skips_close(self) -> None:
        bot_state.bot_frozen = True
        pos = {"pubkey": "P", "sol": 0.1, "usdc": 1.0, "fees": {}}
        replies: list[str] = []
        with patch.object(bot_config, "effective_network", return_value="devnet"):
            with patch.object(bot_config, "MAX_POSITION_USD", 10.0):
                with patch("money_ops.close_position_full") as cl:
                    with self.assertRaises(money_ops.StopRequested):
                        money_ops.rebalance_position(pos, reply=replies.append)
                    cl.assert_not_called()
        self.assertFalse(is_reopen_pending())


class UndersizedOpen(unittest.TestCase):
    def test_undersized_raises(self) -> None:
        replies: list[str] = []
        with patch.object(bot_config, "MAX_POSITION_USD", None):
            with patch(
                "money_ops.meteora_ops.pool_info",
                return_value={
                    "usdcPerSol": 100.0,
                    "activeId": 0,
                    "binStep": 1,
                    "maxBinsPerPosition": 70,
                },
            ):
                with patch(
                    "money_ops.suggest_for_budget",
                    return_value={
                        "needSol": 0.05,
                        "needUsdc": 5.0,
                        "params": {
                            "minBinId": 1,
                            "maxBinId": 3,
                            "usdcPerSol": 100.0,
                        },
                        "swapSuggestion": None,
                    },
                ):
                    with patch(
                        "money_ops._wallet_balances",
                        return_value=(0.1, 0.2, 0.001),
                    ):
                        with self.assertRaises(money_ops.OpenMateriallyUndersized):
                            money_ops.open_with_budget(5.0, reply=replies.append)


class MonitorTimersPersist(unittest.TestCase):
    def test_roundtrip(self) -> None:
        import monitor_timer_state as mts
        from datetime import datetime, timezone

        t0 = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 7, 29, 12, 30, tzinfo=timezone.utc)
        path = mts.STATE_PATH
        bak = path.read_text(encoding="utf-8") if path.is_file() else None
        try:
            mts.save_timers(t0, t1)
            a, b = mts.load_timers()
            self.assertEqual(a, t0)
            self.assertEqual(b, t1)
        finally:
            if bak is None:
                if path.is_file():
                    path.unlink()
            else:
                path.write_text(bak, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
