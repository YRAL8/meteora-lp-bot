#!/usr/bin/env python3
"""Offline tests for C8 audit fixes."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import auto_rebalance_limits as ar_limits  # noqa: E402
import bot_config  # noqa: E402
import bot_state  # noqa: E402
import main as main_mod  # noqa: E402
import money_ops  # noqa: E402
import telegram_commands as tg  # noqa: E402
from meteora_exec import MeteoraExecError  # noqa: E402
from reopen_pending import is_reopen_pending, set_reopen_pending  # noqa: E402


class FakeAutoSuccess(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        set_reopen_pending(False)
        main_mod.out_of_range_since = None
        main_mod.last_auto_attempt_at = None
        bot_state.bot_paused = False
        bot_state.bot_frozen = False

    async def asyncTearDown(self) -> None:
        set_reopen_pending(False)
        main_mod.last_auto_attempt_at = None
        if bot_state.money_lock.locked():
            bot_state.money_lock.release()
        bot_state.bot_frozen = False

    async def test_none_return_is_not_success(self) -> None:
        from datetime import datetime, timedelta, timezone

        t0 = datetime.now(timezone.utc)
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
        sent: list[str] = []
        lim = ar_limits.AutoRebalanceState(day_utc="2099-01-01", count_today=0)

        with (
            patch.object(bot_config, "AUTO_REBALANCE", True),
            patch.object(bot_config, "REBALANCE_DELAY_MIN", 0),
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
            patch("main.ar_limits.record_rebalance") as rec,
            patch("main.money_ops.rebalance_position", return_value=None),
            patch("main.send_telegram_message", side_effect=lambda t: sent.append(t)),
            patch("main.bot_config.wallet_pubkey", return_value="W"),
        ):
            main_mod.out_of_range_since = t0 - timedelta(minutes=30)
            await main_mod.monitor_position(now=t0)

        rec.assert_not_called()
        self.assertTrue(
            any("НЕ завершён" in t for t in sent),
            sent,
        )
        self.assertFalse(any("✅ Авто-ребаланс завершён" in t for t in sent), sent)


class UnknownNotSuccess(unittest.TestCase):
    def test_assert_exec_rejects_unknown(self) -> None:
        with self.assertRaises(MeteoraExecError) as cm:
            money_ops.assert_exec_fully_confirmed(
                {
                    "ok": True,
                    "sends": [{"signature": "x", "status": "unknown"}],
                }
            )
        self.assertTrue(cm.exception.payload.get("confirmationUnknown"))


class CrashHookGone(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_crash_refused(self) -> None:
        from tests.test_telegram_commands import _update_with_args

        update, msg, ctx = _update_with_args(["crash"])
        with patch("money_ops.rebalance_position") as reb:
            await tg.rebalance_command(update, ctx)
            reb.assert_not_called()
        self.assertTrue(any("убран" in r for r in msg.replies), msg.replies)


class StopDuringMoneyLock(unittest.IsolatedAsyncioTestCase):
    async def test_stop_replies_while_lock_held(self) -> None:
        from tests.test_telegram_commands import _update_with_args

        await bot_state.money_lock.acquire()
        try:
            update, msg, ctx = _update_with_args([])
            # Simulate concurrent stop while money lock is busy — must not hang.
            await asyncio.wait_for(tg.stop_command(update, ctx), timeout=2.0)
            self.assertTrue(bot_state.bot_frozen)
            self.assertTrue(any("заморозка" in r.lower() or "🛑" in r for r in msg.replies))
        finally:
            bot_state.bot_frozen = False
            bot_state.money_lock.release()


class JournalForget(unittest.TestCase):
    def test_forget_requires_signature_and_polls(self) -> None:
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        import meteora_exec

        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            state.mkdir()
            jp = state / "exec_journal.jsonl"
            jp.write_text(
                json.dumps(
                    {
                        "ts": "2020-01-01T00:00:00+00:00",
                        "action": "exec-open",
                        "network": "devnet",
                        "params": {},
                        "signature": "sigU",
                        "status": "unknown",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"METEORA_STATE_DIR": str(state)}):
                # Confirmed on-chain → refuse
                with patch("urllib.request.urlopen") as urlopen:
                    class _Resp:
                        def __enter__(self):
                            return self

                        def __exit__(self, *a):
                            return False

                        def read(self):
                            return json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "result": {
                                        "value": [
                                            {
                                                "confirmationStatus": "finalized",
                                                "slot": 1,
                                                "err": None,
                                            }
                                        ]
                                    },
                                }
                            ).encode()

                    urlopen.return_value = _Resp()
                    out = meteora_exec.forget_unresolved_journal(
                        signature="sigU", rpc="http://127.0.0.1:8899"
                    )
                self.assertFalse(out.get("ok"))
                self.assertEqual(out.get("refuse"), "confirmed")

                # Not found + old ts → allow
                with patch("urllib.request.urlopen") as urlopen:
                    class _Resp2:
                        def __enter__(self):
                            return self

                        def __exit__(self, *a):
                            return False

                        def read(self):
                            return json.dumps(
                                {"jsonrpc": "2.0", "result": {"value": [None]}}
                            ).encode()

                    urlopen.return_value = _Resp2()
                    out2 = meteora_exec.forget_unresolved_journal(
                        signature="sigU", rpc="http://127.0.0.1:8899"
                    )
                self.assertTrue(out2.get("ok"))
                self.assertEqual(out2.get("cleared"), 1)
                rows = [
                    json.loads(line)
                    for line in jp.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                self.assertEqual(rows[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
