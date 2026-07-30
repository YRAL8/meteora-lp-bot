"""Single door for the exec send-journal (C13).

All Python readers/writers of ``exec_journal.jsonl`` must go through this module:
paths, lock, atomic rewrite. TypeScript mirror lives only in
``ts/src/journal.ts`` (same filenames under ``METEORA_STATE_DIR`` / ``state/``).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import journal_lock
import state_paths

JOURNAL_BASENAME = "exec_journal.jsonl"
LOCK_BASENAME = "exec_journal.lock"


def journal_path() -> Path:
    return state_paths.path(JOURNAL_BASENAME)


def lock_path() -> Path:
    return state_paths.path(LOCK_BASENAME)


def acquire(*, timeout_s: float = 60.0) -> None:
    journal_lock.acquire_journal_lock(lock_path(), timeout_s=timeout_s)


def release() -> None:
    journal_lock.release_journal_lock(lock_path())


def unique_tmp_path(path: Path | None = None) -> Path:
    p = path or journal_path()
    return p.parent / (
        f"{p.name}.{os.getpid()}."
        f"{int(datetime.now(timezone.utc).timestamp() * 1000)}.tmp"
    )


def read_entries(path: Path | None = None) -> list[dict[str, Any]]:
    jp = path or journal_path()
    if not jp.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in jp.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            entries.append(
                {
                    "signature": "",
                    "status": "failed",
                    "error": "corrupt journal line skipped",
                    "ts": "",
                }
            )
    return entries


def write_entries_holding_lock(
    entries: list[dict[str, Any]], path: Path | None = None
) -> None:
    """Rewrite journal; caller MUST hold ``acquire()`` already."""
    jp = path or journal_path()
    body = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)
    tmp = unique_tmp_path(jp)
    jp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(body + ("\n" if body else ""), encoding="utf-8")
    tmp.replace(jp)


def write_entries(entries: list[dict[str, Any]], path: Path | None = None) -> None:
    """Rewrite under the shared lock (unique temp file)."""
    acquire()
    try:
        write_entries_holding_lock(entries, path)
    finally:
        release()


def list_unresolved(path: Path | None = None) -> list[dict[str, Any]]:
    by_sig: dict[str, dict[str, Any]] = {}
    for e in read_entries(path):
        sig = str(e.get("signature") or "")
        if not sig:
            continue
        by_sig[sig] = e
    return [
        e for e in by_sig.values() if e.get("status") in ("pending", "unknown")
    ]
