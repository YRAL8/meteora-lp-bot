#!/usr/bin/env python3
"""LITE_1B: journal handshake — no journal row ⇒ no send."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_FAKE_EXEC = """\
import json, os, select, sys, time
from pathlib import Path

sig = os.environ["LITE1B_SIG"]
evidence = Path(os.environ["LITE1B_EVIDENCE"])
handshake_s = float(os.environ.get("METEORA_JOURNAL_HANDSHAKE_MS", "15000")) / 1000.0

print(
    "@@JOURNAL "
    + json.dumps(
        {"stage": "sending", "index": 0, "signature": sig, "kind": "open"}
    ),
    file=sys.stderr,
    flush=True,
)

ready, _, _ = select.select([sys.stdin], [], [], handshake_s)
if not ready:
    print(
        json.dumps(
            {
                "ok": False,
                "action": "exec-open",
                "error": "journal handshake timeout — not sent",
                "stage": "journal",
                "confirmationUnknown": False,
                "network": "devnet",
                "sends": [
                    {
                        "index": 0,
                        "signature": sig,
                        "status": "not_sent",
                        "sent": False,
                        "error": "journal-refused",
                    }
                ],
            }
        )
    )
    raise SystemExit(1)

line = sys.stdin.readline().strip()
if line != f"@@JOURNAL-OK {sig}":
    print(
        json.dumps(
            {
                "ok": False,
                "action": "exec-open",
                "error": "journal refused send — not sent",
                "stage": "journal",
                "confirmationUnknown": False,
                "network": "devnet",
                "sends": [
                    {
                        "index": 0,
                        "signature": sig,
                        "status": "not_sent",
                        "sent": False,
                        "error": "journal-refused",
                    }
                ],
            }
        )
    )
    raise SystemExit(1)

