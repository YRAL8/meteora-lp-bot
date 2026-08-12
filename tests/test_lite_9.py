#!/usr/bin/env python3
"""LITE_9: durable bot log, command/decision traces, bigint filter, no secrets."""
from __future__ import annotations

import io
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot_config  # noqa: E402
import bot_logging  # noqa: E402
import bot_state  # noqa: E402
import meteora_ops  # noqa: E402
import telegram_commands as tg  # noqa: E402
import telegram_notify  # noqa: E402

BAIT_TG_TOKEN = "123456789:BAIT_TELEGRAM_TOKEN_xyz789ABC"
BAIT_API_KEY = "BAIT_HELIUS_API_KEY_9f3a7c2e"
BAIT_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={BAIT_API_KEY}"
BAIT_WALLET = "BAIT_WALLET_SECRET_BYTES_DO_NOT_LOG"
BIGINT_LINE = meteora_ops.BIGINT_NOISE_LINE


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


def _update(args: list[str]):
    msg = _FakeMessage()
    update = MagicMock()
    update.effective_message = msg
    ctx = MagicMock()
    ctx.args = args
    return update, msg, ctx


def _capture_formatted(emit) -> str:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(bot_logging.RedactingFormatter(bot_logging.LOG_FORMAT))
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    old = root.level
    root.setLevel(logging.DEBUG)
    try:
        emit()
        handler.flush()
        return buf.getvalue()
    finally:
        root.removeHandler(handler)
        root.setLevel(old)


class CommandLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_negative_logs_accept_and_refuse(self) -> None:
        update, msg, ctx = _update(["-1"])
        with self.assertLogs(tg.log, level="INFO") as cm:
            await tg.open_command(update, ctx)
        joined = "\n".join(cm.output)
        self.assertIn("accepted command open args=['-1']", joined)
        self.assertIn("command open refused:", joined)
        self.assertTrue(any("больше 0" in r or "❌" in r for r in msg.replies))

    async def test_pauza_logs_accept_and_ok(self) -> None:
        bot_state.bot_paused = False
        try:
            update, msg, ctx = _update([])
            with self.assertLogs(tg.log, level="INFO") as cm:
                await tg.pauza_command(update, ctx)
            joined = "\n".join(cm.output)
            self.assertIn("accepted command pauza args=[]", joined)
            self.assertIn("command pauza ok", joined)
        finally:
            bot_state.bot_paused = False


class FileLogTests(unittest.TestCase):
    def tearDown(self) -> None:
        bot_logging.detach_state_file_log()

    def test_unwritable_state_dir_does_not_crash(self) -> None:
        bot_logging.detach_state_file_log()
        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / "notdir"
            blocker.write_text("x")
            with patch("state_paths.state_dir", return_value=blocker):
                with self.assertLogs("bot_logging", level="WARNING") as cm:
                    ok = bot_logging.attach_state_file_log()
            self.assertFalse(ok)
            self.assertTrue(
                any("state log file unavailable" in line for line in cm.output)
            )

    def test_file_handler_writes_same_format(self) -> None:
        bot_logging.detach_state_file_log()
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"METEORA_STATE_DIR": td}):
                self.assertTrue(bot_logging.attach_state_file_log())
                logging.getLogger("test_lite_9").info("durable-line-probe")
                for handler in logging.getLogger().handlers:
                    if getattr(handler, bot_logging._FILE_HANDLER_MARK, False):
                        handler.flush()
                text = (Path(td) / "bot.log").read_text(encoding="utf-8")
        self.assertIn("durable-line-probe", text)
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2} .* \[INFO\] durable-line-probe")


class BigintFilterTests(unittest.TestCase):
    def test_exact_line_dropped_neighbor_kept(self) -> None:
        buf = io.StringIO()
        meteora_ops.emit_cli_stderr(
            f"{BIGINT_LINE}\n"
            "real error: connection refused\n"
            f"{BIGINT_LINE}\n",
            file=buf,
        )
        out = buf.getvalue()
        self.assertNotIn("bigint", out)
        self.assertIn("real error: connection refused", out)

    def test_near_miss_is_not_swallowed(self) -> None:
        extra = BIGINT_LINE + " AND also Error: boom"
        buf = io.StringIO()
        meteora_ops.emit_cli_stderr(extra + "\n", file=buf)
        self.assertIn("Error: boom", buf.getvalue())
        self.assertIn("bigint", buf.getvalue())
        self.assertFalse(meteora_ops.is_bigint_noise_line(extra))
        self.assertTrue(meteora_ops.is_bigint_noise_line("  " + BIGINT_LINE + "  "))


