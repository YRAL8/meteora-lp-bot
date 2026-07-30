#!/usr/bin/env python3
"""Offline tests for C9: reopen_pending timing, stop, max gate, timer, SOL gate."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot_config  # noqa: E402
import bot_state  # noqa: E402
import main as main_mod  # noqa: E402
import money_ops  # noqa: E402
from meteora_exec import MeteoraExecError  # noqa: E402
from reopen_pending import is_reopen_pending, load_reopen_pending, set_reopen_pending  # noqa: E402


class ReopenPendingAfterClose(unittest.TestCase):
    def tearDown(self) -> None:
        set_reopen_pending(False)
        bot_state.bot_frozen = False

    def test_failed_close_does_not_set_pending(self) -> None:
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
                        side_effect=MeteoraExecError(
                            {"ok": False, "error": "build fail", "stage": "build"}
                        ),
                    ):
                        with self.assertRaises(MeteoraExecError):
                            money_ops.rebalance_position(pos, reply=replies.append)
        self.assertFalse(is_reopen_pending())
        self.assertTrue(any("на месте" in r for r in replies))

    def test_unknown_close_sets_pending(self) -> None:
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
                        side_effect=MeteoraExecError(
                            {
                                "ok": False,
                                "error": "unknown",
                                "stage": "confirm-unknown",
                                "confirmationUnknown": True,
                                "signatures": ["SigUnknown111"],
                            }
                        ),
                    ):
                        with self.assertRaises(MeteoraExecError):
                            money_ops.rebalance_position(pos, reply=replies.append)
        self.assertTrue(is_reopen_pending())
        self.assertTrue(any("НЕЯСЕН" in r for r in replies))

    def test_timeout_close_sets_pending_unknown(self) -> None:
        import subprocess

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
                        side_effect=subprocess.TimeoutExpired(
                            cmd="node exec.js exec-close", timeout=300
                        ),
                    ):
                        with self.assertRaises(subprocess.TimeoutExpired):
                            money_ops.rebalance_position(pos, reply=replies.append)
        self.assertTrue(is_reopen_pending())
        meta = load_reopen_pending() or {}
        self.assertEqual(meta.get("close_status"), "unknown")
        self.assertEqual(meta.get("error_type"), "TimeoutExpired")
        self.assertTrue(any("НЕЯСЕН" in r for r in replies))
        self.assertTrue(any("кошельке" in r for r in replies))

    def test_signed_exec_error_sets_pending_even_without_confirm_unknown(self) -> None:
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
                        side_effect=MeteoraExecError(
                            {
                                "ok": False,
                                "error": "rpc drop after send",
                                "stage": "confirm",
                                "sends": [
                                    {
                                        "signature": "SigSentButUnconfirmed222",
                                        "status": "pending",
                                    }
                                ],
                            }
                        ),
                    ):
                        with self.assertRaises(MeteoraExecError):
                            money_ops.rebalance_position(pos, reply=replies.append)
        self.assertTrue(is_reopen_pending())
        meta = load_reopen_pending() or {}
        self.assertEqual(meta.get("close_status"), "unknown")
        self.assertIn("SigSentButUnconfirmed222", meta.get("signatures") or [])
        self.assertTrue(any("НЕЯСЕН" in r for r in replies))


class StopMidRebalance(unittest.TestCase):
    def tearDown(self) -> None:
        set_reopen_pending(False)
        bot_state.bot_frozen = False

    def test_stop_after_close_skips_open(self) -> None:
        set_reopen_pending(False)
        pos = {"pubkey": "P", "sol": 0.1, "usdc": 1.0, "fees": {}}
        replies: list[str] = []

        def _close(*a, **k):
            bot_state.bot_frozen = True

        with patch.object(bot_config, "effective_network", return_value="devnet"):
            with patch.object(bot_config, "MAX_POSITION_USD", 10.0):
                with patch(
                    "money_ops.meteora_ops.pool_info",
                    return_value={"usdcPerSol": 100.0, "activeId": 0, "binStep": 1},
                ):
                    with patch("money_ops.close_position_full", side_effect=_close):
                        with patch("money_ops.open_with_budget") as op:
                            out = money_ops.rebalance_position(
                                pos, reply=replies.append
                            )
        self.assertIsNone(out)
        op.assert_not_called()
        self.assertTrue(is_reopen_pending())
        self.assertTrue(any("/stop" in r for r in replies))


class MainnetMaxGate(unittest.TestCase):
    def test_rebalance_refuses_without_cap(self) -> None:
        replies: list[str] = []
        pos = {"pubkey": "P", "sol": 0.1, "usdc": 1.0, "fees": {}}
        with patch.object(bot_config, "effective_network", return_value="mainnet"):
            with patch.object(bot_config, "MAX_POSITION_USD", None):
                with self.assertRaises(RuntimeError):
                    money_ops.rebalance_position(pos, reply=replies.append)
        self.assertTrue(any("MAX_POSITION_USD" in r for r in replies))


class TimerKeptOnFailure(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        main_mod.out_of_range_since = None
        main_mod.last_auto_attempt_at = None
        bot_state.bot_frozen = False
        if bot_state.money_lock.locked():
            bot_state.money_lock.release()

    async def test_failed_auto_keeps_out_of_range_and_pauses(self) -> None:
        import auto_rebalance_limits as ar_limits

        t0 = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
        pos = {
            "pubkey": "P",
            "lowerBinId": 1,
            "upperBinId": 3,
            "sol": 0.1,
            "usdc": 10.0,
            "fees": {},
        }
        pool = {
            "activeId": 99,
            "binStep": 1,
            "usdcPerSol": 100.0,
            "maxBinsPerPosition": 70,
        }
        since = t0 - timedelta(minutes=30)
        main_mod.out_of_range_since = since
        main_mod.last_auto_attempt_at = None
        lim = ar_limits.AutoRebalanceState(day_utc="2026-07-29", count_today=0)
        with (
            patch.object(bot_config, "AUTO_REBALANCE", True),
            patch.object(bot_config, "REBALANCE_DELAY_MIN", 20),
            patch.object(bot_config, "MIN_REBALANCE_INTERVAL_MIN", 0),
            patch.object(bot_config, "MAX_REBALANCES_PER_DAY", 6),
            patch.object(bot_config, "MAX_POSITION_USD", 50.0),
            patch.object(bot_config, "effective_network", return_value="devnet"),
            patch("main.is_reopen_pending", return_value=False),
            patch("main.money_ops.get_primary_position", return_value=pos),
            patch("main.meteora_ops.pool_info", return_value=pool),
            patch(
                "main.meteora_ops.balances",
                return_value={
                    "sol": {"ui": 1.0},
                    "usdc": {"ui": 10},
                    "solAvailableForOpen": 0.9,
                },
            ),
            patch("main.ar_limits.load_state", return_value=lim),
            patch("main.ar_limits.minutes_since_last", return_value=None),
            patch("main.money_ops.rebalance_position", return_value=None),
            patch("main.send_telegram_message"),
            patch("main.bot_config.wallet_pubkey", return_value="W"),
            patch("main._maybe_warn_uneconomic"),
        ):
            await main_mod.monitor_position(now=t0)
        self.assertEqual(main_mod.out_of_range_since, since)
        self.assertIsNotNone(main_mod.last_auto_attempt_at)

        # Immediate retry blocked by pause
        with (
            patch.object(bot_config, "AUTO_REBALANCE", True),
            patch.object(bot_config, "REBALANCE_DELAY_MIN", 20),
            patch.object(bot_config, "MIN_REBALANCE_INTERVAL_MIN", 0),
            patch.object(bot_config, "MAX_REBALANCES_PER_DAY", 6),
            patch.object(bot_config, "MAX_POSITION_USD", 50.0),
            patch.object(bot_config, "effective_network", return_value="devnet"),
            patch("main.is_reopen_pending", return_value=False),
            patch("main.money_ops.get_primary_position", return_value=pos),
            patch("main.meteora_ops.pool_info", return_value=pool),
            patch(
                "main.meteora_ops.balances",
                return_value={
                    "sol": {"ui": 1.0},
                    "usdc": {"ui": 10},
                    "solAvailableForOpen": 0.9,
                },
            ),
            patch("main.ar_limits.load_state", return_value=lim),
            patch("main.ar_limits.minutes_since_last", return_value=None),
            patch("main.money_ops.rebalance_position") as reb,
            patch("main.send_telegram_message"),
            patch("main.bot_config.wallet_pubkey", return_value="W"),
            patch("main._maybe_warn_uneconomic"),
        ):
            await main_mod.monitor_position(now=t0 + timedelta(minutes=5))
            reb.assert_not_called()

class SolGateUsesAvailable(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        main_mod.out_of_range_since = None
        main_mod.last_auto_attempt_at = None

    async def test_zero_wallet_sol_still_runs_when_position_can_swap(self) -> None:
        """C12: solAvailableForOpen=0 must not block if post-close legs can swap."""
        t0 = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
        main_mod.out_of_range_since = t0 - timedelta(minutes=30)
        main_mod.last_auto_attempt_at = None
        pos = {
            "pubkey": "P",
            "lowerBinId": 1,
            "upperBinId": 3,
            "sol": 0.1,
            "usdc": 10.0,
            "fees": {},
        }
        pool = {
            "activeId": 99,
            "binStep": 1,
            "usdcPerSol": 100.0,
            "maxBinsPerPosition": 70,
        }
        with (
            patch.object(bot_config, "AUTO_REBALANCE", True),
            patch.object(bot_config, "REBALANCE_DELAY_MIN", 20),
            patch.object(bot_config, "MIN_REBALANCE_INTERVAL_MIN", 0),
            patch.object(bot_config, "MAX_REBALANCES_PER_DAY", 6),
            patch.object(bot_config, "MAX_POSITION_USD", 50.0),
            patch.object(bot_config, "effective_network", return_value="devnet"),
            patch("main.is_reopen_pending", return_value=False),
            patch("main._check_unresolved_journal", return_value=False),
            patch("main.money_ops.get_primary_position", return_value=pos),
            patch("main.meteora_ops.pool_info", return_value=pool),
            patch(
                "main.meteora_ops.balances",
                return_value={
                    "sol": {"ui": 0.09},
                    "usdc": {"ui": 10},
                    "solAvailableForOpen": 0.0,
                },
            ),
            patch(
                "main.money_ops.suggest_for_budget",
                return_value={"needSol": 0.05, "needUsdc": 5.0},
            ),
            patch("main.ar_limits.load_state") as ls,
            patch("main.money_ops.rebalance_position", return_value={"ok": True}) as reb,
            patch("main.send_telegram_message"),
            patch("main.bot_config.wallet_pubkey", return_value="W"),
            patch("main._maybe_warn_uneconomic"),
            patch("main.ar_limits.record_rebalance"),
        ):
            lim = MagicMock()
            lim.count_today = 0
            lim.day_utc = "2026-07-29"
            lim.ensure_day = MagicMock()
            ls.return_value = lim
            with patch("main.ar_limits.minutes_since_last", return_value=None):
                await main_mod.monitor_position(now=t0)
            reb.assert_called_once()

    async def test_truly_short_budget_blocks(self) -> None:
        t0 = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
        main_mod.out_of_range_since = t0 - timedelta(minutes=30)
        main_mod.last_auto_attempt_at = None
        pos = {
            "pubkey": "P",
            "lowerBinId": 1,
            "upperBinId": 3,
            "sol": 0.0,
            "usdc": 0.01,
            "fees": {},
        }
        pool = {
            "activeId": 99,
            "binStep": 1,
            "usdcPerSol": 100.0,
            "maxBinsPerPosition": 70,
        }
        with (
            patch.object(bot_config, "AUTO_REBALANCE", True),
            patch.object(bot_config, "REBALANCE_DELAY_MIN", 20),
            patch.object(bot_config, "MIN_REBALANCE_INTERVAL_MIN", 0),
            patch.object(bot_config, "MAX_REBALANCES_PER_DAY", 6),
            patch.object(bot_config, "MAX_POSITION_USD", 50.0),
            patch.object(bot_config, "effective_network", return_value="devnet"),
            patch("main.is_reopen_pending", return_value=False),
            patch("main._check_unresolved_journal", return_value=False),
            patch("main.money_ops.get_primary_position", return_value=pos),
            patch("main.meteora_ops.pool_info", return_value=pool),
            patch(
                "main.meteora_ops.balances",
                return_value={
                    "sol": {"ui": 0.0},
                    "usdc": {"ui": 0.0},
                    "solAvailableForOpen": 0.0,
                },
            ),
            patch(
                "main.money_ops.suggest_for_budget",
                return_value={"needSol": 0.05, "needUsdc": 5.0},
            ),
            patch("main.ar_limits.load_state") as ls,
            patch("main.money_ops.rebalance_position") as reb,
            patch("main.send_telegram_message") as tg,
            patch("main.bot_config.wallet_pubkey", return_value="W"),
        ):
            lim = MagicMock()
            lim.count_today = 0
            lim.day_utc = "2026-07-29"
            lim.ensure_day = MagicMock()
            ls.return_value = lim
            with patch("main.ar_limits.minutes_since_last", return_value=None):
                await main_mod.monitor_position(now=t0)
            reb.assert_not_called()
            texts = " ".join(str(c.args[0]) for c in tg.call_args_list)
            self.assertTrue("своп" in texts.lower() or "бюджет" in texts.lower() or "SOL" in texts)


if __name__ == "__main__":
    unittest.main()
