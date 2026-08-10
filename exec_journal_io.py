"""Single door for the exec send-journal (C13 / LITE_1).

All readers/writers of ``exec_journal.jsonl`` must go through this module:
paths, lock, atomic rewrite. Ownership is Python-only; TypeScript emits
``@@JOURNAL`` markers on stderr and never touches these files.
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

UNRESOLVED_STATUSES = frozenset({"pending", "unknown"})


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


def append_entry(
    entry: dict[str, Any],
    path: Path | None = None,
    *,
    lock_timeout_s: float = 60.0,
) -> None:
    """Append one JSONL row under the lock."""
    jp = path or journal_path()
    jp.parent.mkdir(parents=True, exist_ok=True)
    acquire(timeout_s=lock_timeout_s)
    try:
        with jp.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    finally:
        release()


def update_by_signature(
    signature: str,
    patch: dict[str, Any],
    path: Path | None = None,
) -> None:
    """Patch the latest row for ``signature``. Raises if missing."""
    jp = path or journal_path()
    acquire()
    try:
        entries = read_entries(jp)
        found = False
        for i in range(len(entries) - 1, -1, -1):
            if str(entries[i].get("signature") or "") == signature:
                entries[i] = {**entries[i], **patch}
                found = True
                break
        if not found:
            raise KeyError(f"journal entry not found for signature {signature}")
        write_entries_holding_lock(entries, jp)
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
        e for e in by_sig.values() if e.get("status") in UNRESOLVED_STATUSES
    ]


def action_for_kind(kind: str) -> str:
    k = (kind or "").strip().lower()
    if k == "claim":
        return "exec-claim-fees"
    if k in ("open", "close", "swap", "add", "withdraw"):
        return f"exec-{k}"
    return "exec-open"


# Marker handshake must not wait longer than the child's ack timeout / blockhash life.
MARKER_LOCK_TIMEOUT_S = 5.0


def append_pending_from_marker(
    marker: dict[str, Any],
    *,
    network: str,
    path: Path | None = None,
    lock_timeout_s: float = MARKER_LOCK_TIMEOUT_S,
) -> dict[str, Any]:
    """Build and append a pending row from a parsed @@JOURNAL payload.

    Uses a short lock wait (default 5s): lock timeout → caller sends
    ``@@JOURNAL-FAIL`` so the child never broadcasts without a journal row.
    """
    kind = str(marker.get("kind") or "open")
    signature = str(marker.get("signature") or "")
    if not signature:
        raise ValueError("@@JOURNAL marker missing signature")
    index = marker.get("index")
    try:
        tx_index = int(index) if index is not None else None
    except (TypeError, ValueError):
        tx_index = None
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z",
        "action": action_for_kind(kind),
        "network": network,
        "params": {"txIndex": tx_index} if tx_index is not None else {},
        "signature": signature,
        "status": "pending",
        "slot": None,
        "error": None,
    }
    if tx_index is not None:
        entry["txIndex"] = tx_index
    append_entry(entry, path, lock_timeout_s=lock_timeout_s)
    return entry
