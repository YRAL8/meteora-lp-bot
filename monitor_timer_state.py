"""Persisted auto-monitor timers and storm counters.

Everything the monitor uses to decide *when* it may act again lives in one
file. The out-of-range clock used to survive a restart while the storm
counters did not, so a restart silently handed the bot a clean daily budget.
"""
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


DEFAULTS: dict[str, Any] = {
    "out_of_range_since": None,
    "last_auto_attempt_at": None,
    "last_rebalance_at": None,
    "rebalance_day_utc": "",
    "rebalance_count_today": 0,
    "daily_limit_notified_day": None,
}


def _read_raw() -> dict[str, Any]:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        log.warning("monitor_timers unreadable — starting fresh", exc_info=True)
        return {}
    return raw if isinstance(raw, dict) else {}


def load_all() -> dict[str, Any]:
    """All persisted timers. Missing keys (older file) fall back to defaults."""
    raw = _read_raw()
    out = dict(DEFAULTS)
    for key in ("out_of_range_since", "last_auto_attempt_at", "last_rebalance_at"):
        out[key] = _parse_dt(raw.get(key))
    day = raw.get("rebalance_day_utc")
    out["rebalance_day_utc"] = str(day) if day else ""
    try:
        out["rebalance_count_today"] = max(0, int(raw.get("rebalance_count_today") or 0))
    except (TypeError, ValueError):
        out["rebalance_count_today"] = 0
    notified = raw.get("daily_limit_notified_day")
    out["daily_limit_notified_day"] = str(notified) if notified else None
    return out


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def save_all(**fields: Any) -> None:
    """Write every timer. Unnamed fields keep whatever is already on disk."""
    current = load_all()
    current.update({k: v for k, v in fields.items() if k in DEFAULTS})
    payload = {
        "out_of_range_since": _iso(current["out_of_range_since"]),
        "last_auto_attempt_at": _iso(current["last_auto_attempt_at"]),
        "last_rebalance_at": _iso(current["last_rebalance_at"]),
        "rebalance_day_utc": current["rebalance_day_utc"],
        "rebalance_count_today": int(current["rebalance_count_today"]),
        "daily_limit_notified_day": current["daily_limit_notified_day"],
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def load_timers() -> tuple[datetime | None, datetime | None]:
    state = load_all()
    return state["out_of_range_since"], state["last_auto_attempt_at"]


def save_timers(
    out_of_range_since: datetime | None,
    last_auto_attempt_at: datetime | None,
) -> None:
    save_all(
        out_of_range_since=out_of_range_since,
        last_auto_attempt_at=last_auto_attempt_at,
    )
