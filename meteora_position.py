#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

import config


# ---- Constraints (B1): read-only, no keys, no signing, no transactions. ----

METEORA_API_BASE = "https://dlmm.datapi.meteora.ag"

BINS_PER_BIN_ARRAY = 70

# Enough precision for r**bin_id and pro-rata math.
getcontext().prec = 80


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def rpc_host_for_log(rpc_url: str) -> str:
    """Host only — RPC URLs often carry api-key= in the query."""
    from urllib.parse import urlparse

    try:
        host = urlparse(rpc_url).hostname
        return host or "(rpc)"
    except Exception:
        return "(rpc)"


def sleep_with_jitter(seconds: float) -> None:
    jitter = (time.time_ns() % 10_000_000) / 10_000_000 * 0.05
    time.sleep(max(0.0, seconds + jitter))


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout_s: float = 25.0,
    retries: int = 5,
    backoff_s: float = 0.7,
    sleep_s: float = 0.25,
) -> Any:
    last_err: Optional[BaseException] = None
    for attempt in range(1, retries + 1):
        try:
            sleep_with_jitter(sleep_s)
            resp = session.request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=timeout_s,
                headers={"accept": "application/json"},
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            return resp.json()
        except BaseException as exc:
            last_err = exc
            if attempt == retries:
                break
            sleep_with_jitter(backoff_s * (2 ** (attempt - 1)))
    raise RuntimeError(f"request failed after {retries} attempts: {url}") from last_err


def solana_rpc_call(
    session: requests.Session,
    rpc_url: str,
    method: str,
    params: List[Any],
    *,
    sleep_s: float,
    timeout_s: float = 40.0,
    retries: int = 5,
) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return request_json(
        session,
        "POST",
        rpc_url,
        json_body=payload,
        sleep_s=sleep_s,
        timeout_s=timeout_s,
        retries=retries,
    )


def solana_get_account_info_raw(
    session: requests.Session, rpc_url: str, pubkey: str, *, sleep_s: float
) -> Dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [pubkey, {"encoding": "base64", "commitment": "confirmed"}],
    }
    return request_json(session, "POST", rpc_url, json_body=payload, sleep_s=sleep_s)


def solana_get_account_data(
    session: requests.Session, rpc_url: str, pubkey: str, *, sleep_s: float
) -> Optional[bytes]:
    raw = solana_get_account_info_raw(session, rpc_url, pubkey, sleep_s=sleep_s)
    value = ((raw.get("result") or {}).get("value") or {}) if isinstance(raw, dict) else {}
    data = value.get("data")
    if isinstance(data, list) and data and isinstance(data[0], str):
        try:
            return base64.b64decode(data[0])
        except Exception:
            return None
    return None


