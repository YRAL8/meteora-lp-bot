"""Persisted 'closed but not reopened' flag for /rebalance crash recovery."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path  # noqa: F401 — kept for type hints in callers
from typing import Any

import state_paths

# Tests may patch this Path.
REOPEN_PENDING_PATH = state_paths.path("reopen_pending.json")


class ReopenPendingWriteError(OSError):
    """Raised when reopen_pending cannot be persisted after a confirmed close."""


def set_reopen_pending(pending: bool, *, meta: dict[str, Any] | None = None) -> None:
    """Write or clear the flag. Propagates OSError (disk full / read-only)."""
    path = REOPEN_PENDING_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not pending:
            if path.is_file():
                path.unlink()
            return
        payload = {
            "pending": True,
            "set_at": datetime.now(timezone.utc).isoformat(),
            **(meta or {}),
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as e:
        raise ReopenPendingWriteError(
            f"cannot write reopen_pending at {path}: {e}"
        ) from e


def is_reopen_pending() -> bool:
    if not REOPEN_PENDING_PATH.is_file():
        return False
    try:
        data = json.loads(REOPEN_PENDING_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return True
    return bool(data.get("pending", True))


def load_reopen_pending() -> dict[str, Any] | None:
    if not REOPEN_PENDING_PATH.is_file():
        return None
    try:
        return json.loads(REOPEN_PENDING_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"pending": True, "corrupt": True}
