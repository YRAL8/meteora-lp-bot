#!/usr/bin/env python3
"""LITE_15: pause/freeze survive restart; logs; journal fields; pool snapshots."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot_state  # noqa: E402
import telegram_commands as tg  # noqa: E402


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


def _update(args: list[str], *, chat_id: int = 4242):
    msg = _FakeMessage()
    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    ctx = MagicMock()
    ctx.args = args
    return update, msg, ctx


class PauseSurvivesRestartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_state.bot_paused = False
        bot_state.bot_frozen = False

    def tearDown(self) -> None:
        bot_state.bot_paused = False
        bot_state.bot_frozen = False

    async def test_pauza_then_reread_stays_paused(self) -> None:
        """Fails on old code: flags were RAM-only, restart always resumed combat."""
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"METEORA_STATE_DIR": td}):
                update, msg, ctx = _update([])
                await tg.pauza_command(update, ctx)
                self.assertTrue(bot_state.bot_paused)
                # Simulate process restart: RAM flags reset, disk is the source.
                bot_state.bot_paused = False
                bot_state.bot_frozen = False
                line = bot_state.restore_mode()
                self.assertTrue(bot_state.bot_paused, msg=line)
                self.assertFalse(bot_state.bot_frozen)
                self.assertIn("пауз", line.lower())

    def test_corrupt_mode_file_starts_paused_not_combat(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"METEORA_STATE_DIR": td}):
                path = Path(td) / "bot_mode.json"
                path.write_text("{not-json", encoding="utf-8")
                bot_state.bot_paused = False
                bot_state.bot_frozen = False
                line = bot_state.restore_mode()
                self.assertTrue(bot_state.bot_paused, msg=line)
                self.assertFalse(bot_state.bot_frozen)
                self.assertIn("пауз", line.lower())

    def test_missing_mode_file_starts_paused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"METEORA_STATE_DIR": td}):
                bot_state.bot_paused = False
                bot_state.bot_frozen = False
                line = bot_state.restore_mode()
                self.assertTrue(bot_state.bot_paused, msg=line)
                self.assertIn("пауз", line.lower())

class CommandAndTickLogTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        bot_state.bot_paused = False
        bot_state.bot_frozen = False
        import main as main_mod

        main_mod.out_of_range_since = None
        main_mod.last_auto_attempt_at = None
        main_mod.reset_storm_guards_for_tests()

    async def test_command_log_line_has_chat_and_decision(self) -> None:
        line = tg.format_command_log_line(
            "open", chat_id="4242", args=["-1"], decision="refused: Сумма должна быть больше 0."
        )
        self.assertEqual(
            line,
            "command open chat=4242 args=['-1'] refused: Сумма должна быть больше 0.",
        )
        update, msg, ctx = _update(["-1"], chat_id=4242)
        with self.assertLogs(tg.log, level="INFO") as cm:
            await tg.open_command(update, ctx)
        joined = "\n".join(cm.output)
        self.assertIn(line, joined)

    async def test_mode_change_log_line(self) -> None:
        line = tg.format_mode_change_log_line("пауза", chat_id="4242")
        self.assertEqual(line, "режим: пауза chat=4242")
        update, msg, ctx = _update([], chat_id=4242)
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"METEORA_STATE_DIR": td}):
                with self.assertLogs(tg.log, level="INFO") as cm:
                    await tg.pauza_command(update, ctx)
        joined = "\n".join(cm.output)
        self.assertIn(line, joined)

    async def test_heartbeat_ok_logged(self) -> None:
        import bot_config
        import main as main_mod

        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                raise asyncio.CancelledError()

        with (
            patch.object(bot_config, "HEARTBEAT_INTERVAL_HOURS", 4.0),
            patch("main.send_telegram_message"),
            patch("main.format_heartbeat", return_value="beat"),
            self.assertLogs("main", level="INFO") as cm,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await main_mod.heartbeat_loop(sleep_fn=fake_sleep)
        self.assertTrue(any("heartbeat ok" in x for x in cm.output))

    async def test_deferred_rebalance_log_line(self) -> None:
        import bot_config
        import main as main_mod
        from datetime import datetime, timedelta, timezone

        t0 = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        main_mod.out_of_range_since = None
        main_mod.last_auto_attempt_at = None
        main_mod.reset_storm_guards_for_tests()
        main_mod._last_rebalance_at = t0
        main_mod._rebalance_day_utc = "2026-07-29"
        main_mod._rebalance_count_today = 1
        main_mod.out_of_range_since = t0 - timedelta(minutes=30)
        pos = {
            "pubkey": "P",
            "lowerBinId": 100,
            "upperBinId": 110,
            "sol": 0.1,
            "usdc": 5.0,
            "fees": {},
        }
        pool = {
            "activeId": 200,
            "binStep": 1,
            "usdcPerSol": 100.0,
            "maxBinsPerPosition": 70,
        }
        with (
            patch.object(bot_config, "AUTO_REBALANCE", True),
            patch.object(bot_config, "REBALANCE_DELAY_MIN", 20),
            patch.object(bot_config, "MIN_REBALANCE_INTERVAL_MIN", 60),
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
            patch("main.send_telegram_message"),
            patch("main.bot_config.wallet_pubkey", return_value="W"),
            self.assertLogs("main", level="INFO") as cm,
        ):
            await main_mod.monitor_position(now=t0 + timedelta(minutes=10))
        joined = "\n".join(cm.output)
        self.assertIn("ребаланс отложен: сторож частоты", joined)


class CycleJournalFieldsTests(unittest.TestCase):
    def test_new_record_has_width_time_and_rent_fields(self) -> None:
        from datetime import datetime, timezone

        from meteora_cycle_journal import JOURNAL_RECORD_VERSION, CycleJournal

        with tempfile.TemporaryDirectory() as td:
            j = CycleJournal(data_dir=td)
            t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
            j.on_open(
                position_pubkey="Pos111",
                range_width_pct=1.0,
                lower_bin_id=10,
                upper_bin_id=18,
                lower_price=90.0,
                upper_price=110.0,
                open_price=100.0,
                open_sol_qty=0.1,
                open_usdc_qty=10.0,
                open_position_value_usd=20.0,
                now=t0,
                bin_step=4,
                bins_count=9,
                position_rent_sol=0.0738,
            )
            j.on_monitor_tick(100.0, True, 5.0)
            j.on_monitor_tick(120.0, False, 5.0)
            j.capture_close_snapshot(
                close_price=105.0,
                close_position_value_usd=20.5,
                fees_sol=0.001,
                fees_usdc=0.2,
                now=datetime(2026, 5, 1, 1, tzinfo=timezone.utc),
            )
            j.finalize_pending_cycle()
            rec = j.read_all_cycles()[0]
            self.assertEqual(rec["version"], JOURNAL_RECORD_VERSION)
            self.assertEqual(rec["bin_step"], 4)
            self.assertEqual(rec["bins_count"], 9)
            self.assertEqual(rec["in_range_minutes"], 5.0)
            self.assertEqual(rec["out_of_range_minutes"], 5.0)
            self.assertEqual(rec["position_rent_sol"], 0.0738)

    def test_old_journal_line_without_new_fields_still_reads(self) -> None:
        from meteora_cycle_journal import CycleJournal

        old = {
            "version": 1,
            "mint": "OldPos",
            "fees_usd": 1.25,
            "divergence_usd": -0.4,
            "pnl_usd": 0.8,
            "duration_hours": 3.0,
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cycle_journal.jsonl"
            path.write_text(json.dumps(old) + "\n", encoding="utf-8")
            recs = CycleJournal(data_dir=td).read_all_cycles()
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["mint"], "OldPos")
            self.assertEqual(recs[0]["fees_usd"], 1.25)
            self.assertIsNone(recs[0].get("bin_step"))
            self.assertIsNone(recs[0].get("in_range_minutes"))

    def test_old_active_state_without_new_fields_finalizes(self) -> None:
        from meteora_cycle_journal import CycleJournal

        old_state = {
            "version": 1,
            "active": None,
            "pending_close": {
                "cycle": {
                    "mint": "OldPos",
                    "open_time_utc": "2026-05-01T00:00:00Z",
                    "range_width_pct": 1.0,
                    "lower_price": 90.0,
                    "upper_price": 110.0,
                    "open_price": 100.0,
                    "open_sol_qty": 0.1,
                    "open_usdc_qty": 10.0,
                    "open_position_value_usd": 20.0,
                    "lower_bin_id": 10,
                    "upper_bin_id": 18,
                },
                "close_time_utc": "2026-05-01T01:00:00Z",
                "close_price": 105.0,
                "close_position_value_usd": 20.5,
                "fees_sol": 0.0,
                "fees_usdc": 0.1,
                "trigger": "manual",
            },
            "pending_swap": None,
        }
        with tempfile.TemporaryDirectory() as td:
            Path(td, "cycle_state.json").write_text(
                json.dumps(old_state), encoding="utf-8"
            )
            j = CycleJournal(data_dir=td)
            j.finalize_pending_cycle()
            rec = j.read_all_cycles()[0]
            self.assertEqual(rec["mint"], "OldPos")
            self.assertIn("bin_step", rec)
            self.assertEqual(rec["bins_count"], 9)


class PoolSnapshotTests(unittest.TestCase):
    def test_three_appended_rows(self) -> None:
        import pool_snapshots

        payload = {
            "tvl": 1_000_000.0,
            "volume": {"24h": 50_000.0},
            "fees": {"24h": 120.0},
            "apy": 35.5,
            "active_id": -6422,
            "current_price": 76.67,
        }
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"METEORA_STATE_DIR": td}):
                for i in range(3):
                    pool_snapshots.take_snapshot(
                        pool="PoolAddr111",
                        fetch=lambda _addr, n=i: payload,
                    )
                text = (Path(td) / "pool_snapshots.jsonl").read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 3)
        rows = [json.loads(ln) for ln in lines]
        for row in rows:
            self.assertEqual(row["pool"], "PoolAddr111")
            self.assertEqual(row["tvl"], 1_000_000.0)
            self.assertEqual(row["volume_24h"], 50_000.0)
            self.assertEqual(row["fees_24h"], 120.0)
            self.assertEqual(row["apy"], 35.5)
            self.assertEqual(row["active_bin"], -6422)
            self.assertEqual(row["price"], 76.67)
            self.assertNotIn("error", row)

    def test_api_failure_writes_error_and_stays_alive(self) -> None:
        import pool_snapshots

        def boom(_addr: str) -> dict:
            raise TimeoutError("api down")

        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"METEORA_STATE_DIR": td}):
                row = pool_snapshots.take_snapshot(pool="PoolAddr111", fetch=boom)
                self.assertIn("error", row)
                self.assertIn("TimeoutError", row["error"])
                # still alive — a second successful row can follow
                ok = pool_snapshots.take_snapshot(
                    pool="PoolAddr111",
                    fetch=lambda _a: {"tvl": 1.0, "apy": 2.0, "current_price": 3.0},
                )
                self.assertNotIn("error", ok)
                lines = (Path(td) / "pool_snapshots.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("error", json.loads(lines[0]))
        self.assertNotIn("error", json.loads(lines[1]))


if __name__ == "__main__":
    unittest.main()
