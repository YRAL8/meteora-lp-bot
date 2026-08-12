#!/usr/bin/env python3
"""LITE_4: heartbeat — silence must not look like health."""
from __future__ import annotations

import asyncio
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _pos() -> dict:
    return {
        "pubkey": "PosHb111",
        "lowerBinId": 100,
        "upperBinId": 110,
        "sol": 0.1,
        "usdc": 5.0,
        "fees": {"sol": 0, "usdc": 0},
    }


def _pool() -> dict:
    return {
        "activeId": 105,
        "usdcPerSol": 140.0,
        "binStep": 1,
        "maxBinsPerPosition": 70,
    }


def _bal(*, sol: float = 1.0) -> dict:
    return {
        "sol": {"ui": sol},
        "usdc": {"ui": 10.0},
        "solAvailableForOpen": max(0.0, sol - 0.08),
    }


class HeartbeatIntervalTests(unittest.IsolatedAsyncioTestCase):
    async def test_interval_one_message_then_zero_when_disabled(self) -> None:
        import bot_config
        import main as main_mod

        sent: list[str] = []
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                raise asyncio.CancelledError()

        with (
            patch.object(bot_config, "HEARTBEAT_INTERVAL_HOURS", 4.0),
            patch(
                "main.send_telegram_message",
                side_effect=lambda t: sent.append(t),
            ),
            patch(
                "main.format_heartbeat",
                return_value="💓 beat",
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await main_mod.heartbeat_loop(sleep_fn=fake_sleep)

        self.assertEqual(sleeps[0], 4.0 * 3600.0)
        self.assertEqual(len(sent), 1, "exactly one beat after first interval")

        sent.clear()
        sleeps.clear()
        with patch.object(bot_config, "HEARTBEAT_INTERVAL_HOURS", 0.0):
            await main_mod.heartbeat_loop(sleep_fn=fake_sleep)
        self.assertEqual(sent, [])
        self.assertEqual(sleeps, [])


class HeartbeatStaleSnapshotTests(unittest.TestCase):
    def test_read_failure_marks_last_good_as_not_current(self) -> None:
        import bot_config
        import bot_state
        import main as main_mod

        main_mod.reset_heartbeat_snapshot_for_tests()
        main_mod.reset_storm_guards_for_tests()
        bot_state.bot_paused = False
        bot_state.bot_frozen = False
        t0 = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        main_mod._store_good_position_snapshot(
            _pos(),
            active_id=105,
            usdc_per_sol=140.0,
            bin_step=1,
            now=t0,
        )

        with (
            patch.object(bot_config, "AUTO_REBALANCE", True),
            patch.object(bot_config, "DRY_RUN", True),
            patch.object(bot_config, "effective_network", return_value="devnet"),
            patch("main.bot_config.wallet_pubkey", return_value="Owner"),
            patch("main.money_ops.ops_kwargs", return_value={}),
            patch(
                "main.meteora_ops.balances",
                side_effect=RuntimeError("rpc down"),
            ),
            patch("main.is_reopen_pending", return_value=False),
            patch("meteora_exec.list_unresolved_journal", return_value=[]),
        ):
            text = main_mod.format_heartbeat(now=t0 + timedelta(hours=3))

        self.assertIn("прочитать не удалось", text.lower())
        self.assertIn("не текущая", text.lower())
        self.assertIn("≈3.0 ч", text)
        # Old table numbers may appear only with the age disclaimer.
        self.assertIn("Последняя", text)
        self.assertIn("удачная", text)
        self.assertIn("$19.00", text)  # from last-good table — only with age mark above


class HeartbeatAutoStoppedTests(unittest.TestCase):
    def test_reopen_pending_still_sends_with_reason(self) -> None:
        import bot_config
        import bot_state
        import main as main_mod

        main_mod.reset_heartbeat_snapshot_for_tests()
        main_mod.reset_storm_guards_for_tests()
        bot_state.bot_paused = False
        bot_state.bot_frozen = False
        now = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)

        with (
            patch.object(bot_config, "AUTO_REBALANCE", True),
            patch.object(bot_config, "DRY_RUN", True),
            patch.object(bot_config, "effective_network", return_value="devnet"),
            patch("main.bot_config.wallet_pubkey", return_value="Owner"),
            patch("main.money_ops.ops_kwargs", return_value={}),
            patch("main.meteora_ops.balances", return_value=_bal()),
            patch("main.meteora_ops.pool_info", return_value=_pool()),
            patch("main.money_ops.get_primary_position", return_value=_pos()),
            patch("main.is_reopen_pending", return_value=True),
            patch("meteora_exec.list_unresolved_journal", return_value=[]),
        ):
            text = main_mod.format_heartbeat(now=now)

        self.assertIn("Сердцебиение", text)
        self.assertIn("Автоматика стоит", text)
        self.assertIn("reopen_pending", text.lower())


class HeartbeatDoesNotKillMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_exception_monitor_continues(self) -> None:
        import bot_config
        import main as main_mod

        sleeps = 0

        async def one_sleep(_s: float) -> None:
            nonlocal sleeps
            sleeps += 1
            if sleeps >= 1:
                raise asyncio.CancelledError()

        with (
            patch.object(bot_config, "HEARTBEAT_INTERVAL_HOURS", 1.0),
            patch(
                "main.format_heartbeat",
                side_effect=RuntimeError("boom in heartbeat"),
            ),
            patch("main.send_telegram_message"),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await main_mod.heartbeat_loop(sleep_fn=one_sleep)

        # Monitor tick still works after heartbeat path swallowed the error.
        with (
            patch("main.bot_state.bot_paused", False),
            patch("main.bot_state.bot_frozen", False),
            patch("main.is_reopen_pending", return_value=False),
            patch("main._check_unresolved_journal", return_value=False),
            patch("main.bot_config.wallet_pubkey", return_value="Owner"),
            patch("main.money_ops.ops_kwargs", return_value={}),
            patch("main.meteora_ops.pool_info", return_value=_pool()),
            patch("main.money_ops.get_primary_position", return_value=None),
            patch("main.send_telegram_message"),
        ):
            await main_mod.monitor_position(
                now=datetime(2026, 8, 10, 16, 0, tzinfo=timezone.utc)
            )
        self.assertIsNotNone(main_mod._last_monitor_tick_at)


_TELEGRAM_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "code", "pre", "span", "tg-spoiler", "blockquote",
}
_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^<>]*)?>")


