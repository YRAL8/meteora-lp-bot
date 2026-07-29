"""Persisted 'closed but not reopened' flag for /rebalance crash recovery."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REOPEN_PENDING_PATH = ROOT / "state" / "reopen_pending.json"


def set_reopen_pending(pending: bool, *, meta: dict[str, Any] | None = None) -> None:
    REOPEN_PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not pending:
        if REOPEN_PENDING_PATH.is_file():
            REOPEN_PENDING_PATH.unlink()
        return
    payload = {
        "pending": True,
        "set_at": datetime.now(timezone.utc).isoformat(),
        **(meta or {}),
    }
    REOPEN_PENDING_PATH.write_text(
        json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
    )


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