def _chunks(xs: List[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def solana_get_multiple_accounts(
    session: requests.Session,
    rpc_url: str,
    pubkeys: List[str],
    *,
    sleep_s: float,
    chunk_size: int = 100,
) -> Dict[str, Optional[bytes]]:
    out: Dict[str, Optional[bytes]] = {}
    for chunk in _chunks(pubkeys, chunk_size):
        raw = solana_rpc_call(
            session,
            rpc_url,
            "getMultipleAccounts",
            [chunk, {"encoding": "base64", "commitment": "confirmed"}],
            sleep_s=sleep_s,
            timeout_s=40.0,
        )
        values = ((raw.get("result") or {}).get("value") or []) if isinstance(raw, dict) else []
        if not isinstance(values, list) or len(values) != len(chunk):
            raise RuntimeError("unexpected getMultipleAccounts response shape")
        for pk, v in zip(chunk, values):
            if not isinstance(v, dict):
                out[pk] = None
                continue
            data = v.get("data")
            if isinstance(data, list) and data and isinstance(data[0], str):
                try:
                    out[pk] = base64.b64decode(data[0])
                except Exception:
                    out[pk] = None
            else:
                out[pk] = None
    return out


def solana_get_program_accounts(
    session: requests.Session,
    rpc_url: str,
    program_id: str,
    *,
    filters: List[Dict[str, Any]],
    data_slice: Optional[Dict[str, int]],
    sleep_s: float,
) -> List[Dict[str, Any]]:
    cfg: Dict[str, Any] = {
        "encoding": "base64",
        "commitment": "confirmed",
        "filters": filters,
    }
    if data_slice is not None:
        cfg["dataSlice"] = data_slice
    raw = solana_rpc_call(
        session,
        rpc_url,
        "getProgramAccounts",
        [program_id, cfg],
        sleep_s=sleep_s,
        timeout_s=60.0,
    )
    res: Any = raw.get("result") if isinstance(raw, dict) else None
    if not isinstance(res, list):
        raise RuntimeError("unexpected getProgramAccounts response shape")
    return res


# ---- minimal base58 (no external libs) ----

_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58_encode(b: bytes) -> str:
    if not b:
        return ""
    zeros = 0
    for ch in b:
        if ch == 0:
            zeros += 1
        else:
            break
    n = int.from_bytes(b, "big", signed=False)
    out = bytearray()
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(_B58_ALPHABET[rem])
    out.reverse()
    return ("1" * zeros) + out.decode("ascii")


def anchor_account_discriminator(name: str) -> bytes:
    h = hashlib.sha256(f"account:{name}".encode("utf-8")).digest()
    return h[:8]


# ---- decoders (layout reused from meteora-watch approach) ----

def _read_u8(buf: bytes, off: int) -> int:
    return buf[off]


def _read_u16_le(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off : off + 2], "little", signed=False)


def _read_i32_le(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off : off + 4], "little", signed=True)


def _read_u64_le(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off : off + 8], "little", signed=False)


def _read_i64_le(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off : off + 8], "little", signed=True)


def _read_u128_le(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off : off + 16], "little", signed=False)


@dataclass
class LbPairState:
    discriminator_ok: bool
    active_id: Optional[int]
    bin_step: Optional[int]


def decode_lbpair_state(account_data: bytes) -> LbPairState:
    disc_ok = account_data[:8] == anchor_account_discriminator("LbPair")
    if len(account_data) < 8 + 80:
        return LbPairState(discriminator_ok=disc_ok, active_id=None, bin_step=None)
    body = account_data[8:]
    try:
        # Offsets validated in meteora-watch (IDL): active_id at 68, bin_step at 72.
        active_id = _read_i32_le(body, 68)
        bin_step = _read_u16_le(body, 72)
        return LbPairState(discriminator_ok=disc_ok, active_id=active_id, bin_step=bin_step)
    except Exception:
        return LbPairState(discriminator_ok=disc_ok, active_id=None, bin_step=None)


@dataclass
class PositionV2Decoded:
    discriminator_ok: bool
    lb_pair: Optional[str]
    owner: Optional[str]
    lower_bin_id: Optional[int]
    upper_bin_id: Optional[int]
    liquidity_shares: List[int]  # u128[70]
    fee_x_pending: int  # sum over bins
    fee_y_pending: int  # sum over bins


