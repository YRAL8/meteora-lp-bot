"""Hourly public-pool snapshots. Not on the money path.

A timeout, 403, or garbage JSON must not touch monitoring or rebalance.
The file only grows (one JSON object per line).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

import requests

import bot_config
import state_paths

log = logging.getLogger(__name__)

POOL_URL_TMPL = "https://dlmm.datapi.meteora.ag/pools/{}"
USER_AGENT = "meteora-lp-bot (pool snapshots)"
INTERVAL_SEC = 3600
FILENAME = "pool_snapshots.jsonl"


def snapshots_path():
    return state_paths.path(FILENAME)


def _utc_iso(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _from_maybe_dict(value: Any, *keys: str) -> float | None:
    n = _num(value)
    if n is not None:
        return n
    if isinstance(value, dict):
        for key in keys:
            n = _num(value.get(key))
            if n is not None:
                return n
    return None


def extract_pool_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(inner, dict):
        raise ValueError("pool snapshot: body is not an object")
    active = inner.get("active_id")
    if active is None:
        active = inner.get("activeId")
    if isinstance(active, bool):
        active = None
    elif isinstance(active, (int, float)):
        active = int(active)
    else:
        active = None
    return {
        "tvl": _num(inner.get("tvl")),
        "volume_24h": _from_maybe_dict(
            inner.get("volume_24h") or inner.get("volume24h") or inner.get("volume"),
            "24h",
            "hour_24",
            "h24",
        ),
        "fees_24h": _from_maybe_dict(
            inner.get("fees_24h") or inner.get("fees24h") or inner.get("fees"),
            "24h",
            "hour_24",
            "h24",
        ),
        "apy": _num(inner.get("apy") if inner.get("apy") is not None else inner.get("apr")),
        "active_bin": active,
        "price": _num(
            inner.get("current_price")
            if inner.get("current_price") is not None
            else inner.get("usdcPerSol")
            if inner.get("usdcPerSol") is not None
            else inner.get("price")
        ),
    }


def _fetch_pool(address: str) -> dict[str, Any]:
    resp = requests.get(
        POOL_URL_TMPL.format(address),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError("pool snapshot: response is not an object")
    return payload


def _append_line(row: dict[str, Any]) -> None:
    path = snapshots_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def take_snapshot(
    *,
    pool: str | None = None,
    now: datetime | None = None,
    fetch: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write one JSONL row. Never raises."""
    addr = pool or bot_config.snapshot_pool()
    row: dict[str, Any] = {"ts": _utc_iso(now), "pool": addr}
    try:
        data = (fetch or _fetch_pool)(addr)
        row.update(extract_pool_snapshot(data))
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("pool snapshot failed: %s", row["error"])
    try:
        _append_line(row)
    except Exception:
        log.warning("pool snapshot write failed", exc_info=True)
    return row


async def snapshot_loop(
    *,
    sleep_fn=asyncio.sleep,
    interval_sec: float = INTERVAL_SEC,
) -> None:
    """Take a snapshot, then wait. Errors stay here; CancelledError propagates."""
    while True:
        try:
            take_snapshot()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("pool snapshot loop survived an error", exc_info=True)
        await sleep_fn(interval_sec)
