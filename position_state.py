"""Persist last opened position pubkey for Telegram convenience."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LAST_POSITION_PATH = ROOT / "state" / "last_position.json"


def save_last_position(pubkey: str, pool: str) -> None:
    LAST_POSITION_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pubkey": pubkey,
        "pool": pool,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    LAST_POSITION_PATH.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def load_last_position() -> str | None:
    if not LAST_POSITION_PATH.is_file():
        return None
    try:
        data = json.loads(LAST_POSITION_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    pk = data.get("pubkey")
    return str(pk) if pk else None


def clear_last_position() -> None:
    if LAST_POSITION_PATH.is_file():
        LAST_POSITION_PATH.unlink()
