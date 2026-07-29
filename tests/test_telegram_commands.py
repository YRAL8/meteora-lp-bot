#!/usr/bin/env python3
"""Offline tests for Telegram command handlers (C1 + C_PARITY)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot_config  # noqa: E402
import bot_state  # noqa: E402
import telegram_commands as tg  # noqa: E402


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


def _update_with_args(args: list[str]):
    msg = _FakeMessage()
    update = MagicMock()
    update.effective_message = msg
    ctx = MagicMock()
    ctx.args = args
    return update, msg, ctx


class TelegramCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_without_args_rejects(self) -> None:
        update, msg, ctx = _update_with_args([])
        with patch("money_ops.open_with_budget") as mock_exec:
            await tg.open_command(update, ctx)
            mock_exec.assert_not_called()
        self.assertTrue(any("Использование" in r for r in msg.replies))

    async def test_open_negative_amount_rejects(self) -> None:
        update, msg, ctx = _update_with_args(["-1"])
        with patch("money_ops.open_with_budget") as mock_exec:
            await tg.open_command(update, ctx)
            mock_exec.assert_not_called()
        self.assertTrue(any("больше 0" in r or "❌" in r for r in msg.replies))

    async def test_dry_run_forces_devnet_in_exec_kwargs(self) -> None:
        with patch.object(bot_config, "DRY_RUN", True):
            kw = tg._exec_kwargs()
        self.assertEqual(kw["network"], "devnet")

    async def test_dry_run_ignores_mainnet_env(self) -> None:
        with patch.object(bot_config, "DRY_RUN", True):
            with patch.dict(os.environ, {"METEORA_ALLOW_MAINNET": "1"}):
                kw = tg._exec_kwargs()
        self.assertEqual(kw["network"], "devnet")

    async def test_frozen_blocks_open(self) -> None:
        bot_state.bot_frozen = True
        try:
            update, msg, ctx = _update_with_args(["5"])
            with patch("money_ops.open_with_budget") as mock_exec:
                await tg.open_command(update, ctx)
                mock_exec.assert_not_called()
            self.assertTrue(any("заморожен" in r for r in msg.replies))
        finally:
            bot_state.bot_frozen = False


if __name__ == "__main__":
    unittest.main()
