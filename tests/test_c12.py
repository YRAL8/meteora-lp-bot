#!/usr/bin/env python3
"""C12: state isolation, SOL-after-swap gate, payback scaling, journal helpers."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class StateDirRedirectTests(unittest.TestCase):
    def test_state_paths_honour_env(self) -> None:
        import state_paths

        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"METEORA_STATE_DIR": td}):
                self.assertEqual(state_paths.state_dir(), Path(td))
                self.assertEqual(
                    state_paths.path("auto_rebalance.json"),
                    Path(td) / "auto_rebalance.json",
                )

    def test_writes_go_to_override_not_repo_state(self) -> None:
        import auto_rebalance_limits as ar_limits
        import reopen_pending

        real = ROOT / "state" / "auto_rebalance.json"
        before = real.read_bytes() if real.is_file() else None
        with tempfile.TemporaryDirectory() as td:
            with patch.object(ar_limits, "STATE_PATH", Path(td) / "auto_rebalance.json"):
                with patch.object(
                    reopen_pending, "REOPEN_PENDING_PATH", Path(td) / "reopen_pending.json"
                ):
                    st = ar_limits.AutoRebalanceState(
                        day_utc="2099-01-01", count_today=0
                    )
                    ar_limits.save_state(st)
                    reopen_pending.set_reopen_pending(True, meta={"t": 1})
                    self.assertTrue((Path(td) / "auto_rebalance.json").is_file())
                    self.assertTrue((Path(td) / "reopen_pending.json").is_file())
        after = real.read_bytes() if real.is_file() else None
        self.assertEqual(before, after)


class SolAfterSwapGateTests(unittest.TestCase):
    def test_all_usdc_can_swap_to_sol(self) -> None:
        import money_ops

        # Upward exit: ~$10 all USDC, need ~0.05 SOL at $140.
        short = money_ops.sol_short_after_planned_swap(
            est_sol=0.0004,
            est_usdc=10.0,
            est_budget=9.8,
            need_sol=0.05,
            price=140.0,
        )
        self.assertFalse(short)

    def test_dust_budget_blocks(self) -> None:
        import money_ops

        self.assertTrue(
            money_ops.sol_short_after_planned_swap(
                est_sol=0.0,
                est_usdc=0.01,
                est_budget=0.01,
                need_sol=0.05,
                price=140.0,
            )
        )

    def test_no_usdc_to_cover_deficit_blocks(self) -> None:
        import money_ops

        self.assertTrue(
            money_ops.sol_short_after_planned_swap(
                est_sol=0.001,
                est_usdc=0.01,
                est_budget=0.15,
                need_sol=0.05,
                price=140.0,
            )
        )


class PaybackHoursTests(unittest.TestCase):
    def test_scales_with_size(self) -> None:
        import bot_config

        h10 = bot_config.effective_payback_hours(10.0)
        h1000 = bot_config.effective_payback_hours(1000.0)
        self.assertGreater(h10, 8.0)
        self.assertLess(h10, 12.0)
        self.assertGreater(h1000, 2.5)
        self.assertLess(h1000, 3.5)
        self.assertGreater(h10, h1000)


class ReopenPendingWriteTests(unittest.TestCase):
    def test_oserror_becomes_reopen_pending_write_error(self) -> None:
        import reopen_pending

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "reopen_pending.json"
            with patch.object(reopen_pending, "REOPEN_PENDING_PATH", path):
                # Make parent unwritable by pointing at a file-as-dir trick:
                blocker = Path(td) / "notadir"
                blocker.write_text("x", encoding="utf-8")
                with patch.object(
                    reopen_pending, "REOPEN_PENDING_PATH", blocker / "nested.json"
                ):
                    with self.assertRaises(reopen_pending.ReopenPendingWriteError):
                        reopen_pending.set_reopen_pending(True, meta={})


class UnresolvedJournalListTests(unittest.TestCase):
    def test_lists_pending_latest(self) -> None:
        import meteora_exec
        import state_paths

        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"METEORA_STATE_DIR": td}):
                # Re-bind helpers that cached paths at import — call via path().
                jp = Path(td) / "exec_journal.jsonl"
                jp.write_text(
                    '{"signature":"SigA","status":"pending","action":"exec-open"}\n'
                    '{"signature":"SigA","status":"confirmed","action":"exec-open"}\n'
                    '{"signature":"SigB","status":"unknown","action":"exec-close"}\n',
                    encoding="utf-8",
                )
                with patch.object(
                    meteora_exec, "exec_journal_path", lambda: jp
                ):
                    u = meteora_exec.list_unresolved_journal()
                    sigs = {x["signature"] for x in u}
                    self.assertEqual(sigs, {"SigB"})


if __name__ == "__main__":
    unittest.main()
