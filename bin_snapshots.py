"""Hourly on-chain snapshots of where the pool's money sits. Not on the money path.

Why this exists: a cycle's divergence is set by how much value sits in one bin at
the price, not by how wide we opened. Coverage — fees against divergence — is

    coverage = fees_per_day / (4 * value_per_bin / bin_step_frac * sigma^2 / 8)

so ``value_per_bin_usd`` below is the missing half of every result the journal
records. Without it a month of cycles says how much was earned, never why.

Read-only: pool metadata over HTTP, accounts over RPC. No keys, no signing.
A timeout, a 403, or garbage must not touch monitoring or rebalance.
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
from datetime import datetime, timezone
from typing import Any, Callable

import requests

import bot_config
import config
import meteora_position as mp
import state_paths

log = logging.getLogger(__name__)

POOL_URL_TMPL = "https://dlmm.datapi.meteora.ag/pools/{}"
USER_AGENT = "meteora-lp-bot (bin snapshots)"
INTERVAL_SEC = 3600
FILENAME = "bin_snapshots.jsonl"
PUBKEY_CACHE = "bin_array_pubkeys.json"

# ±175 bins. At bin step 4 that is ±7%: far wider than one position can be
# (70 bins, ±1.37%), and wide enough to see the shape thin out at the edges.
SPAN_BINS = 175
BUCKET_BINS = 25
BANDS_PCT = (0.5, 1.0, 2.0, 3.0, 5.0)
NEAR_BINS = 5  # value_per_bin is the median over the active bin ± this many


def snapshots_path():
    return state_paths.path(FILENAME)


def pubkey_cache_path():
    return state_paths.path(PUBKEY_CACHE)


def _utc_iso(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---- pure math (offline-testable) ----


def bin_price(bin_id: int, *, bin_step: int, dec_x: int, dec_y: int) -> float:
    """Price of token_y per token_x in UI units for a bin."""
    return (1.0 + bin_step / 1e4) ** bin_id * (10 ** (dec_x - dec_y))


def concentration(value_per_bin_usd: float, *, bin_step: int, tvl: float) -> float | None:
    """How many times a dollar here diverges faster than in a full-range pool.

    A full-range constant-product pool holds ``V/4`` of value per unit of
    log-price; one bin spans ``bin_step/1e4`` of it, so a pool whose bins hold
    ``v`` each behaves like ``4*v/(h*V)`` full-range pools stacked together.
    """
    h = bin_step / 1e4
    if tvl <= 0 or h <= 0:
        return None
    return 4.0 * value_per_bin_usd / (h * tvl)


def summarize(
    bins: dict[int, float],
    *,
    active_id: int,
    bin_step: int,
    tvl: float,
) -> dict[str, Any]:
    """Aggregate {bin_id: value_usd} into the numbers a month of cycles needs."""
    near = [bins.get(b, 0.0) for b in range(active_id - NEAR_BINS, active_id + NEAR_BINS + 1)]
    value_per_bin = statistics.median(near) if near else 0.0

    bands: dict[str, Any] = {}
    for pct in BANDS_PCT:
        width = max(1, int(round(pct / 100.0 / (bin_step / 1e4))))
        ids = range(active_id - width, active_id + width + 1)
        vals = [bins.get(b, 0.0) for b in ids]
        total = sum(vals)
        band_m = concentration(total / len(vals), bin_step=bin_step, tvl=tvl)
        bands[f"{pct:g}"] = {
            "bins": len(vals),
            "occupied": sum(1 for v in vals if v > 0),
            "value_usd": round(total, 2),
            "share_of_tvl": round(total / tvl, 6) if tvl > 0 else None,
            "m": None if band_m is None else round(band_m, 3),
        }

    lo = active_id - SPAN_BINS
    buckets = []
    for start in range(lo, active_id + SPAN_BINS + 1, BUCKET_BINS):
        buckets.append(round(sum(bins.get(b, 0.0) for b in range(start, start + BUCKET_BINS)), 2))

    m_at_price = concentration(value_per_bin, bin_step=bin_step, tvl=tvl)
    return {
        "value_per_bin_usd": round(value_per_bin, 2),
        "m_at_price": None if m_at_price is None else round(m_at_price, 3),
        "occupied_bins": sum(1 for v in bins.values() if v > 0),
        "value_in_span_usd": round(sum(bins.values()), 2),
        "bands": bands,
        "buckets": {"from_bin": lo, "width_bins": BUCKET_BINS, "values": buckets},
    }


# ---- reading the world ----


def _fetch_pool(address: str) -> dict[str, Any]:
    resp = requests.get(
        POOL_URL_TMPL.format(address),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError("bin snapshot: response is not an object")
    return payload


def _load_pubkey_cache(pool: str) -> dict[int, str]:
    try:
        raw = json.loads(pubkey_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict) or raw.get("pool") != pool:
        return {}
    out: dict[int, str] = {}
    for k, v in (raw.get("indexes") or {}).items():
        try:
            out[int(k)] = str(v)
        except Exception:
            continue
    return out


def _save_pubkey_cache(pool: str, indexes: dict[int, str]) -> None:
    path = pubkey_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"pool": pool, "indexes": {str(k): v for k, v in indexes.items()}}),
        encoding="utf-8",
    )
    tmp.replace(path)


def read_bins(
    session: requests.Session,
    *,
    pool: str,
    rpc_url: str,
    active_id: int,
    bin_step: int,
    price: float,
    dec_x: int,
    dec_y: int,
    sleep_s: float,
) -> dict[int, float]:
    """{bin_id: value in USD} for the bins around the price.

    Bin-array addresses are cached: ``getProgramAccounts`` is expensive and the
    addresses never change, so it runs only when the price walks into an array
    this pool has not shown us before.
    """
    lo, hi = active_id - SPAN_BINS, active_id + SPAN_BINS
    want = list(range(lo // mp.BINS_PER_BIN_ARRAY, hi // mp.BINS_PER_BIN_ARRAY + 1))

    cache = _load_pubkey_cache(pool)
    if any(i not in cache for i in want):
        found = mp.fetch_binarrays_for_lb_pair_indexes(
            session, rpc_url, pool, want_indexes=want, sleep_s=sleep_s
        )
        if found:
            cache.update({int(k): v for k, v in found.items()})
            _save_pubkey_cache(pool, cache)

    pubkeys = [cache[i] for i in want if i in cache]
    if not pubkeys:
        raise RuntimeError("bin snapshot: no bin arrays found for pool")

    accounts = mp.solana_get_multiple_accounts(session, rpc_url, pubkeys, sleep_s=sleep_s)
    out: dict[int, float] = {}
    for data in accounts.values():
        if not data:
            continue
        _, _, rows = mp.decode_binarray_bins_subset(data, want_bin_ids=None)
        for r in rows:
            if not lo <= r.bin_id <= hi:
                continue
            out[r.bin_id] = r.amount_x / (10**dec_x) * price + r.amount_y / (10**dec_y)
    return out


def _append_line(row: dict[str, Any]) -> None:
    path = snapshots_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def take_snapshot(
    *,
    pool: str | None = None,
    now: datetime | None = None,
    fetch_pool: Callable[[str], dict[str, Any]] | None = None,
    read_bins_fn: Callable[..., dict[int, float]] | None = None,
) -> dict[str, Any]:
    """Write one JSONL row. Never raises."""
    addr = pool or bot_config.pool_pubkey()
    row: dict[str, Any] = {"ts": _utc_iso(now), "pool": addr}
    try:
        meta = (fetch_pool or _fetch_pool)(addr)
        inner = meta.get("data") if isinstance(meta.get("data"), dict) else meta
        cfg = inner.get("pool_config") or {}
        bin_step = int(cfg.get("bin_step"))
        price = float(inner["current_price"])
        tvl = float(inner["tvl"])
        dec_x = int((inner.get("token_x") or {}).get("decimals"))
        dec_y = int((inner.get("token_y") or {}).get("decimals"))

        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        rpc_url = bot_config.effective_rpc()
        state = mp.decode_lbpair_state(
            mp.solana_get_account_data(session, rpc_url, addr, sleep_s=config.request_sleep_s())
        )
        if state.active_id is None:
            raise ValueError("bin snapshot: could not read the active bin")
        if state.bin_step:
            bin_step = int(state.bin_step)

        bins = (read_bins_fn or read_bins)(
            session,
            pool=addr,
            rpc_url=rpc_url,
            active_id=state.active_id,
            bin_step=bin_step,
            price=price,
            dec_x=dec_x,
            dec_y=dec_y,
            sleep_s=config.request_sleep_s(),
        )
        row.update(
            {
                "active_bin": state.active_id,
                "bin_step": bin_step,
                "price": price,
                "tvl": tvl,
                "span_bins": SPAN_BINS,
            }
        )
        row.update(summarize(bins, active_id=state.active_id, bin_step=bin_step, tvl=tvl))
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("bin snapshot failed: %s", row["error"])
    try:
        _append_line(row)
    except Exception:
        log.warning("bin snapshot write failed", exc_info=True)
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
            log.warning("bin snapshot loop survived an error", exc_info=True)
        await sleep_fn(interval_sec)
