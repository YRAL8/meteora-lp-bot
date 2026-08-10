#!/usr/bin/env python3
"""LITE_3B: real Node exec.js must exit after journal handshake (stdin.pause)."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXEC_JS = ROOT / "ts" / "dist" / "exec.js"


@unittest.skipUnless(EXEC_JS.is_file(), "ts/dist/exec.js not built — skip")
class ExecJsHandshakeExitTests(unittest.TestCase):
    def _run_self_test(self, *, ack_line: str, signature: str = "TESTSIG") -> dict:
        cmd = [
            "node",
            str(EXEC_JS),
            "handshake-self-test",
            "--signature",
            signature,
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        t0 = time.monotonic()
        # Wait for @@JOURNAL marker, then ack — same order as meteora_exec.
        marker_deadline = time.monotonic() + 2.0
        saw_marker = False
        while time.monotonic() < marker_deadline:
            line = proc.stderr.readline()
            if not line:
                break
            if line.startswith("@@JOURNAL "):
                saw_marker = True
                break
        self.assertTrue(saw_marker, "child never emitted @@JOURNAL marker")
        try:
            proc.stdin.write(ack_line if ack_line.endswith("\n") else ack_line + "\n")
            proc.stdin.flush()
            # Leave stdin open — mirrors meteora_exec (pipe held until child exits).
        except BrokenPipeError:
            pass
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)
            self.fail(
                "node handshake-self-test did not exit within 5s "
                "(stdin still holding the event loop — need pause+unref)"
            )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 5.0, f"exit too slow: {elapsed:.2f}s")
        self.assertIsNotNone(proc.returncode)
        try:
            out = proc.stdout.read()
            err_rest = proc.stderr.read()
        finally:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None and not pipe.closed:
                    try:
                        pipe.close()
                    except OSError:
                        pass
        line = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
        self.assertTrue(line, f"empty stdout; stderr_rest={err_rest!r}")
        payload = json.loads(line)
        self.assertEqual(payload.get("action"), "handshake-self-test")
        return payload

    def test_ok_ack_process_exits(self) -> None:
        payload = self._run_self_test(ack_line="@@JOURNAL-OK TESTSIG")
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("journalOk"))

    def test_fail_ack_process_exits(self) -> None:
        payload = self._run_self_test(ack_line="@@JOURNAL-FAIL TESTSIG")
        self.assertFalse(payload.get("ok"))
        self.assertFalse(payload.get("journalOk"))


if __name__ == "__main__":
    unittest.main()
