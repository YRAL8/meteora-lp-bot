"""Persisted counters for auto-rebalance storm guards (C4)."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

import state_paths  # noqa: E402

# Tests may patch this; otherwise follow METEORA_STATE_DIR / state_paths.
STATE_PATH = state_paths.path("auto_rebalance.json")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day_key(dt: datetime | None = None) -> str:
    dt = dt or _utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)


@dataclass
class AutoRebalanceState:
    day_utc: str = ""
    count_today: int = 0
    last_rebalance_at: str | None = None
    daily_limit_notified_day: str | None = None
    uneconomic_warned_at: str | None = None

    def ensure_day(self, now: datetime | None = None) -> None:
        key = _day_key(now)
        if self.day_utc != key:
            self.day_utc = key
            self.count_today = 0
            # daily_limit_notified_day stays — compared against today's key


def load_state(
    path: Path | None = None, *, now: datetime | None = None
) -> AutoRebalanceState:
    p = path or STATE_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        st = AutoRebalanceState(day_utc=_day_key(now))
        return st
    except Exception:
        log.warning("auto_rebalance state unreadable — starting fresh", exc_info=True)
        return AutoRebalanceState(day_utc=_day_key(now))
    st = AutoRebalanceState(
        day_utc=str(raw.get("day_utc") or _day_key(now)),
        count_today=int(raw.get("count_today") or 0),
        last_rebalance_at=raw.get("last_rebalance_at"),
        daily_limit_notified_day=raw.get("daily_limit_notified_day"),
        uneconomic_warned_at=raw.get("uneconomic_warned_at"),
    )
    st.ensure_day(now)
    return st


def save_state(st: AutoRebalanceState, path: Path | None = None) -> None:
    p = path or STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(asdict(st), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(p)


def minutes_since_last(st: AutoRebalanceState, now: datetime | None = None) -> float | None:
    last = _parse(st.last_rebalance_at)
    if last is None:
        return None
    now = now or _utc_now()
    return (now - last).total_seconds() / 60.0


def record_rebalance(st: AutoRebalanceState, now: datetime | None = None) -> AutoRebalanceState:
    now = now or _utc_now()
    st.ensure_day(now)
    st.count_today += 1
    st.last_rebalance_at = _iso(now)
    save_state(st)
    return st


def mark_daily_limit_notified(st: AutoRebalanceState, now: datetime | None = None) -> None:
    now = now or _utc_now()
    st.ensure_day(now)
    st.daily_limit_notified_day = st.day_utc
    save_state(st)


def mark_uneconomic_warned(st: AutoRebalanceState, now: datetime | None = None) -> None:
    now = now or _utc_now()
    st.uneconomic_warned_at = _iso(now)
    save_state(st)


def should_warn_uneconomic(
    st: AutoRebalanceState, *, reminder_hours: float, now: datetime | None = None
) -> bool:
    now = now or _utc_now()
    prev = _parse(st.uneconomic_warned_at)
    if prev is None:
        return True
    return (now - prev).total_seconds() / 3600.0 >= reminder_hours


def median_cycle_hours(cycles: list[dict[str, Any]]) -> float | None:
    vals = [
        float(c["duration_hours"])
        for c in cycles
        if c.get("duration_hours") is not None and not c.get("incomplete")
    ]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0
