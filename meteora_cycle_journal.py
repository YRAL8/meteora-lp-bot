"""Per-cycle PnL journal — port of orca-lp-bot/orca_bot/cycle_journal.py.

Math (compute_would_be_value_usd / compute_divergence_usd) is copied literally.
DLMM adaptations: mint → position pubkey; lower/upper prices stored alongside
bin ids; path/efficiency still tracked from monitor ticks on usdcPerSol.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Meteora DLMM fee is dynamic; use a small constant estimate for cost accounting
# (same order of magnitude as Orca's 0.04% when fee is unknown).
POOL_FEE_PCT = 0.0004
NETWORK_FEE_LAMPORTS_EST = 41_000
LAMPORTS_PER_SOL = 1_000_000_000

import state_paths

ROOT = state_paths.ROOT
STATE_FILENAME = "cycle_state.json"
JOURNAL_FILENAME = "cycle_journal.jsonl"


def default_data_dir() -> str:
    return str(state_paths.state_dir())


# Back-compat alias (resolved at import; prefer default_data_dir()).
DEFAULT_DATA_DIR = default_data_dir()

MAX_JOURNAL_RECORDS = 5000
MAX_JOURNAL_BYTES = 8 * 1024 * 1024


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def compute_would_be_value_usd(
    *, open_sol_qty: float, open_usdc_qty: float, close_price: float
) -> float:
    return float(open_sol_qty * close_price + open_usdc_qty)


def compute_divergence_usd(
    *,
    open_sol_qty: float,
    open_usdc_qty: float,
    close_price: float,
    close_position_value_usd: float,
) -> float:
    """Расхождение = фактическая стоимость позиции при закрытии - 'стоило бы'."""
    would_be = compute_would_be_value_usd(
        open_sol_qty=open_sol_qty,
        open_usdc_qty=open_usdc_qty,
        close_price=close_price,
    )
    return float(close_position_value_usd - would_be)


@dataclass
class ActiveCycle:
    mint: str  # position pubkey (Orca field name kept for JSON compatibility)
    open_time_utc: str
    range_width_pct: float
    lower_price: float
    upper_price: float
    open_price: float
    open_sol_qty: float
    open_usdc_qty: float
    open_position_value_usd: float
    lower_bin_id: int = 0
    upper_bin_id: int = 0
    samples: int = 0
    path_sum: float = 0.0
    min_price: float = 0.0
    max_price: float = 0.0
    last_price: float = 0.0
    incomplete: bool = False
    incomplete_reasons: list[str] | None = None


@dataclass
class PendingClose:
    cycle: ActiveCycle
    close_time_utc: str
    close_price: float
    close_position_value_usd: float
    fees_sol: float
    fees_usdc: float
    trigger: str = "manual"  # "manual" | "auto"


@dataclass
class PendingSwap:
    direction: str  # "SOL_TO_USDC" | "USDC_TO_SOL"
    amount_in: float
    price: float


@dataclass
class StateFile:
    version: int = 1
    active: ActiveCycle | None = None
    pending_close: PendingClose | None = None
    pending_swap: PendingSwap | None = None


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


class CycleJournal:
    """Observer-only journal: never throws to caller."""

    def __init__(self, *, data_dir: str = DEFAULT_DATA_DIR) -> None:
        self.data_dir = data_dir

    @property
    def state_path(self) -> str:
        return os.path.join(self.data_dir, STATE_FILENAME)

    @property
    def journal_path(self) -> str:
        return os.path.join(self.data_dir, JOURNAL_FILENAME)

    def _ensure_dir(self) -> bool:
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            return True
        except Exception:
            log.warning("CycleJournal: не удалось создать data_dir=%r", self.data_dir, exc_info=True)
            return False

    def _load_state(self) -> StateFile:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return StateFile()
        except Exception:
            log.warning("CycleJournal: не удалось прочитать state", exc_info=True)
            return StateFile()
        try:
            st = StateFile(version=int(raw.get("version", 1)))
            if raw.get("active"):
                st.active = ActiveCycle(**raw["active"])
            if raw.get("pending_close"):
                pc = raw["pending_close"]
                st.pending_close = PendingClose(
                    cycle=ActiveCycle(**pc["cycle"]),
                    close_time_utc=pc["close_time_utc"],
                    close_price=float(pc["close_price"]),
                    close_position_value_usd=float(pc["close_position_value_usd"]),
                    fees_sol=float(pc["fees_sol"]),
                    fees_usdc=float(pc["fees_usdc"]),
                    trigger=str(pc.get("trigger") or "manual"),
                )
            if raw.get("pending_swap"):
                ps = raw["pending_swap"]
                st.pending_swap = PendingSwap(
                    direction=str(ps["direction"]),
                    amount_in=float(ps["amount_in"]),
                    price=float(ps["price"]),
                )
            return st
        except Exception:
            log.warning("CycleJournal: state JSON некорректен", exc_info=True)
            return StateFile()

    def _atomic_write_json(self, path: str, obj: Any) -> None:
        if not self._ensure_dir():
            return
        try:
            data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            fd, tmp = tempfile.mkstemp(prefix=".tmp.", suffix=".json", dir=self.data_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, path)
            finally:
                try:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                except Exception:
                    pass
        except Exception:
            log.warning("CycleJournal: не удалось записать %s", path, exc_info=True)

    def _save_state(self, st: StateFile) -> None:
        payload: dict[str, Any] = {
            "version": st.version,
            "active": None,
            "pending_close": None,
            "pending_swap": None,
        }
        if st.active is not None:
            payload["active"] = asdict(st.active)
        if st.pending_close is not None:
            payload["pending_close"] = {
                "cycle": asdict(st.pending_close.cycle),
                "close_time_utc": st.pending_close.close_time_utc,
                "close_price": st.pending_close.close_price,
                "close_position_value_usd": st.pending_close.close_position_value_usd,
                "fees_sol": st.pending_close.fees_sol,
                "fees_usdc": st.pending_close.fees_usdc,
                "trigger": getattr(st.pending_close, "trigger", None) or "manual",
            }
        if st.pending_swap is not None:
            payload["pending_swap"] = asdict(st.pending_swap)
        self._atomic_write_json(self.state_path, payload)

    def _append_jsonl(self, obj: dict[str, Any]) -> None:
        if not self._ensure_dir():
            return
        try:
            line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._trim_journal()
        except Exception:
            log.warning("CycleJournal: не удалось append JSONL", exc_info=True)

    def _trim_journal(self) -> None:
        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            size = os.path.getsize(self.journal_path)
            if len(lines) <= MAX_JOURNAL_RECORDS and size <= MAX_JOURNAL_BYTES:
                return
            if size > MAX_JOURNAL_BYTES and len(lines) > 1:
                lines = lines[-max(1, len(lines) // 2) :]
            keep = lines[-MAX_JOURNAL_RECORDS:]
            tmp = self.journal_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(keep)
            os.replace(tmp, self.journal_path)
        except Exception:
            log.warning("CycleJournal: не удалось обрезать журнал", exc_info=True)

    def on_open(
        self,
        *,
        position_pubkey: str,
        range_width_pct: float,
        lower_bin_id: int,
        upper_bin_id: int,
        lower_price: float,
        upper_price: float,
        open_price: float,
        open_sol_qty: float,
        open_usdc_qty: float,
        open_position_value_usd: float,
        now: datetime | None = None,
    ) -> None:
        now = now or _utc_now()
        self.finalize_pending_cycle()
        st = self._load_state()
        active = ActiveCycle(
            mint=position_pubkey,
            open_time_utc=_utc_iso(now),
            range_width_pct=float(range_width_pct),
            lower_price=float(lower_price),
            upper_price=float(upper_price),
            open_price=float(open_price),
            open_sol_qty=float(open_sol_qty),
            open_usdc_qty=float(open_usdc_qty),
            open_position_value_usd=float(open_position_value_usd),
            lower_bin_id=int(lower_bin_id),
            upper_bin_id=int(upper_bin_id),
            samples=0,
            path_sum=0.0,
            min_price=float(open_price),
            max_price=float(open_price),
            last_price=float(open_price),
            incomplete=False,
            incomplete_reasons=[],
        )
        st.active = active
        self._save_state(st)

    def on_monitor_tick(self, price: float) -> None:
        st = self._load_state()
        if st.active is None:
            return
        p = float(price)
        st.active.samples += 1
        if st.active.last_price > 0:
            st.active.path_sum += abs(p - st.active.last_price)
        st.active.last_price = p
        if st.active.min_price <= 0:
            st.active.min_price = p
        if st.active.max_price <= 0:
            st.active.max_price = p
        st.active.min_price = min(st.active.min_price, p)
        st.active.max_price = max(st.active.max_price, p)
        self._save_state(st)

    def mark_add_liquidity_incomplete(self) -> None:
        st = self._load_state()
        if st.active is None:
            return
        st.active.incomplete = True
        reasons = list(st.active.incomplete_reasons or [])
        if "add_liquidity" not in reasons:
            reasons.append("add_liquidity")
        st.active.incomplete_reasons = reasons
        self._save_state(st)

    def capture_close_snapshot(
        self,
        *,
        close_price: float,
        close_position_value_usd: float,
        fees_sol: float,
        fees_usdc: float,
        position_pubkey: str | None = None,
        now: datetime | None = None,
        trigger: str = "manual",
    ) -> None:
        now = now or _utc_now()
        st = self._load_state()
        if st.active is None:
            return
        trig = trigger if trigger in ("manual", "auto") else "manual"
        pending = PendingClose(
            cycle=st.active,
            close_time_utc=_utc_iso(now),
            close_price=float(close_price),
            close_position_value_usd=float(close_position_value_usd),
            fees_sol=float(fees_sol),
            fees_usdc=float(fees_usdc),
            trigger=trig,
        )
        if position_pubkey and st.active.mint != position_pubkey:
            st.active.incomplete = True
            reasons = list(st.active.incomplete_reasons or [])
            if "mint_mismatch" not in reasons:
                reasons.append("mint_mismatch")
            st.active.incomplete_reasons = reasons
        st.pending_close = pending
        st.active = None
        st.pending_swap = None
        self._save_state(st)

    def record_swap_success(
        self, *, direction: str, amount_in: float, price: float
    ) -> None:
        st = self._load_state()
        if st.pending_close is None:
            return
        if direction not in ("SOL_TO_USDC", "USDC_TO_SOL"):
            return
        st.pending_swap = PendingSwap(
            direction=direction, amount_in=float(amount_in), price=float(price)
        )
        self._save_state(st)

    def finalize_pending_cycle(self) -> None:
        st = self._load_state()
        if st.pending_close is None:
            return
        cycle = st.pending_close.cycle
        close_price = float(st.pending_close.close_price)
        close_value_usd = float(st.pending_close.close_position_value_usd)
        fees_usd = float(
            st.pending_close.fees_sol * close_price + st.pending_close.fees_usdc
        )
        divergence_usd = compute_divergence_usd(
            open_sol_qty=cycle.open_sol_qty,
            open_usdc_qty=cycle.open_usdc_qty,
            close_price=close_price,
            close_position_value_usd=close_value_usd,
        )
        swap_fee_usd = 0.0
        swap_direction = "NONE"
        swap_amount_in = 0.0
        if st.pending_swap is not None:
            swap_direction = st.pending_swap.direction
            swap_amount_in = float(st.pending_swap.amount_in)
            if swap_direction == "SOL_TO_USDC":
                swap_fee_usd = POOL_FEE_PCT * (
                    swap_amount_in * float(st.pending_swap.price)
                )
            elif swap_direction == "USDC_TO_SOL":
                swap_fee_usd = POOL_FEE_PCT * swap_amount_in
        tx_fee_usd = (NETWORK_FEE_LAMPORTS_EST / LAMPORTS_PER_SOL) * close_price
        costs_usd = float(swap_fee_usd + tx_fee_usd)
        pnl_usd = float(fees_usd - abs(divergence_usd) - costs_usd)
        open_time = _parse_utc_iso(cycle.open_time_utc)
        close_time = _parse_utc_iso(st.pending_close.close_time_utc)
        duration_hours = float((close_time - open_time).total_seconds() / 3600.0)
        net_pct = (
            (close_price - cycle.open_price) / cycle.open_price
            if cycle.open_price > 0
            else 0.0
        )
        path_pct = (cycle.path_sum / cycle.open_price) if cycle.open_price > 0 else 0.0
        efficiency: float | None
        if path_pct > 0:
            efficiency = abs(net_pct) / path_pct
        else:
            efficiency = None
        amplitude_pct = (
            ((cycle.max_price - cycle.min_price) / cycle.open_price)
            if cycle.open_price > 0
            else 0.0
        )
        record: dict[str, Any] = {
            "version": 1,
            "mint": cycle.mint,
            "open_time_utc": cycle.open_time_utc,
            "close_time_utc": st.pending_close.close_time_utc,
            "range_width_pct": cycle.range_width_pct,
            "lower_price": cycle.lower_price,
            "upper_price": cycle.upper_price,
            "lower_bin_id": cycle.lower_bin_id,
            "upper_bin_id": cycle.upper_bin_id,
            "open_price": cycle.open_price,
            "close_price": close_price,
            "open_sol_qty": cycle.open_sol_qty,
            "open_usdc_qty": cycle.open_usdc_qty,
            "open_position_value_usd": cycle.open_position_value_usd,
            "close_position_value_usd": close_value_usd,
            "fees_sol": st.pending_close.fees_sol,
            "fees_usdc": st.pending_close.fees_usdc,
            "fees_usd": fees_usd,
            "divergence_usd": divergence_usd,
            "swap_direction": swap_direction,
            "swap_amount_in": swap_amount_in,
            "swap_fee_usd": float(swap_fee_usd),
            "tx_fee_lamports_est": NETWORK_FEE_LAMPORTS_EST,
            "tx_fee_usd_est": float(tx_fee_usd),
            "costs_usd_est": costs_usd,
            "pnl_usd": pnl_usd,
            "samples": cycle.samples,
            "path_sum": cycle.path_sum,
            "min_price": cycle.min_price,
            "max_price": cycle.max_price,
            "net_pct": float(net_pct),
            "path_pct": float(path_pct),
            "efficiency": float(efficiency) if efficiency is not None else None,
            "amplitude_pct": float(amplitude_pct),
            "duration_hours": duration_hours,
            "incomplete": bool(cycle.incomplete),
            "incomplete_reasons": list(cycle.incomplete_reasons or []),
            "trigger": getattr(st.pending_close, "trigger", None) or "manual",
        }
        self._append_jsonl(record)
        st.pending_close = None
        st.pending_swap = None
        self._save_state(st)

    def read_recent_cycles(self, *, limit: int = 10) -> list[dict[str, Any]]:
        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            return []
        except Exception:
            log.warning("CycleJournal: не удалось прочитать journal", exc_info=True)
            return []
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
            if len(out) >= limit:
                break
        return list(reversed(out))

    def read_all_cycles(self) -> list[dict[str, Any]]:
        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            return []
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out


_DEFAULT: CycleJournal | None = None


def get_default_journal() -> CycleJournal:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = CycleJournal(data_dir=default_data_dir())
    return _DEFAULT


def safe_call(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:
        log.warning(
            "CycleJournal observer call failed: %s",
            getattr(fn, "__name__", "<?>"),
            exc_info=True,
        )
