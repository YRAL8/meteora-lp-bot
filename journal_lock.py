"""Python-only journal lock via O_EXCL lockfile (C12 / LITE_1).

Processes that write ``exec_journal.jsonl`` through ``exec_journal_io`` create
``exec_journal.lock`` exclusively, write pid, and unlink on release. Stale
locks (dead pid) are broken after a short wait.
"""
from __future__ import annotations

import os
import time
from pathlib import Path


def acquire_journal_lock(lock_path: Path, *, timeout_s: float = 60.0) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
            finally:
                os.close(fd)
            return
        except FileExistsError:
            if _break_stale_lock(lock_path):
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"journal lock timeout: {lock_path}")
            time.sleep(0.05)


def release_journal_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def _break_stale_lock(lock_path: Path) -> bool:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
        pid = int(raw.splitlines()[0])
    except (OSError, ValueError, IndexError):
        try:
            lock_path.unlink()
            return True
        except OSError:
            return False
    try:
        os.kill(pid, 0)
        return False  # holder alive
    except ProcessLookupError:
        try:
            lock_path.unlink()
            return True
        except OSError:
            return False
    except PermissionError:
        return False
