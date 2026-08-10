#!/usr/bin/env python3
"""C13: exec journal must be written only through the single door modules."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_BAD_LOCK = re.compile(r"exec_journal\.jsonl\.lock")
_JOURNAL_NAME = re.compile(r"exec_journal\.jsonl")

# Split so this file does not match _BAD_LOCK itself.
_BAD_LOCK_EXAMPLE = "exec_journal.jsonl" + ".lock"

_PY_ALLOWED = {
    "exec_journal_io.py",
    "journal_lock.py",
    "meteora_exec.py",
}


class JournalDoorGuardTests(unittest.TestCase):
    def test_no_wrong_lock_basename_anywhere(self) -> None:
        offenders: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in {".py", ".ts", ".js"}:
                continue
            if "node_modules" in path.parts or "/dist/" in str(path):
                continue
            if path.name.startswith(("REPORT_", "AUDIT", "TASK_")):
                continue
            if path.name == "test_c13_journal_door.py":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if _BAD_LOCK.search(text):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            offenders,
            [],
            "forbidden lock basename "
            + _BAD_LOCK_EXAMPLE
            + " in: "
            + ", ".join(offenders),
        )

    def test_python_root_modules_do_not_hardcode_journal(self) -> None:
        offenders: list[str] = []
        for path in ROOT.glob("*.py"):
            if path.name in _PY_ALLOWED:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if _JOURNAL_NAME.search(text):
                offenders.append(path.name)
        self.assertEqual(
            offenders,
            [],
            "exec_journal.jsonl outside door modules: " + ", ".join(offenders),
        )

    def test_ts_has_no_journal_basenames(self) -> None:
        """LITE_1: TypeScript must not name the journal or lock files."""
        offenders: list[str] = []
        src = ROOT / "ts" / "src"
        if not src.is_dir():
            return
        for path in src.glob("*.ts"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "exec_journal.jsonl" in text or "exec_journal.lock" in text:
                offenders.append(path.name)
        self.assertEqual(
            offenders,
            [],
            "TS must not reference journal basenames: " + ", ".join(offenders),
        )
        self.assertFalse(
            (src / "journal.ts").is_file(),
            "ts/src/journal.ts must be deleted (Python owns the journal)",
        )

    def test_python_door_lock_basename(self) -> None:
        import exec_journal_io

        self.assertEqual(exec_journal_io.LOCK_BASENAME, "exec_journal.lock")
        self.assertEqual(exec_journal_io.JOURNAL_BASENAME, "exec_journal.jsonl")
        self.assertEqual(exec_journal_io.lock_path().name, "exec_journal.lock")


if __name__ == "__main__":
    unittest.main()
