"""Persisted auto-monitor timers (out-of-range + post-failure pause)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import state_paths

STATE_PATH = state_paths.path("monitor_timers.json")

log = logging.getLogger(__name__)


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        s = str(raw)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def load_timers() -> tuple[datetime | None, datetime | None]:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except Exception:
        log.warning("monitor_timers unreadable — starting fresh", exc_info=True)
        return None, None
    return _parse_dt(raw.get("out_of_range_since")), _parse_dt(
        raw.get("last_auto_attempt_at")
    )


def save_timers(
    out_of_range_since: datetime | None,
    last_auto_attempt_at: datetime | None,
) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "out_of_range_since": (
            out_of_range_since.astimezone(timezone.utc).isoformat()
            if out_of_range_since
            else None
        ),
        "last_auto_attempt_at": (
            last_auto_attempt_at.astimezone(timezone.utc).isoformat()
            if last_auto_attempt_at
            else None
        ),
    }
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)
