"""Live scenario harness helpers (devnet only). Separate from unit tests."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent

# Market wallet path from env only — never hardcode a home directory.
MARKET_ENV = "METEORA_MARKET_KEYPAIR_PATH"
MARKET_PUBKEY_EXPECTED = "3MZ89ysmEieqPSX2S5DP1qXMicTtMXiTTk5M6rXjtVA2"
DEFAULT_MARKET = "~/.config/solana/meteora-devnet-market.json"
DEFAULT_BOT = "~/.config/solana/meteora-devnet.json"


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    expected: str
    observed: str
    signatures: list[str] = field(default_factory=list)
    detail: str = ""
    skipped: bool = False


def ensure_scenario_state_dir() -> Path:
    """Isolate scenario runs from live bot state via METEORA_STATE_DIR."""
    existing = os.environ.get("METEORA_STATE_DIR", "").strip()
    if existing:
        p = Path(existing)
        p.mkdir(parents=True, exist_ok=True)
        return p
    td = Path(tempfile.mkdtemp(prefix="meteora-scenario-state-"))
    os.environ["METEORA_STATE_DIR"] = str(td)
    return td


def bootstrap_live() -> str:
    """Point WALLET at the real bot keypair; return pubkey."""
    from tests.live_guard import begin_live_script

    os.environ.setdefault(
        "WALLET_KEYPAIR_PATH",
        os.path.expanduser(
            os.environ.get("METEORA_LIVE_WALLET")
            or os.environ.get("WALLET_KEYPAIR_PATH")
            or DEFAULT_BOT
        ),
    )
    # Cap size for scenario runs (process-local).
    os.environ.setdefault("MAX_POSITION_USD", "3")
    os.environ.setdefault("AUTO_REBALANCE", "true")
    os.environ.setdefault("REBALANCE_DELAY_MIN", "0")
    os.environ.setdefault("MIN_REBALANCE_INTERVAL_MIN", "0")
    ensure_scenario_state_dir()
    return begin_live_script()


def market_keypair_path() -> Path:
    raw = os.environ.get(MARKET_ENV) or DEFAULT_MARKET
    path = Path(os.path.expanduser(raw)).resolve()
    if not path.is_file():
        raise FileNotFoundError(
            f"market keypair missing: {path} (set {MARKET_ENV})"
        )
    return path


def market_pubkey() -> str:
    out = subprocess.check_output(
        ["solana-keygen", "pubkey", str(market_keypair_path())],
        text=True,
    ).strip()
    if out != MARKET_PUBKEY_EXPECTED:
        raise RuntimeError(
            f"market pubkey {out} != expected {MARKET_PUBKEY_EXPECTED}"
        )
    return out


def _rpc_url() -> str:
    import bot_config

    return bot_config.effective_rpc()


def sol_balance(pubkey: str) -> float:
    out = subprocess.check_output(
        ["solana", "balance", pubkey, "-u", _rpc_url(), "--output", "json"],
        text=True,
    )
    data = json.loads(out)
    # solana CLI json: {"value": lamports} or {"amount": ...}
    if isinstance(data, dict) and "value" in data:
        v = data["value"]
        if isinstance(v, (int, float)):
            return float(v) / 1_000_000_000.0 if v > 1000 else float(v)
    # fallback text parse
    out2 = subprocess.check_output(
        ["solana", "balance", pubkey, "-u", _rpc_url()], text=True
    ).strip()
    return float(out2.split()[0])


def transfer_sol(
    *,
    from_keypair: Path,
    to_pubkey: str,
    amount_sol: float,
    allow_unfunded: bool = False,
) -> str:
    """Native SOL transfer; returns signature."""
    cmd = [
        "solana",
        "transfer",
        to_pubkey,
        f"{amount_sol:.9f}",
        "-u",
        _rpc_url(),
        "-k",
        str(from_keypair),
        "--allow-unfunded-recipient",
        "--no-wait",
        "--output",
        "json",
    ]
    if allow_unfunded:
        pass
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"solana transfer failed: {proc.stderr or proc.stdout}"
        )
    # JSON may be signature string or object
    raw = (proc.stdout or "").strip().splitlines()[-1]
    try:
        data = json.loads(raw)
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            return str(data.get("signature") or data.get("txid") or raw)
    except json.JSONDecodeError:
        pass
    return raw


def drain_bot_sol_to_floor(*, floor: float | None = None) -> list[str]:
    """Leave bot with just above MIN_SOL_BALANCE; park excess on market wallet."""
    import bot_config

    floor = float(floor if floor is not None else bot_config.MIN_SOL_BALANCE + 0.005)
    bot_pk = bot_config.wallet_pubkey()
    bot_kp = Path(bot_config.WALLET_KEYPAIR_PATH)
    bal = sol_balance(bot_pk)
    sigs: list[str] = []
    excess = bal - floor
    if excess > 0.002:
        sigs.append(
            transfer_sol(
                from_keypair=bot_kp,
                to_pubkey=market_pubkey(),
                amount_sol=excess,
            )
        )
    return sigs


def restore_sol_from_market(*, target_sol: float = 0.4) -> list[str]:
    """Top bot wallet back up from market wallet if low."""
    import bot_config

    bot_pk = bot_config.wallet_pubkey()
    bal = sol_balance(bot_pk)
    need = target_sol - bal
    if need <= 0.01:
        return []
    return [
        transfer_sol(
            from_keypair=market_keypair_path(),
            to_pubkey=bot_pk,
            amount_sol=need,
        )
    ]


def close_all_positions(reply: Callable[[str], Any] | None = None) -> list[str]:
    import time

    import money_ops

    log = reply or (lambda m: print(m, flush=True))
    sigs: list[str] = []
    # list-positions is flaky on public/Helius devnet under load — retry.
    pos = None
    last_err: Exception | None = None
    for attempt in range(6):
        try:
            pos = money_ops.get_primary_position(include_empty=True)
            last_err = None
            break
        except Exception as e:
            last_err = e
            log(f"list-positions retry {attempt + 1}/6: {e}")
            time.sleep(2.0 * (attempt + 1))
    if last_err is not None:
        raise last_err
    while pos is not None:
        log(f"cleanup close {pos.get('pubkey')}")
        try:
            payload = money_ops.close_position_full(
                pos, reply=log, record_cycle=False
            )
            sigs.extend(list(payload.get("signatures") or []))
        except Exception as e:
            log(f"cleanup close failed: {e}")
            break
        pos = None
        for attempt in range(6):
            try:
                pos = money_ops.get_primary_position(include_empty=True)
                break
            except Exception as e:
                log(f"list-positions retry {attempt + 1}/6: {e}")
                time.sleep(2.0 * (attempt + 1))
    return sigs


def open_oor_position(
    *,
    side: str,
    budget_usd: float = 1.5,
    reply: Callable[[str], Any] | None = None,
) -> tuple[dict, list[str]]:
    """Open a narrow out-of-range pocket.

    side=up → bins entirely below active (all USDC).
    side=down → bins entirely above active (all SOL).
    """
    import money_ops
    import meteora_exec
    import meteora_ops
    from reopen_pending import set_reopen_pending

    log = reply or (lambda m: print(m, flush=True))
    set_reopen_pending(False)
    existing = money_ops.get_primary_position(include_empty=True)
    if existing is not None:
        money_ops.close_position_full(existing, reply=log, record_cycle=False)

    pool = meteora_ops.pool_info(**money_ops.ops_kwargs())
    active = int(pool["activeId"])
    if side == "up":
        # Price went up relative to position → position below market.
        hi = active - 8
        lo = active - 12
    elif side == "down":
        lo = active + 8
        hi = active + 12
    else:
        raise ValueError(f"side must be up|down, got {side!r}")

    log(f"activeId={active} open {side} bins [{lo},{hi}] budget=${budget_usd}")
    sug = money_ops.suggest_for_budget(budget_usd, min_bin_id=lo, max_bin_id=hi)
    need_sol = float(sug.get("needSol") or 0)
    need_usdc = float(sug.get("needUsdc") or 0)
    money_ops.apply_swap_suggestion(sug.get("swapSuggestion"), log)
    payload = meteora_exec.exec_open(
        money_ops.owner(),
        need_sol,
        need_usdc,
        min_bin_id=lo,
        max_bin_id=hi,
        **money_ops.exec_kwargs(),
    )
    sigs = list(payload.get("signatures") or [])
    pos = money_ops.get_primary_position()
    if not pos:
        raise RuntimeError("open left no position")
    log(
        f"POS {pos['pubkey']} sol={pos.get('sol')} usdc={pos.get('usdc')} "
        f"bins=[{pos['lowerBinId']},{pos['upperBinId']}]"
    )
    return pos, sigs


def network_snapshot() -> dict[str, Any]:
    import bot_config
    import money_ops
    import meteora_ops

    owner = money_ops.owner()
    bal = meteora_ops.balances(owner, **money_ops.ops_kwargs())
    positions = money_ops.list_open_positions()
    return {
        "owner": owner,
        "sol": float((bal.get("sol") or {}).get("ui") or 0),
        "usdc": float((bal.get("usdc") or {}).get("ui") or 0),
        "solAvailableForOpen": bal.get("solAvailableForOpen"),
        "positions": [
            {
                "pubkey": p.get("pubkey"),
                "sol": p.get("sol"),
                "usdc": p.get("usdc"),
                "lowerBinId": p.get("lowerBinId"),
                "upperBinId": p.get("upperBinId"),
            }
            for p in positions
        ],
        "network": bot_config.effective_network(),
        "state_dir": os.environ.get("METEORA_STATE_DIR"),
    }