def decode_position_v2(account_data: bytes) -> PositionV2Decoded:
    disc_ok = account_data[:8] == anchor_account_discriminator("PositionV2")
    if len(account_data) < 8 + 64 + (70 * 16):
        return PositionV2Decoded(
            discriminator_ok=disc_ok,
            lb_pair=None,
            owner=None,
            lower_bin_id=None,
            upper_bin_id=None,
            liquidity_shares=[],
            fee_x_pending=0,
            fee_y_pending=0,
        )

    try:
        lb_pair = base58_encode(account_data[8 : 8 + 32])
        owner = base58_encode(account_data[40 : 40 + 32])

        liq_off = 72
        liquidity_shares: List[int] = []
        for i in range(70):
            liquidity_shares.append(_read_u128_le(account_data, liq_off + i * 16))

        # reward_infos: 70 * UserRewardInfo(48)
        reward_off = liq_off + 70 * 16
        fee_off = reward_off + 70 * 48

        # fee_infos: 70 * FeeInfo(48)
        fee_x_pending = 0
        fee_y_pending = 0
        for i in range(70):
            off = fee_off + i * 48
            # fee_x_pending at +32, fee_y_pending at +40
            fee_x_pending += _read_u64_le(account_data, off + 32)
            fee_y_pending += _read_u64_le(account_data, off + 40)

        lower_bin_id = _read_i32_le(account_data, fee_off + 70 * 48)
        upper_bin_id = _read_i32_le(account_data, fee_off + 70 * 48 + 4)

        return PositionV2Decoded(
            discriminator_ok=disc_ok,
            lb_pair=lb_pair,
            owner=owner,
            lower_bin_id=lower_bin_id,
            upper_bin_id=upper_bin_id,
            liquidity_shares=liquidity_shares,
            fee_x_pending=fee_x_pending,
            fee_y_pending=fee_y_pending,
        )
    except Exception:
        return PositionV2Decoded(
            discriminator_ok=disc_ok,
            lb_pair=None,
            owner=None,
            lower_bin_id=None,
            upper_bin_id=None,
            liquidity_shares=[],
            fee_x_pending=0,
            fee_y_pending=0,
        )


@dataclass
class BinRow:
    bin_id: int
    amount_x: int
    amount_y: int
    liquidity_supply: int
    price_u128: int


def decode_binarray_bins_subset(
    account_data: bytes, *, want_bin_ids: Optional[set]
) -> Tuple[Optional[int], Optional[str], List[BinRow]]:
    disc_ok = account_data[:8] == anchor_account_discriminator("BinArray")
    if not disc_ok or len(account_data) < 56:
        return None, None, []

    # Layout (IDL/bytemuck): disc(8) + index(i64) + version(u8) + pad7 + lb_pair(pubkey) + bins[70]
    idx = _read_i64_le(account_data, 8)
    lb_pair = base58_encode(account_data[24:56])

    bins_off = 56
    bin_size = 144
    out: List[BinRow] = []
    for i in range(BINS_PER_BIN_ARRAY):
        bin_id = idx * BINS_PER_BIN_ARRAY + i
        if want_bin_ids is not None and bin_id not in want_bin_ids:
            continue
        off = bins_off + i * bin_size
        if off + bin_size > len(account_data):
            break
        ax = _read_u64_le(account_data, off + 0)
        ay = _read_u64_le(account_data, off + 8)
        price_u128 = _read_u128_le(account_data, off + 16)
        liq_supply = _read_u128_le(account_data, off + 32)
        if ax == 0 and ay == 0 and liq_supply == 0:
            continue
        out.append(
            BinRow(
                bin_id=bin_id,
                amount_x=ax,
                amount_y=ay,
                liquidity_supply=liq_supply,
                price_u128=price_u128,
            )
        )
    return idx, lb_pair, out


def decode_spl_mint_decimals(account_data: bytes) -> Optional[int]:
    if len(account_data) < 45:
        return None
    return _read_u8(account_data, 44)


def meteora_pool_details(session: requests.Session, lb_pair: str, *, sleep_s: float) -> Dict[str, Any]:
    return request_json(session, "GET", f"{METEORA_API_BASE}/pools/{lb_pair}", sleep_s=sleep_s)


def token_is_sol(token_obj: Dict[str, Any]) -> bool:
    sym = str(token_obj.get("symbol") or "").upper()
    mint = str(token_obj.get("address") or "")
    return sym == "SOL" or mint == "So11111111111111111111111111111111111111112"


