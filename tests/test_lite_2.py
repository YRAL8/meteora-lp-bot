#!/usr/bin/env python3
"""LITE_2: in-memory storm guards (reset on process restart)."""
from __future__ import annotations

import importlib
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class StormGuardMemoryTests(unittest.TestCase):
    def test_daily_counter_resets_on_module_reload(self) -> None:
        """After a process restart (simulated by reload), the UTC-day count is 0."""
        import main as main_mod

        now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        main_mod.reset_storm_guards_for_tests()
        main_mod._record_rebalance(now)
        main_mod._record_rebalance(now + __import__("datetime").timedelta(minutes=1))
        self.assertEqual(main_mod._rebalance_count_today, 2)
        self.assertEqual(main_mod._rebalance_day_utc, "2026-07-29")

        # Simulate new process: re-import fresh module state.
        importlib.reload(main_mod)
        self.assertEqual(main_mod._rebalance_count_today, 0)
        self.assertIsNone(main_mod._last_rebalance_at)
        self.assertEqual(main_mod._rebalance_day_utc, "")
        # Keep suite isolation for other tests that import main.
        main_mod.reset_storm_guards_for_tests()

    def test_new_utc_day_resets_count_without_reload(self) -> None:
        import main as main_mod

        main_mod.reset_storm_guards_for_tests()
        d0 = datetime(2026, 7, 29, 23, 0, tzinfo=timezone.utc)
        main_mod._record_rebalance(d0)
        self.assertEqual(main_mod._rebalance_count_today, 1)
        d1 = datetime(2026, 7, 30, 0, 5, tzinfo=timezone.utc)
        main_mod._ensure_rebalance_day(d1)
        self.assertEqual(main_mod._rebalance_count_today, 0)
        self.assertEqual(main_mod._rebalance_day_utc, "2026-07-30")
        main_mod.reset_storm_guards_for_tests()


if __name__ == "__main__":
    unittest.main()