class SecretLeakTests(unittest.TestCase):
    def _emit_typical(self, keypair_text: str) -> None:
        import main as main_mod

        host = main_mod._rpc_host_for_log(BAIT_RPC_URL)
        logging.getLogger("main").info(
            "Meteora LP-бот | DRY_RUN=%s network=%s AUTO_REBALANCE=%s "
            "DELAY=%smin INTERVAL=%smin MAX/day=%s pool=%s rpc=%s",
            True,
            "mainnet",
            True,
            20,
            60,
            6,
            "Pool111",
            host,
        )
        logging.getLogger("telegram_commands").info(
            "accepted command open args=['10', 'confirm']"
        )
        try:
            raise RuntimeError(f"RPC 401 Unauthorized {BAIT_RPC_URL}")
        except Exception:
            logging.getLogger("main").exception("Ошибка мониторинга (тик пропущен)")
        try:
            raise RuntimeError(
                f'server said {{"error":"invalid api-key {BAIT_API_KEY}"}}'
            )
        except Exception:
            logging.getLogger("meteora_ops").exception("cli failed")
        try:
            raise ValueError(f"unexpected keypair format: {keypair_text}")
        except Exception:
            logging.getLogger("bot_config").exception("wallet load failed")

        class Boom(Exception):
            pass

        with patch.object(bot_config, "TELEGRAM_BOT_TOKEN", BAIT_TG_TOKEN), patch.object(
            bot_config, "TELEGRAM_CHAT_ID", "4242"
        ), patch(
            "telegram_notify.requests.post",
            side_effect=Boom(
                f"https://api.telegram.org/bot{BAIT_TG_TOKEN}/sendMessage"
            ),
        ):
            telegram_notify.send_telegram_message("hello")

    def test_typical_records_do_not_contain_baits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            kp = Path(td) / "wallet.json"
            kp.write_text(f'[1, 2, "{BAIT_WALLET}"]\n', encoding="utf-8")
            keypair_text = kp.read_text(encoding="utf-8")
            with patch.object(bot_config, "TELEGRAM_BOT_TOKEN", BAIT_TG_TOKEN), patch.object(
                bot_config, "WALLET_KEYPAIR_PATH", str(kp)
            ), patch.object(
                bot_config, "effective_rpc", return_value=BAIT_RPC_URL
            ), patch.dict(
                os.environ,
                {"SOLANA_RPC_URL": BAIT_RPC_URL, "WALLET_KEYPAIR_PATH": str(kp)},
            ):
                text = _capture_formatted(lambda: self._emit_typical(keypair_text))
        self.assertIn("rpc=mainnet.helius-rpc.com", text)
        self.assertNotIn(BAIT_TG_TOKEN, text)
        self.assertNotIn(BAIT_API_KEY, text)
        self.assertNotIn(BAIT_WALLET, text)
        self.assertNotIn(BAIT_RPC_URL, text)
        self.assertNotIn(f"?api-key={BAIT_API_KEY}", text)


class BreakProbeTests(unittest.TestCase):
    def test_redaction_is_what_strips_baits(self) -> None:
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="rpc=%s token=%s",
            args=(BAIT_RPC_URL, BAIT_TG_TOKEN),
            exc_info=None,
        )
        fmt = bot_logging.RedactingFormatter(bot_logging.LOG_FORMAT)
        with patch.object(bot_config, "TELEGRAM_BOT_TOKEN", BAIT_TG_TOKEN), patch.dict(
            os.environ, {"SOLANA_RPC_URL": BAIT_RPC_URL}
        ), patch.object(bot_config, "effective_rpc", return_value=BAIT_RPC_URL):
            self.assertNotIn(BAIT_API_KEY, fmt.format(record))
        with patch("bot_logging.redact_secrets", side_effect=lambda t: t):
            leaked = fmt.format(record)
        self.assertIn(BAIT_API_KEY, leaked)
        self.assertIn(BAIT_TG_TOKEN, leaked)


if __name__ == "__main__":
    unittest.main()