# Would be sendRawTransaction — leave a breadcrumb if we incorrectly get here.
evidence.write_text("sent", encoding="utf-8")
print(
    json.dumps(
        {
            "ok": True,
            "action": "exec-open",
            "network": "devnet",
            "sends": [
                {
                    "signature": sig,
                    "status": "confirmed",
                    "slot": 1,
                    "error": None,
                }
            ],
        }
    )
)
"""


def _write_fake(td: Path) -> Path:
    script = td / "fake_exec_handshake.py"
    script.write_text(_FAKE_EXEC, encoding="utf-8")
    return script


class JournalHandshakeRefuseTests(unittest.TestCase):
    def test_write_failure_does_not_send(self) -> None:
        import exec_journal_io
        import meteora_exec

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            os.environ["METEORA_STATE_DIR"] = td
            evidence = td_path / "sent.evidence"
            sig = "Lite1bFailWrite" + ("a" * 40)
            script = _write_fake(td_path)

            def boom(*_a: Any, **_k: Any) -> None:
                raise OSError("disk full")

            with patch.object(
                meteora_exec, "_gate_journal_before_send", lambda **_k: None
            ), patch.object(
                exec_journal_io, "append_pending_from_marker", boom
            ):
                with self.assertRaises(meteora_exec.MeteoraExecError) as ctx:
                    meteora_exec.run_exec(
                        ["exec-open"],
                        send=True,
                        force_ignore_journal=True,
                        network="devnet",
                        rpc="http://127.0.0.1:9",
                        timeout_s=30.0,
                        extra_env={
                            "LITE1B_SIG": sig,
                            "LITE1B_EVIDENCE": str(evidence),
                            "METEORA_JOURNAL_HANDSHAKE_MS": "5000",
                        },
                        executable=[sys.executable, "-u", str(script)],
                    )
            self.assertFalse(
                evidence.is_file(),
                "send breadcrumb must not exist when journal write fails",
            )
            payload = ctx.exception.payload
            self.assertFalse(payload.get("ok", True))
            sends = payload.get("sends") or []
            self.assertTrue(sends)
            self.assertEqual(sends[0].get("status"), "not_sent")
            self.assertEqual(sends[0].get("error"), "journal-refused")
            # No pending left unresolved (write never landed).
            self.assertEqual(exec_journal_io.list_unresolved(), [])

    def test_handshake_timeout_does_not_send(self) -> None:
        import exec_journal_io
        import meteora_exec

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            os.environ["METEORA_STATE_DIR"] = td
            evidence = td_path / "sent.evidence"
            sig = "Lite1bTimeout" + ("b" * 40)
            script = _write_fake(td_path)
            handshake_ms = "800"

            def silent_marker(
                line: str,
                *,
                network: str,
                reply: Any = None,
            ) -> None:
                # Parent sees the marker but never acks (and does not journal).
                if line.startswith("@@JOURNAL "):
                    return
                if line.strip():
                    print(line, file=sys.stderr)

            t0 = time.monotonic()
            with patch.object(
                meteora_exec, "_gate_journal_before_send", lambda **_k: None
            ), patch.object(
                meteora_exec, "_handle_journal_stderr_line", silent_marker
            ):
                with self.assertRaises(meteora_exec.MeteoraExecError) as ctx:
                    meteora_exec.run_exec(
                        ["exec-open"],
                        send=True,
                        force_ignore_journal=True,
                        network="devnet",
                        rpc="http://127.0.0.1:9",
                        timeout_s=30.0,
                        extra_env={
                            "LITE1B_SIG": sig,
                            "LITE1B_EVIDENCE": str(evidence),
                            "METEORA_JOURNAL_HANDSHAKE_MS": handshake_ms,
                        },
                        executable=[sys.executable, "-u", str(script)],
                    )
            elapsed = time.monotonic() - t0
            self.assertFalse(evidence.is_file(), "must not send on handshake timeout")
            self.assertGreaterEqual(elapsed, 0.7)
            self.assertLess(elapsed, 5.0, "should stop near handshake timeout")
            payload = ctx.exception.payload
            self.assertEqual(
                (payload.get("sends") or [{}])[0].get("status"), "not_sent"
            )
            self.assertEqual(exec_journal_io.list_unresolved(), [])

    def test_signature_mismatch_does_not_send(self) -> None:
        import exec_journal_io
        import meteora_exec

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            os.environ["METEORA_STATE_DIR"] = td
            evidence = td_path / "sent.evidence"
            sig = "Lite1bMismatch" + ("c" * 40)
            script = _write_fake(td_path)

            def wrong_sig_reply(
                line: str,
                *,
                network: str,
                reply: Any = None,
            ) -> None:
                if not line.startswith("@@JOURNAL "):
                    if line.strip():
                        print(line, file=sys.stderr)
                    return
                if reply is not None:
                    reply("@@JOURNAL-OK not-the-same-signature\n")

            with patch.object(
                meteora_exec, "_gate_journal_before_send", lambda **_k: None
            ), patch.object(
                meteora_exec, "_handle_journal_stderr_line", wrong_sig_reply
            ):
                with self.assertRaises(meteora_exec.MeteoraExecError) as ctx:
                    meteora_exec.run_exec(
                        ["exec-open"],
                        send=True,
                        force_ignore_journal=True,
                        network="devnet",
                        rpc="http://127.0.0.1:9",
                        timeout_s=30.0,
                        extra_env={
                            "LITE1B_SIG": sig,
                            "LITE1B_EVIDENCE": str(evidence),
                            "METEORA_JOURNAL_HANDSHAKE_MS": "5000",
                        },
                        executable=[sys.executable, "-u", str(script)],
                    )
            self.assertFalse(
                evidence.is_file(),
                "must not send when ack signature mismatches",
            )
            payload = ctx.exception.payload
            self.assertEqual(
                (payload.get("sends") or [{}])[0].get("status"), "not_sent"
            )
            self.assertEqual(
                (payload.get("sends") or [{}])[0].get("error"), "journal-refused"
            )
            self.assertEqual(exec_journal_io.list_unresolved(), [])


if __name__ == "__main__":
    unittest.main()
