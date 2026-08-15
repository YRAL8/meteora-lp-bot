#!/usr/bin/env python3
"""LITE_15: pause/freeze survive restart; logs; journal fields; pool snapshots."""
from __future__ import annotations

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

    def test_boot_line_is_from_code_not_prose(self) -> None:
        import bot_mode_state

        text = bot_mode_state.format_boot_mode_line(
            paused=True, frozen=False, source="file"
        )
        self.assertIn("/pauza", text)
        self.assertIn("пауз", text.lower())


if __name__ == "__main__":
    unittest.main()