def token_is_usdc(token_obj: Dict[str, Any]) -> bool:
    sym = str(token_obj.get("symbol") or "").upper()
    mint = str(token_obj.get("address") or "")
    return sym == "USDC" or mint == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _price_y_per_x_ui(bin_id: int, *, bin_step_bps: int, dec_x: int, dec_y: int) -> Decimal:
    # Price in UI units: tokenY per tokenX.
    r = Decimal(1) + (Decimal(bin_step_bps) / Decimal(10_000))
    # r**bin_id supports negative bin ids.
    p = r ** Decimal(bin_id)
    scale = Decimal(10) ** Decimal(dec_x - dec_y)
    return p * scale


def _fmt_dec(x: Decimal, digits: int = 6) -> str:
    q = Decimal(10) ** Decimal(-digits)
    try:
        return str(x.quantize(q))
    except Exception:
        return str(x)


def _fmt_float(x: float, digits: int = 6) -> str:
    try:
        return f"{float(x):.{digits}f}"
    except Exception:
        return "n/a"


def find_positions_for_lb_pair(
    session: requests.Session,
    rpc_url: str,
    lb_pair: str,
    *,
    sleep_s: float,
    limit: int,
) -> List[str]:
    # IDL says discriminator for PositionV2 is [117, 176, 212, 199, 245, 180, 133, 182]
    disc = anchor_account_discriminator("PositionV2")
    disc_b58 = base58_encode(disc)

    # Compute expected dataSize from IDL (best-effort); keep it as a filter to cut response size.
    # body = 32+32 + 70*u128(16) + 70*UserRewardInfo(48) + 70*FeeInfo(48) + i32+i32+i64 + u64+u64 + [u64;2] + 32 + u64 + u8 + 32 + u8 + u8 + [u8;85]
    body = 32 + 32 + 70 * 16 + 70 * 48 + 70 * 48 + 4 + 4 + 8 + 8 + 8 + 16 + 32 + 8 + 1 + 32 + 1 + 1 + 85
    expected_size = 8 + body

    accounts = solana_get_program_accounts(
        session,
        rpc_url,
        config.DLMM_PROGRAM_ID,
        filters=[
            {"dataSize": expected_size},
            {"memcmp": {"offset": 0, "bytes": disc_b58}},
            {"memcmp": {"offset": 8, "bytes": lb_pair}},
        ],
        data_slice={"offset": 0, "length": 0},
        sleep_s=sleep_s,
    )
    pubkeys: List[str] = []
    for it in accounts:
        pk = it.get("pubkey")
        if isinstance(pk, str):
            pubkeys.append(pk)
    return pubkeys[: max(0, int(limit))]


def _need_bin_ids_for_position(pos: PositionV2Decoded) -> List[int]:
    if pos.lower_bin_id is None or pos.upper_bin_id is None:
        return []
    lo = int(pos.lower_bin_id)
    hi = int(pos.upper_bin_id)
    if hi < lo:
        lo, hi = hi, lo
    # PositionV2 has fixed arrays length 70; expected hi-lo <= 69 in practice.
    return list(range(lo, hi + 1))


def _bin_array_indexes_for_bins(bin_ids: List[int]) -> List[int]:
    if not bin_ids:
        return []
    imin = min(bin_ids) // BINS_PER_BIN_ARRAY
    imax = max(bin_ids) // BINS_PER_BIN_ARRAY
    return list(range(int(imin), int(imax) + 1))


