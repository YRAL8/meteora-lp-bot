#!/usr/bin/env python3
"""Offline tests for C4 auto-rebalance discipline and storm guards."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import auto_rebalance_limits as ar_limits  # noqa: E402
import bot_config  # noqa: E402
import bot_state  # noqa: E402
import main as main_mod  # noqa: E402
import money_ops  # noqa: E402


def _pos(*, lo: int = 100, hi: int = 110, sol: float = 0.1, usdc: float = 5.0) -> dict:
    return {
        "pubkey": "PosTest111",
        "lowerBinId": lo,
        "upperBinId": hi,
        "sol": sol,
        "usdc": usdc,
        "fees": {"sol": 0, "usdc": 0},
        "raw": {"totalXAmount": "1", "totalYAmount": "1"},
    }


def _pool(*, active: int = 105, price: float = 100.0, bin_step: int = 1) -> dict:
    return {
        "activeId": active,
        "usdcPerSol": price,
        "binStep": bin_step,
        "maxBinsPerPosition": 70,
    }


class AutoRebalanceMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        main_mod.out_of_range_since = None
        main_mod._reset_rebalance_blocked_state()
        bot_state.bot_paused = False
        bot_state.bot_frozen = False
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "auto_rebalance.json"
        self.patches = [
            patch.object(ar_limits, "STATE_PATH", self.state_path),
            patch.object(bot_config, "AUTO_REBALANCE", True),
            patch.object(bot_config, "REBALANCE_DELAY_MIN", 20),
            patch.object(bot_config, "MIN_REBALANCE_INTERVAL_MIN", 60),
            patch.object(bot_config, "MAX_REBALANCES_PER_DAY", 6),
            patch.object(bot_config, "REBALANCE_BLOCKED_REMINDER_HOURS", 1),
            patch.object(bot_config, "MIN_SOL_BALANCE", 0.05),
            patch.object(bot_config, "REBALANCE_PAYBACK_HOURS", 2.8),
            patch.object(bot_config, "UNECONOMIC_LOOKBACK_CYCLES", 5),
            patch.object(bot_config, "POLL_INTERVAL_SEC", 60),
            patch("main.bot_config.wallet_pubkey", return_value="Owner111"),
            patch("main.money_ops.ops_kwargs", return_value={"pool": "P", "rpc": "R"}),
            patch("main.is_reopen_pending", return_value=False),
            patch("main.send_telegram_message"),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()
        main_mod.out_of_range_since = None
        main_mod._reset_rebalance_blocked_state()

    async def test_delay_prevents_immediate_rebalance(self) -> None:
        t0 = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        with patch("main.meteora_ops.pool_info", return_value=_pool(active=200)):
            with patch("main.money_ops.get_primary_position", return_value=_pos()):
                with patch("main.money_ops.rebalance_position") as reb:
                    await main_mod.monitor_position(now=t0)
                    reb.assert_not_called()
                    self.assertIsNotNone(main_mod.out_of_range_since)
                    # 10 min later — still waiting
                    await main_mod.monitor_position(now=t0 + timedelta(minutes=10))
                    reb.assert_not_called()

    async def test_return_before_delay_cancels(self) -> None:
        t0 = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        with patch("main.meteora_ops.pool_info", return_value=_pool(active=200)):
            with patch("main.money_ops.get_primary_position", return_value=_pos()):
                await main_mod.monitor_position(now=t0)
        self.assertIsNotNone(main_mod.out_of_range_since)
        with patch("main.meteora_ops.pool_info", return_value=_pool(active=105)):
            with patch("main.money_ops.get_primary_position", return_value=_pos()):
                with patch("main.money_ops.rebalance_position") as reb:
                    await main_mod.monitor_position(now=t0 + timedelta(minutes=5))
                    reb.assert_not_called()
                    self.assertIsNone(main_mod.out_of_range_since)

    async def test_auto_false_repeats_reminder(self) -> None:
        patch.object(bot_config, "AUTO_REBALANCE", False).start()
        t0 = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        with patch("main.meteora_ops.pool_info", return_value=_pool(active=200)):
            with patch("main.money_ops.get_primary_position", return_value=_pos()):
                with patch("main.send_telegram_message") as tg:
                    await main_mod.monitor_position(now=t0)
                    # past delay
                    await main_mod.monitor_position(now=t0 + timedelta(minutes=25))
                    first_wave = tg.call_count
                    self.assertGreaterEqual(first_wave, 2)  # out-of-range + manual needed
                    # same hour — no new blocked reminder
                    await main_mod.monitor_position(now=t0 + timedelta(minutes=30))
                    mid = tg.call_count
                    self.assertEqual(mid, first_wave)
                    # after reminder window — another manual reminder
                    await main_mod.monitor_position(now=t0 + timedelta(hours=2))
                    self.assertGreater(tg.call_count, mid)
                    texts = " ".join(str(c.args[0]) for c in tg.call_args_list)
                    self.assertIn("ручной", texts.lower())

    async def test_min_interval_blocks_second(self) -> None:
        t0 = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        st = ar_limits.AutoRebalanceState(day_utc="2026-07-29", count_today=1)
        ar_limits.record_rebalance(st, t0)  # sets last_rebalance_at=t0, count=2
        # Reset count for clarity — we care about interval
        st = ar_limits.load_state()
        st.count_today = 1
        ar_limits.save_state(st)

        main_mod.out_of_range_since = t0 - timedelta(minutes=30)
        with patch("main.meteora_ops.pool_info", return_value=_pool(active=200)):
            with patch("main.money_ops.get_primary_position", return_value=_pos()):
                with patch(
                    "main.meteora_ops.balances",
                    return_value={"sol": {"ui": 1.0}, "usdc": {"ui": 10}},
                ):
                    with patch("main.money_ops.rebalance_position") as reb:
                        await main_mod.monitor_position(now=t0 + timedelta(minutes=10))
                        reb.assert_not_called()

    async def test_daily_cap_stops_and_notifies(self) -> None:
        t0 = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        patch.object(bot_config, "MAX_REBALANCES_PER_DAY", 1).start()
        st = ar_limits.AutoRebalanceState(day_utc="2026-07-29", count_today=1)
        # last rebalance long ago so interval OK
        st.last_rebalance_at = (t0 - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        ar_limits.save_state(st)

        main_mod.out_of_range_since = t0 - timedelta(minutes=30)
        with patch("main.meteora_ops.pool_info", return_value=_pool(active=200)):
            with patch("main.money_ops.get_primary_position", return_value=_pos()):
                with patch(
                    "main.meteora_ops.balances",
                    return_value={"sol": {"ui": 1.0}, "usdc": {"ui": 10}},
                ):
                    with patch("main.money_ops.rebalance_position") as reb:
                        with patch("main.send_telegram_message") as tg:
                            await main_mod.monitor_position(now=t0)
                            reb.assert_not_called()
                            texts = " ".join(str(c.args[0]) for c in tg.call_args_list)
                            self.assertIn("Лимит", texts)

    async def test_uneconomic_warns_but_still_rebalances(self) -> None:
        t0 = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
        main_mod.out_of_range_since = t0 - timedelta(minutes=30)
        short_cycles = [{"duration_hours": 1.0, "incomplete": False} for _ in range(5)]
        with patch("main.meteora_ops.pool_info", return_value=_pool(active=200)):
            with patch("main.money_ops.get_primary_position", return_value=_pos()):
                with patch(
                    "main.meteora_ops.balances",
                    return_value={"sol": {"ui": 1.0}, "usdc": {"ui": 10}},
                ):
                    with patch(
                        "main.cycle_journal.get_default_journal"
                    ) as gj:
                        journal = MagicMock()
                        journal.read_recent_cycles.return_value = short_cycles
                        journal.on_monitor_tick = MagicMock()
                        gj.return_value = journal
                        with patch("main.money_ops.rebalance_position") as reb:
                            with patch("main.send_telegram_message") as tg:
                                await main_mod.monitor_position(now=t0)
                                reb.assert_called_once()
                                self.assertTrue(reb.call_args.kwargs.get("auto"))
                                texts = " ".join(
                                    str(c.args[0]) for c in tg.call_args_list
                                )
                                self.assertIn("окупаемости", texts.lower())

    async def test_reopen_pending_blocks_auto(self) -> None:
        with patch("main.is_reopen_pending", return_value=True):
            with patch("main.load_reopen_pending", return_value={"x": 1}):
                with patch("main.money_ops.rebalance_position") as reb:
                    with patch("main.send_telegram_message") as tg:
                        await main_mod.monitor_position(
                            now=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
                        )
                        reb.assert_not_called()
                        texts = " ".join(str(c.args[0]) for c in tg.call_args_list)
                        self.assertIn("reopen_pending", texts)

    async def test_recheck_after_delay_cancels_if_back(self) -> None:
        t0 = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        main_mod.out_of_range_since = t0 - timedelta(minutes=30)
        # First pool_info in tick: out; second (recheck): in range
        pools = [_pool(active=200), _pool(active=105)]

        def pool_side(*a, **k):
            return pools.pop(0) if pools else _pool(active=105)

        with patch("main.meteora_ops.pool_info", side_effect=pool_side):
            with patch("main.money_ops.get_primary_position", return_value=_pos()):
                with patch("main.money_ops.rebalance_position") as reb:
                    with patch("main.send_telegram_message") as tg:
                        await main_mod.monitor_position(now=t0)
                        reb.assert_not_called()
                        texts = " ".join(str(c.args[0]) for c in tg.call_args_list)
                        self.assertIn("отменён", texts.lower())
                        self.assertIsNone(main_mod.out_of_range_since)


class MedianHoursTests(unittest.TestCase):
    def test_median(self) -> None:
        self.assertEqual(
            ar_limits.median_cycle_hours(
                [{"duration_hours": 1}, {"duration_hours": 3}, {"duration_hours": 2}]
            ),
            2.0,
        )


if __name__ == "__main__":
    unittest.main()
