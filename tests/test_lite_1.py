#!/usr/bin/env python3
"""LITE_1: Python owns the send journal (@@JOURNAL + resolve RPC)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class JournalMarkerBeforeExitTests(unittest.TestCase):
    def test_marker_appends_pending_before_subprocess_ends(self) -> None:
        import exec_journal_io
        import meteora_exec

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            os.environ["METEORA_STATE_DIR"] = td
            release = td_path / "release_child"
            script = td_path / "fake_exec.py"
            script.write_text(
                "\n".join(
                    [
                        "import json, os, select, sys, time",
                        "from pathlib import Path",
                        "sig = 'Lite1MarkerSig' + ('1' * 40)",
                        "print(",
                        "  '@@JOURNAL ' + json.dumps({",
                        "    'stage': 'sending', 'index': 0,",
                        "    'signature': sig, 'kind': 'open',",
                        "  }),",
                        "  file=sys.stderr,",
                        "  flush=True,",
                        ")",
                        "ready, _, _ = select.select([sys.stdin], [], [], 15.0)",
                        "if not ready:",
                        "  raise SystemExit('handshake timeout')",
                        "ack = sys.stdin.readline().strip()",
                        "if ack != f'@@JOURNAL-OK {sig}':",
                        "  raise SystemExit(f'bad ack {ack!r}')",
                        "release = Path(os.environ['LITE1_RELEASE'])",
                        "for _ in range(200):",
                        "  if release.is_file():",
                        "    break",
                        "  time.sleep(0.05)",
                        "print(json.dumps({",
                        "  'ok': True,",
                        "  'action': 'exec-open',",
                        "  'network': 'devnet',",
                        "  'sends': [{",
                        "    'signature': sig, 'status': 'confirmed',",
                        "    'slot': 1, 'error': None,",
                        "  }],",
                        "}))",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            saw_pending = threading.Event()
            err: list[BaseException] = []

            def worker() -> None:
                try:
                    with patch.object(
                        meteora_exec, "_gate_journal_before_send", lambda **_k: None
                    ):
                        meteora_exec.run_exec(
                            ["exec-open"],
                            send=True,
                            force_ignore_journal=True,
                            network="devnet",
                            rpc="http://127.0.0.1:9",
                            timeout_s=30.0,
                            extra_env={"LITE1_RELEASE": str(release)},
                            executable=[sys.executable, "-u", str(script)],
                        )
                except BaseException as exc:  # noqa: BLE001 — collect for parent
                    err.append(exc)

            t = threading.Thread(target=worker)
            t.start()
            deadline = time.monotonic() + 10.0
            pending_while_alive = False
            while time.monotonic() < deadline:
                rows = exec_journal_io.read_entries()
                if any(
                    r.get("signature", "").startswith("Lite1MarkerSig")
                    and r.get("status") == "pending"
                    for r in rows
                ):
                    pending_while_alive = t.is_alive()
                    saw_pending.set()
                    release.write_text("go", encoding="utf-8")
                    break
                time.sleep(0.05)
            t.join(timeout=15.0)
            self.assertTrue(saw_pending.is_set(), "pending row never appeared")
            self.assertTrue(
                pending_while_alive,
                "pending must appear while child still running",
            )
            self.assertEqual(err, [], f"run_exec failed: {err}")
            final = exec_journal_io.list_unresolved()
            self.assertEqual(final, [], "confirmed send should clear unresolved")


class ResolveRpcClassificationTests(unittest.TestCase):
    def test_transient_rpc_leaves_unknown(self) -> None:
        import exec_journal_io
        import meteora_exec

        with tempfile.TemporaryDirectory() as td:
            os.environ["METEORA_STATE_DIR"] = td
            sig = "Lite1Transient" + ("2" * 40)
            exec_journal_io.append_entry(
                {
                    "ts": "2026-07-30T00:00:00Z",
                    "action": "exec-open",
                    "network": "devnet",
                    "params": {},
                    "signature": sig,
                    "status": "unknown",
                }
            )

            def boom(*_a: Any, **_k: Any) -> None:
                raise RuntimeError("fetch failed: ECONNRESET")

            with patch.object(meteora_exec, "_rpc_get_signature_statuses", boom):
                out = meteora_exec.resolve_unresolved_signatures(
                    rpc_url="http://127.0.0.1:9",
                    timeout_ms=80,
                    poll_ms=20,
                    wall_ms=200,
                    network="devnet",
                    sleep_fn=lambda _s: None,
                )
            self.assertTrue(out.get("ok"))
            rows = exec_journal_io.list_unresolved()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "unknown")
            self.assertEqual(rows[0]["signature"], sig)

    def test_confirmed_signature_updates_journal(self) -> None:
        import exec_journal_io
        import meteora_exec

        with tempfile.TemporaryDirectory() as td:
            os.environ["METEORA_STATE_DIR"] = td
            sig = "Lite1Confirmed" + ("3" * 40)
            exec_journal_io.append_entry(
                {
                    "ts": "2026-07-30T00:00:00Z",
                    "action": "exec-swap",
                    "network": "devnet",
                    "params": {},
                    "signature": sig,
                    "status": "pending",
                }
            )

            def ok(_rpc: str, _sig: str, **_k: Any) -> dict[str, Any]:
                return {
                    "err": None,
                    "confirmationStatus": "confirmed",
                    "slot": 4242,
                }

            with patch.object(meteora_exec, "_rpc_get_signature_statuses", ok):
                out = meteora_exec.resolve_unresolved_signatures(
                    rpc_url="http://127.0.0.1:9",
                    timeout_ms=1_000,
                    poll_ms=10,
                    wall_ms=5_000,
                    network="devnet",
                    sleep_fn=lambda _s: None,
                )
            self.assertEqual(out.get("cleared"), 1)
            self.assertEqual(exec_journal_io.list_unresolved(), [])
            latest = exec_journal_io.read_entries()[-1]
            self.assertEqual(latest["status"], "confirmed")
            self.assertEqual(latest["slot"], 4242)

    def test_is_transient_classifier(self) -> None:
        import meteora_exec

        self.assertTrue(meteora_exec.is_transient_rpc_error("fetch failed"))
        self.assertTrue(meteora_exec.is_transient_rpc_error("429 Too Many Requests"))
        self.assertTrue(meteora_exec.is_transient_rpc_error("ECONNRESET"))
        self.assertFalse(meteora_exec.is_transient_rpc_error("account not found"))

    def test_rpc_host_strips_api_key(self) -> None:
        import meteora_exec

        host = meteora_exec.rpc_host_for_log(
            "https://devnet.helius-rpc.com/?api-key=SECRET"
        )
        self.assertEqual(host, "devnet.helius-rpc.com")
        self.assertNotIn("SECRET", host)


class JournalIoHelpersTests(unittest.TestCase):
    def test_append_pending_from_marker(self) -> None:
        import exec_journal_io

        with tempfile.TemporaryDirectory() as td:
            os.environ["METEORA_STATE_DIR"] = td
            entry = exec_journal_io.append_pending_from_marker(
                {
                    "stage": "sending",
                    "index": 2,
                    "signature": "Lite1Append" + ("4" * 40),
                    "kind": "claim",
                },
                network="devnet",
            )
            self.assertEqual(entry["status"], "pending")
            self.assertEqual(entry["action"], "exec-claim-fees")
            self.assertEqual(entry["txIndex"], 2)


if __name__ == "__main__":
    unittest.main()