def fetch_binarrays_for_lb_pair_indexes(
    session: requests.Session,
    rpc_url: str,
    lb_pair: str,
    *,
    want_indexes: List[int],
    sleep_s: float,
) -> Dict[int, str]:
    if not want_indexes:
        return {}
    want = set(int(x) for x in want_indexes)

    # BinArray: lb_pair pubkey starts at offset 24 (disc+index+version+pad).
    light = solana_get_program_accounts(
        session,
        rpc_url,
        config.DLMM_PROGRAM_ID,
        filters=[
            {"memcmp": {"offset": 24, "bytes": lb_pair}},
        ],
        data_slice={"offset": 8, "length": 16},  # index(i64) + version/pad
        sleep_s=sleep_s,
    )

    idx_to_pk: Dict[int, str] = {}
    for it in light:
        pk = it.get("pubkey")
        acc = it.get("account") or {}
        data = acc.get("data")
        if not isinstance(pk, str):
            continue
        if not (isinstance(data, list) and data and isinstance(data[0], str)):
            continue
        try:
            b = base64.b64decode(data[0])
            if len(b) < 8:
                continue
            idx = int.from_bytes(b[:8], "little", signed=True)
            if idx in want:
                idx_to_pk[idx] = pk
        except Exception:
            continue
    return idx_to_pk


