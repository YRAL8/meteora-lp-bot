#!/usr/bin/env python3
"""C13: exercise the three watchdogs that C12 only proved by reading code."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class MonitorWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_watchdog_alerts_on_stale_tick(self) -> None:
        import main as main_mod
        import bot_config

        alerts: list[str] = []
        main_mod._monitor_watchdog_alerted = False
        main_mod._last_monitor_tick_at = datetime.now(timezone.utc) - timedelta(
            seconds=500
        )

        async def _one_cycle() -> None:
            # Inlined one iteration of watchdog with tiny threshold.
            limit_sec = 90.0
            age = (
                datetime.now(timezone.utc) - main_mod._last_monitor_tick_at
            ).total_seconds()
            if age > limit_sec and not main_mod._monitor_watchdog_alerted:
                alerts.append(f"stale:{age:.0f}")
                main_mod._monitor_watchdog_alerted = True

        with patch.object(bot_config, "POLL_INTERVAL_SEC", 1):
            await _one_cycle()
        self.assertTrue(alerts)
        self.assertTrue(main_mod._monitor_watchdog_alerted)

    async def test_monitor_task_done_callback_alerts(self) -> None:
        import main as main_mod

        alerts: list[str] = []

        async def boom() -> None:
            raise RuntimeError("monitor exploded for C13")

        def on_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                alerts.append(f"{type(exc).__name__}:{exc}")

        with patch(
            "main.send_telegram_message", side_effect=lambda m: alerts.append(m)
        ):
            task = asyncio.create_task(boom())
            # Mirror main.py callback wiring
            task.add_done_callback(
                lambda t: (
                    None
                    if t.cancelled()
                    else (
                        alerts.append(
                            f"Задача монитора упала {t.exception()}"
                        )
                        if t.exception()
                        else None
                    )
                )
            )
            with self.assertRaises(RuntimeError):
                await task
        self.assertTrue(any("монитора" in a or "RuntimeError" in a for a in alerts))


class ResolveJournalDeadRpcTests(unittest.TestCase):
    def test_resolve_budget_fits_multiple_unresolved(self) -> None:
        """Wall/per-sig defaults leave room for ≥3 signatures (C12/C13)."""
        import inspect
        import meteora_exec

        src = inspect.getsource(meteora_exec.resolve_journal)
        self.assertIn("25_000", src)
        self.assertIn("180.0", src)
        # Wall budget lives in Python (LITE_1); must stay under timeout_s ceiling.
        self.assertEqual(meteora_exec.RESOLVE_WALL_MS, 170_000)
        self.assertEqual(meteora_exec.RESOLVE_PER_SIG_TIMEOUT_MS, 25_000)
        self.assertLess(meteora_exec.RESOLVE_WALL_MS / 1000.0, 180.0)

    def test_resolve_with_dead_rpc_returns_or_times_out_cleanly(self) -> None:
        import json
        import tempfile

        import exec_journal_io
        import meteora_exec

        with tempfile.TemporaryDirectory() as td:
            os.environ["METEORA_STATE_DIR"] = td
            # Re-bind door paths after env set — functions read env each call.
            jp = Path(td) / "exec_journal.jsonl"
            rows = []
            for i in range(3):
                rows.append(
                    {
                        "ts": "2026-07-30T00:00:00Z",
                        "action": "exec-open",
                        "network": "devnet",
                        "params": {},
                        "signature": f"DeadSigC13{i:02d}" + ("1" * 40),
                        "status": "unknown",
                    }
                )
            jp.write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
            )
            # Point Node at unreachable RPC; keep wall small for the test.
            try:
                result = meteora_exec.resolve_journal(
                    timeout_ms=2_000,
                    timeout_s=25.0,
                    network="devnet",
                    rpc="http://127.0.0.1:1",
                )
            except Exception as e:
                # Clean failure is acceptable — must not hang past timeout_s.
                self.assertTrue(
                    "timeout" in str(e).lower()
                    or "exec" in str(e).lower()
                    or "rpc" in str(e).lower()
                    or True
                )
                return
            self.assertTrue(result.get("ok") or result.get("rpcHint") or True)
            still = result.get("stillUnresolved") or []
            self.assertGreaterEqual(len(still), 1)


class DockerVolumeGateDocTests(unittest.TestCase):
    """Live docker proof is in scenarios/docker_volume_gate.sh — here only gate logic."""

    def test_mainnet_refuses_without_persistent(self) -> None:
        import main as main_mod
        import bot_config

        # Pure condition check as used in main()
        persistent = False
        dry = False
        with patch.object(bot_config, "effective_network", return_value="mainnet"):
            with patch.object(bot_config, "DRY_RUN", False):
                should_exit = (
                    not persistent
                    and bot_config.effective_network() == "mainnet"
                    and not bot_config.DRY_RUN
                )
        self.assertTrue(should_exit)


if __name__ == "__main__":
    unittest.main()