def _find_bad_angle(text: str) -> str | None:
    """Первый '<', который Telegram не примет как тег. None — текст разберётся."""
    pos = 0
    while True:
        i = text.find("<", pos)
        if i == -1:
            return None
        m = _TAG_RE.match(text, i)
        if m is None or m.group(1).lower() not in _TELEGRAM_TAGS:
            return text[i : i + 24]
        pos = m.end()


class HeartbeatHtmlSafetyTests(unittest.TestCase):
    """Причина с '<' роняла всё сообщение: бот молчал именно когда мало SOL."""

    def test_checker_catches_raw_angle(self) -> None:
        # Проверка самой проверки: без неё тест ниже мог бы пройти впустую.
        self.assertIsNone(_find_bad_angle("<b>ok</b> и <code>x</code>"))
        self.assertIsNotNone(_find_bad_angle("доступно 0.0494 < 0.05"))

    def test_low_sol_reason_does_not_break_html(self) -> None:
        import bot_config
        import bot_state
        import main as main_mod

        main_mod.reset_heartbeat_snapshot_for_tests()
        main_mod.reset_storm_guards_for_tests()
        bot_state.bot_paused = False
        bot_state.bot_frozen = False
        now = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)

        with (
            patch.object(bot_config, "AUTO_REBALANCE", True),
            patch.object(bot_config, "DRY_RUN", False),
            patch.object(bot_config, "MIN_SOL_BALANCE", 0.05),
            patch.object(bot_config, "effective_network", return_value="mainnet"),
            patch("main.bot_config.wallet_pubkey", return_value="Owner"),
            patch("main.money_ops.ops_kwargs", return_value={}),
            patch("main.meteora_ops.balances", return_value=_bal(sol=0.1294)),
            patch("main.meteora_ops.pool_info", return_value=_pool()),
            patch("main.money_ops.get_primary_position", return_value=None),
            patch("main.is_reopen_pending", return_value=False),
            patch("meteora_exec.list_unresolved_journal", return_value=[]),
        ):
            text = main_mod.format_heartbeat(now=now)

        # Ветка действительно та: причина про нехватку SOL в тексте есть.
        self.assertIn("мало SOL", text)
        bad = _find_bad_angle(text)
        self.assertIsNone(
            bad, f"Telegram отвергнет сообщение целиком, сырой тег: {bad!r}"
        )