def compute_position_composition_from_bins(
    *,
    pos: PositionV2Decoded,
    bins_by_id: Dict[int, BinRow],
) -> Tuple[Decimal, Decimal]:
    """
    Returns (amount_x_ui_raw_units? no: base units as Decimal), as Decimal in base units.
    Convert to UI by dividing by 10**decimals outside.
    """
    if pos.lower_bin_id is None or not pos.liquidity_shares:
        return Decimal(0), Decimal(0)
    lo = int(pos.lower_bin_id)
    amt_x = Decimal(0)
    amt_y = Decimal(0)

    # liquidity_shares[i] corresponds to bin_id = lower_bin_id + i (IDL docs).
    for i, share in enumerate(pos.liquidity_shares):
        if share == 0:
            continue
        bin_id = lo + i
        br = bins_by_id.get(bin_id)
        if br is None:
            continue
        if br.liquidity_supply <= 0:
            continue
        # Pro-rata share of reserves in the bin.
        s = Decimal(int(share))
        L = Decimal(int(br.liquidity_supply))
        ax = Decimal(int(br.amount_x))
        ay = Decimal(int(br.amount_y))
        amt_x += ax * s / L
        amt_y += ay * s / L
    return amt_x, amt_y


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Meteora DLMM position reader (phase B1, read-only)."
    )
    p.add_argument(
        "--rpc-url",
        default=config.solana_rpc_url(),
        help="Solana RPC URL (default from env SOLANA_RPC_URL or mainnet public RPC).",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=config.request_sleep_s(),
        help="Base sleep between HTTP requests (seconds).",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    p_find = sub.add_parser("find-positions", help="Find public PositionV2 accounts for a pool (LbPair).")
    p_find.add_argument("--lb-pair", default=config.dlmm_lb_pair(), help="LbPair pubkey.")
    p_find.add_argument("--limit", type=int, default=50, help="Max pubkeys to print.")

    p_get = sub.add_parser("get-position", help="Decode and print one PositionV2.")
    p_get.add_argument("--position", required=True, help="PositionV2 account pubkey.")
    p_get.add_argument(
        "--raw-rpc",
        action="store_true",
        help="Print raw getAccountInfo JSON (truncated) for this position.",
    )

    args = p.parse_args(argv)
    session = requests.Session()
    rpc_url = str(args.rpc_url)
    sleep_s = float(args.sleep)

    if args.cmd == "find-positions":
        lb_pair = str(args.lb_pair)
        pubs = find_positions_for_lb_pair(
            session,
            rpc_url,
            lb_pair,
            sleep_s=sleep_s,
            limit=int(args.limit),
        )
        print(f"RPC: {rpc_host_for_log(rpc_url)}")
        print(f"LbPair: {lb_pair}")
        print(f"Found PositionV2 accounts (showing up to {len(pubs)}):")
        for pk in pubs:
            print(f"- {pk}")
        return 0 if pubs else 2

    if args.cmd == "get-position":
        pos_pk = str(args.position)
        if args.raw_rpc:
            raw = solana_get_account_info_raw(session, rpc_url, pos_pk, sleep_s=sleep_s)
            raw_txt = json.dumps(raw, ensure_ascii=False)
            print("=== RAW RPC getAccountInfo (truncated to 5000 chars) ===")
            print(raw_txt[:5000] + ("..." if len(raw_txt) > 5000 else ""))
            print("=== END RAW RPC ===")
            print("")

        pos_bytes = solana_get_account_data(session, rpc_url, pos_pk, sleep_s=sleep_s)
        if not pos_bytes:
            eprint("getAccountInfo returned no data for position")
            return 2
        pos = decode_position_v2(pos_bytes)
        if not pos.discriminator_ok:
            eprint("WARN: discriminator mismatch for PositionV2 (layout may have changed)")

        if not pos.lb_pair:
            eprint("Failed to decode lb_pair from position")
            return 2

        lb_pair = pos.lb_pair

        # Pool context (token mints/decimals/prices).
        details = meteora_pool_details(session, lb_pair, sleep_s=sleep_s)
        tx = details.get("token_x") if isinstance(details, dict) else None
        ty = details.get("token_y") if isinstance(details, dict) else None
        if not isinstance(tx, dict) or not isinstance(ty, dict):
            eprint("Unexpected /pools/{lb_pair} response shape (missing token_x/token_y)")
            return 2

        dec_x = int(tx.get("decimals") or 0)
        dec_y = int(ty.get("decimals") or 0)
        px_usd = float(tx.get("price") or 0.0)
        py_usd = float(ty.get("price") or 0.0)

        # On-chain lb_pair to get active_id and bin_step.
        lb_bytes = solana_get_account_data(session, rpc_url, lb_pair, sleep_s=sleep_s)
        if not lb_bytes:
            eprint("Failed to fetch LbPair account data")
            return 2
        st = decode_lbpair_state(lb_bytes)
        if st.active_id is None or st.bin_step is None:
            eprint("Failed to decode active_id/bin_step from LbPair")
            return 2

        active_id = int(st.active_id)
        bin_step = int(st.bin_step)

        # Price (UI): tokenY per tokenX.
        p_active_y_per_x = _price_y_per_x_ui(active_id, bin_step_bps=bin_step, dec_x=dec_x, dec_y=dec_y)
        p_lo_y_per_x = _price_y_per_x_ui(int(pos.lower_bin_id or 0), bin_step_bps=bin_step, dec_x=dec_x, dec_y=dec_y) if pos.lower_bin_id is not None else None
        p_hi_y_per_x = _price_y_per_x_ui(int(pos.upper_bin_id or 0), bin_step_bps=bin_step, dec_x=dec_x, dec_y=dec_y) if pos.upper_bin_id is not None else None

        # Determine which side is SOL/USDC (we only need "USD per SOL" output).
        x_is_sol = token_is_sol(tx)
        y_is_sol = token_is_sol(ty)
        x_is_usdc = token_is_usdc(tx)
        y_is_usdc = token_is_usdc(ty)

        # Convert to USD per SOL boundaries/current.
        def usd_per_sol_from_price_y_per_x(p_y_per_x: Decimal) -> Optional[Decimal]:
            # p_y_per_x is tokenY per tokenX.
            if x_is_sol and y_is_usdc:
                return p_y_per_x
            if x_is_usdc and y_is_sol:
                return (Decimal(1) / p_y_per_x) if p_y_per_x != 0 else None
            # Fallback: infer by API USD prices if available.
            if px_usd > 0 and py_usd > 0:
                # If token_x is SOL then SOL price is px_usd, else if token_y is SOL then py_usd.
                if x_is_sol:
                    return Decimal(str(px_usd))
                if y_is_sol:
                    return Decimal(str(py_usd))
            return None

        cur_usd_per_sol = usd_per_sol_from_price_y_per_x(p_active_y_per_x)
        lo_usd_per_sol = usd_per_sol_from_price_y_per_x(p_lo_y_per_x) if p_lo_y_per_x is not None else None
        hi_usd_per_sol = usd_per_sol_from_price_y_per_x(p_hi_y_per_x) if p_hi_y_per_x is not None else None

        in_range = (
            (pos.lower_bin_id is not None)
            and (pos.upper_bin_id is not None)
            and (active_id >= int(pos.lower_bin_id))
            and (active_id <= int(pos.upper_bin_id))
        )

        # Fetch needed bin arrays and compute composition.
        bin_ids = _need_bin_ids_for_position(pos)
        want_arr_idx = _bin_array_indexes_for_bins(bin_ids)
        idx_to_pk = fetch_binarrays_for_lb_pair_indexes(
            session,
            rpc_url,
            lb_pair,
            want_indexes=want_arr_idx,
            sleep_s=sleep_s,
        )
        need_binarray_pks = [idx_to_pk[i] for i in sorted(idx_to_pk.keys())]
        binarray_map = solana_get_multiple_accounts(
            session, rpc_url, need_binarray_pks, sleep_s=sleep_s, chunk_size=30
        ) if need_binarray_pks else {}

        want_set = set(bin_ids)
        bins_by_id: Dict[int, BinRow] = {}
        for _pk, b in binarray_map.items():
            if not b:
                continue
            _idx, _lb, rows = decode_binarray_bins_subset(b, want_bin_ids=want_set)
            for r in rows:
                bins_by_id[r.bin_id] = r

        amt_x_base, amt_y_base = compute_position_composition_from_bins(pos=pos, bins_by_id=bins_by_id)
        amt_x_ui = (amt_x_base / (Decimal(10) ** Decimal(dec_x))) if dec_x >= 0 else Decimal(0)
        amt_y_ui = (amt_y_base / (Decimal(10) ** Decimal(dec_y))) if dec_y >= 0 else Decimal(0)

        # Map to SOL/USDC for printing.
        sol_amt = None
        usdc_amt = None
        if x_is_sol and y_is_usdc:
            sol_amt = amt_x_ui
            usdc_amt = amt_y_ui
        elif x_is_usdc and y_is_sol:
            sol_amt = amt_y_ui
            usdc_amt = amt_x_ui
        else:
            # fallback: still print X/Y, and also try best-effort SOL/USDC if symbols match.
            if x_is_sol:
                sol_amt = amt_x_ui
            if y_is_sol:
                sol_amt = amt_y_ui
            if x_is_usdc:
                usdc_amt = amt_x_ui
            if y_is_usdc:
                usdc_amt = amt_y_ui

        sol_price_usd = Decimal(str(px_usd)) if x_is_sol else Decimal(str(py_usd)) if y_is_sol else None
        usdc_price_usd = Decimal(str(px_usd)) if x_is_usdc else Decimal(str(py_usd)) if y_is_usdc else None

        pos_usd = None
        if sol_amt is not None and usdc_amt is not None and sol_price_usd is not None:
            pos_usd = sol_amt * sol_price_usd + usdc_amt * Decimal(1)

        # Fees (pending) from PositionV2 fee_infos.
        fee_x_ui = Decimal(pos.fee_x_pending) / (Decimal(10) ** Decimal(dec_x))
        fee_y_ui = Decimal(pos.fee_y_pending) / (Decimal(10) ** Decimal(dec_y))
        fee_sol = None
        fee_usdc = None
        if x_is_sol and y_is_usdc:
            fee_sol, fee_usdc = fee_x_ui, fee_y_ui
        elif x_is_usdc and y_is_sol:
            fee_sol, fee_usdc = fee_y_ui, fee_x_ui

        fee_usd = None
        if fee_sol is not None and fee_usdc is not None and sol_price_usd is not None:
            fee_usd = fee_sol * sol_price_usd + fee_usdc * Decimal(1)

        # Cross-check: compare on-chain price from active_id with API USD price ratio if SOL/USDC.
        api_ratio_usdc_per_sol = None
        if x_is_sol and y_is_usdc and px_usd > 0 and py_usd > 0:
            api_ratio_usdc_per_sol = Decimal(str(px_usd / py_usd))
        elif x_is_usdc and y_is_sol and px_usd > 0 and py_usd > 0:
            api_ratio_usdc_per_sol = Decimal(str(py_usd / px_usd))

        print(f"RPC: {rpc_host_for_log(rpc_url)}")
        print(f"Position: {pos_pk}")
        print(f"LbPair: {lb_pair}")
        print("")
        print("Decoded position (best-effort):")
        print(f"- owner: {pos.owner}")
        print(f"- bins: lower={pos.lower_bin_id} upper={pos.upper_bin_id} width={(int(pos.upper_bin_id)-int(pos.lower_bin_id)+1) if (pos.lower_bin_id is not None and pos.upper_bin_id is not None) else 'n/a'}")
        if cur_usd_per_sol is not None:
            print(f"- pool price (from active_id={active_id}, bin_step={bin_step}bps): ${_fmt_dec(cur_usd_per_sol, 6)} per SOL")
        else:
            print(f"- pool price (from active_id={active_id}, bin_step={bin_step}bps): n/a")
        if api_ratio_usdc_per_sol is not None and cur_usd_per_sol is not None:
            diff_pct = (cur_usd_per_sol / api_ratio_usdc_per_sol - Decimal(1)) * Decimal(100)
            print(f"- cross-check vs Data API token USD ratio: ${_fmt_dec(api_ratio_usdc_per_sol, 6)} per SOL (diff {_fmt_dec(diff_pct, 4)}%)")
        if lo_usd_per_sol is not None and hi_usd_per_sol is not None:
            print(f"- range bounds (by bin ids): ${_fmt_dec(lo_usd_per_sol, 6)} .. ${_fmt_dec(hi_usd_per_sol, 6)} per SOL")
        print(f"- in range now: {bool(in_range)}")
        print("")
        print("Composition (computed pro-rata from Bin.amount_{x,y} and Bin.liquidity_supply):")
        print(f"- token_x ({tx.get('symbol')}): {_fmt_dec(amt_x_ui, 9)}")
        print(f"- token_y ({ty.get('symbol')}): {_fmt_dec(amt_y_ui, 6)}")
        if sol_amt is not None and usdc_amt is not None:
            print(f"- SOL: {_fmt_dec(sol_amt, 9)}")
            print(f"- USDC: {_fmt_dec(usdc_amt, 6)}")
        if pos_usd is not None:
            print(f"- position value (USD, using Data API token prices): ${_fmt_dec(pos_usd, 2)}")
        print("")
        print("Unclaimed fees (from PositionV2.fee_infos[*].fee_*_pending sums):")
        print(f"- token_x pending ({tx.get('symbol')}): {_fmt_dec(fee_x_ui, 9)}")
        print(f"- token_y pending ({ty.get('symbol')}): {_fmt_dec(fee_y_ui, 6)}")
        if fee_sol is not None and fee_usdc is not None:
            print(f"- pending SOL: {_fmt_dec(fee_sol, 9)}")
            print(f"- pending USDC: {_fmt_dec(fee_usdc, 6)}")
        if fee_usd is not None:
            print(f"- pending fees (USD): ${_fmt_dec(fee_usd, 4)}")

        # Proof fragments: show one bin row if available.
        if bin_ids:
            sample_id = bin_ids[len(bin_ids) // 2]
            br = bins_by_id.get(sample_id)
            if br is not None:
                print("")
                print("Raw fragment (decoded Bin fields for one bin in range):")
                print(
                    f"- bin_id={br.bin_id} amount_x={br.amount_x} amount_y={br.amount_y} liquidity_supply={br.liquidity_supply} price_u128={br.price_u128}"
                )

        return 0

    raise RuntimeError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())

