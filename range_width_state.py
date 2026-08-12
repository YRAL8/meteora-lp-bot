"""Persisted /setrange width (survives container restart)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import range_state
import state_paths

log = logging.getLogger(__name__)

STATE_PATH = state_paths.path("range_width.json")


@dataclass
class RangeRestoreResult:
    """Outcome of restore_at_startup for the Telegram boot message."""

    source: str  # "state" | "env"
    pct: float
    half: int
    warning: str | None = None


def save_range_width(pct: float, half: int) -> None:
    """Atomic write of the active range (same pattern as monitor_timers)."""
    path = STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "range_width_pct": float(pct),
        "half_width_bins": int(half),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def _read_file() -> dict[str, Any] | None:
    path = STATE_PATH
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("range_width.json unreadable: %s", e)
        return {"_corrupt": True, "error": str(e)}
    if not isinstance(data, dict):
        return {"_corrupt": True, "error": "not an object"}
    return data


def restore_at_startup(
    *,
    bin_step: int,
    max_bins_per_position: int,
) -> RangeRestoreResult:
    """Load saved width or keep .env defaults. Never raises for bad files.

    File missing → leave module state as imported from env (today's behaviour).
    File present but invalid for this pool → keep env and set ``warning``.
    """
    env_pct = range_state.current_range_pct()
    env_half = range_state.current_half_width()
    raw = _read_file()
    if raw is None:
        return RangeRestoreResult(source="env", pct=env_pct, half=env_half)

    if raw.get("_corrupt"):
        return RangeRestoreResult(
            source="env",
            pct=env_pct,
            half=env_half,
            warning=(
                "сохранённая ширина повреждена — взял RANGE_WIDTH_PCT из .env"
            ),
        )

    try:
        pct = float(raw["range_width_pct"])
        half_saved = int(raw.get("half_width_bins") or 0)
    except (KeyError, TypeError, ValueError):
        return RangeRestoreResult(
            source="env",
            pct=env_pct,
            half=env_half,
            warning=(
                "сохранённая ширина нечитаема — взял RANGE_WIDTH_PCT из .env"
            ),
        )

    try:
        half = range_state.half_width_for_pct(
            pct, bin_step, max_bins_per_position=max_bins_per_position
        )
    except (ValueError, range_state.RangeTooWideError) as e:
        return RangeRestoreResult(
            source="env",
            pct=env_pct,
            half=env_half,
            warning=(
                f"сохранённая ширина ±{pct:g}% не подходит этому пулу "
                f"({e}) — взял RANGE_WIDTH_PCT из .env"
            ),
        )

    # Prefer recomputed half (pool may have changed binStep since save);
    # keep file's half only when it matches the pct conversion.
    if half_saved and half_saved != half:
        log.info(
            "range_width.json half=%s vs recomputed %s for pct=%s — using recomputed",
            half_saved,
            half,
            pct,
        )

    range_state.range_width_pct = float(pct)
    range_state.half_width_bins = int(half)
    return RangeRestoreResult(source="state", pct=float(pct), half=int(half))