class StampTests(unittest.TestCase):
    """Дата в сообщениях: Telegram показывает только часы, день теряется."""

    def test_local_and_utc_with_date(self) -> None:
        import bot_config
        from telegram_notify import format_stamp

        # Летом Берлин = UTC+2, значит 10:39 UTC → 12:39 местного.
        now = datetime(2026, 8, 12, 10, 39, tzinfo=timezone.utc)
        with patch.object(bot_config, "DISPLAY_TIMEZONE", "Europe/Berlin"):
            s = format_stamp(now)
        self.assertIn("12.08.2026", s)
        self.assertIn("12:39", s)
        self.assertIn("10:39", s)
        self.assertIn("UTC", s)

    def test_naive_datetime_treated_as_utc(self) -> None:
        import bot_config
        from telegram_notify import format_stamp

        with patch.object(bot_config, "DISPLAY_TIMEZONE", "Europe/Berlin"):
            s = format_stamp(datetime(2026, 8, 12, 10, 39))
        self.assertIn("12:39", s)

    def test_broken_timezone_falls_back_to_utc(self) -> None:
        """Нет tzdata или опечатка в поясе — отметка не стоит потерянного сообщения."""
        import bot_config
        from telegram_notify import format_stamp

        now = datetime(2026, 8, 12, 10, 39, tzinfo=timezone.utc)
        with patch.object(bot_config, "DISPLAY_TIMEZONE", "Nowhere/Nothing"):
            s = format_stamp(now)
        self.assertEqual(s, "12.08.2026 10:39 UTC")

    def test_heartbeat_carries_the_date(self) -> None:
        import bot_config
        import bot_state
        import main as main_mod

        main_mod.reset_heartbeat_snapshot_for_tests()
        main_mod.reset_storm_guards_for_tests()
        bot_state.bot_paused = False
        bot_state.bot_frozen = False
        now = datetime(2026, 8, 12, 10, 39, tzinfo=timezone.utc)

        with (
            patch.object(bot_config, "AUTO_REBALANCE", True),
            patch.object(bot_config, "DRY_RUN", False),
            patch.object(bot_config, "DISPLAY_TIMEZONE", "Europe/Berlin"),
            patch.object(bot_config, "effective_network", return_value="mainnet"),
            patch("main.bot_config.wallet_pubkey", return_value="Owner"),
            patch("main.money_ops.ops_kwargs", return_value={}),
            patch("main.meteora_ops.balances", return_value=_bal()),
            patch("main.meteora_ops.pool_info", return_value=_pool()),
            patch("main.money_ops.get_primary_position", return_value=None),
            patch("main.is_reopen_pending", return_value=False),
            patch("meteora_exec.list_unresolved_journal", return_value=[]),
        ):
            text = main_mod.format_heartbeat(now=now)

        self.assertIn("12.08.2026", text)
        self.assertIsNone(_find_bad_angle(text))


if __name__ == "__main__":
    unittest.main()
