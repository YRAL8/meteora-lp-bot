#!/usr/bin/env python3
"""LITE_2 + 29.08.2026: storm guards — UTC-day rollover and restart survival."""
from __future__ import annotations

import importlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class StormGuardPersistenceTests(unittest.TestCase):
    def test_daily_counter_survives_restart(self) -> None:
        """Перезапуск не должен выдавать боту чистый дневной бюджет.

        До 29.08.2026 счётчик жил только в памяти: часы выдержки перезапуск
        переживали, а «сколько ребалансов уже сделано сегодня» — нет.
        """
        import main as main_mod

        now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
        main_mod.reset_storm_guards_for_tests()
        main_mod._record_rebalance(now)
        main_mod._record_rebalance(now + timedelta(minutes=1))
        self.assertEqual(main_mod._rebalance_count_today, 2)
        self.assertEqual(main_mod._rebalance_day_utc, "2026-07-29")

        # Новый процесс: память чистая, диск — нет.
        importlib.reload(main_mod)
        self.assertEqual(main_mod._rebalance_count_today, 0, "reload чистит память")
        main_mod.restore_monitor_timers()

        self.assertEqual(main_mod._rebalance_count_today, 2)
        self.assertEqual(main_mod._rebalance_day_utc, "2026-07-29")
        self.assertIsNotNone(main_mod._last_rebalance_at)
        self.assertEqual(
            main_mod._minutes_since_last_rebalance(now + timedelta(minutes=6)),
            5.0,
            "сторож частоты тоже должен пережить перезапуск",
        )
        main_mod.reset_storm_guards_for_tests()

    def test_restore_from_missing_file_starts_clean(self) -> None:
        """Первый запуск и битый файл — не повод отказаться работать."""
        import main as main_mod
        import monitor_timer_state

        main_mod.reset_storm_guards_for_tests()
        if monitor_timer_state.STATE_PATH.is_file():
            monitor_timer_state.STATE_PATH.unlink()
        main_mod.restore_monitor_timers()
        self.assertEqual(main_mod._rebalance_count_today, 0)
        self.assertIsNone(main_mod._last_rebalance_at)
        self.assertIsNone(main_mod.out_of_range_since)

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
