#!/usr/bin/env python3
"""Offline tests for C7: HTML escaping and /pnl length limit."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import money_ops  # noqa: E402
import telegram_commands as tg  # noqa: E402
from meteora_exec import MeteoraExecError  # noqa: E402
from telegram_notify import (  # noqa: E402
    TG_MAX_MESSAGE_LEN,
    escape_html,
    format_exec_replies,
    format_html_error,
)


HOSTILE = 'fail <script> & "quotes" > emoji 🔥 «ёлка»'


class EscapeHtmlTests(unittest.TestCase):
    def test_escape_specials(self) -> None:
        out = escape_html(HOSTILE)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;", out)
        self.assertIn("&gt;", out)
        self.assertIn("&amp;", out)
        self.assertIn("🔥", out)

    def test_format_html_error(self) -> None:
        msg = format_html_error("❌ Ошибка: ", HOSTILE)
        self.assertTrue(msg.startswith("❌ Ошибка: "))
        self.assertIn("&lt;", msg)
        self.assertNotIn("<script>", msg)

    def test_format_exec_replies_escapes_sig_and_href(self) -> None:
        payload = {
            "sends": [
                {
                    "signature": "Ab<cd>&ef",
                    "explorer": 'https://explorer.solana.com/tx/Ab<cd>?q="x"',
                }
            ]
        }
        text = format_exec_replies(payload)
        self.assertIn("&lt;", text)
        self.assertIn("&amp;", text)
        self.assertIn("&gt;", text)
        self.assertIn('href="https://explorer.solana.com/tx/Ab&lt;cd&gt;?q=&quot;x&quot;"', text)
        self.assertNotIn("<cd>", text)

    def test_swap_failure_escapes_external_text(self) -> None:
        replies: list[str] = []
        with patch.object(
            money_ops.meteora_exec,
            "exec_swap",
            side_effect=MeteoraExecError({"error": HOSTILE}),
        ):
            ok = money_ops.apply_swap_suggestion(
                {"side": "sol-to-usdc", "amount": 0.01},
                replies.append,
            )
        self.assertFalse(ok)
        self.assertEqual(len(replies), 1)
        self.assertIn("&lt;", replies[0])
        self.assertNotIn("<script>", replies[0])

    def test_reopen_pending_meta_escaped_in_html(self) -> None:
        meta = {"reason": HOSTILE, "n": 1}
        escaped = escape_html(
            __import__("json").dumps(meta, ensure_ascii=False)[:400]
        )
        self.assertIn("&lt;", escaped)
        self.assertNotIn("<script>", escaped)


class PnlLengthTests(unittest.TestCase):
    def _fake_cycles(self, n: int) -> list[dict]:
        cycles = []
        for i in range(n):
            cycles.append(
                {
                    "close_time_utc": f"2026-07-{(i % 28) + 1:02d}T12:00:00Z",
                    "range_width_pct": float((i % 17) + 1) * 0.1,
                    "duration_hours": 1.5 + (i % 5) * 0.3,
                    "fees_usd": 0.01 * (i % 9),
                    "divergence_usd": -0.02 * (i % 4),
                    "pnl_usd": 0.005 * (i % 6) - 0.01,
                    "efficiency": (i % 10) / 10.0,
                    "incomplete": False,
                }
            )
        return cycles

    def test_pnl_50_cycles_under_limit(self) -> None:
        cycles = self._fake_cycles(55)
        text = tg.render_pnl_message(cycles=cycles, recent_limit=10)
        self.assertLessEqual(len(text), TG_MAX_MESSAGE_LEN)
        self.assertIn("PnL по циклам", text)

    def test_pnl_huge_journal_still_under_limit(self) -> None:
        # Много разных ширин → сводка раздувается; укладка обязана удержать лимит.
        cycles = self._fake_cycles(200)
        text = tg.render_pnl_message(cycles=cycles, recent_limit=50)
        self.assertLessEqual(len(text), TG_MAX_MESSAGE_LEN)
        self.assertTrue(len(text) > 100)

    def test_empty_pnl(self) -> None:
        text = tg.render_pnl_message(cycles=[], recent_limit=10)
        self.assertIn("пуст", text)
        self.assertLessEqual(len(text), TG_MAX_MESSAGE_LEN)


if __name__ == "__main__":
    unittest.main()
