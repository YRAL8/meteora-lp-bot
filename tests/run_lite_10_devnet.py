#!/usr/bin/env python3
"""LITE_10 live stand: measure DLMM rebalancePosition vs the old close→swap→open path.

Devnet only. Isolates METEORA_STATE_DIR. Does not touch the live bot state/.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROBE_JS = ROOT / "ts" / "dist" / "rebalance_probe.js"
LOG_DIR = ROOT / "logs"
DEVNET_GENESIS_HINT = "devnet"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _rpc() -> str:
    import bot_config

    return bot_config.effective_rpc()


def _pool() -> str:
    import bot_config

    return bot_config.pool_pubkey()


def _probe_cmd(
    cmd: str,
    position: str,
    *,
    send: bool = False,
    x_bps: int = 10_000,
    y_bps: int = 10_000,
    top_up_sol: float = 0.0,
    top_up_usdc: float = 0.0,
    strategy: str = "Spot",
    send_group: str = "all",
    slippage_bps: int = 100,
) -> dict[str, Any]:
    if not PROBE_JS.is_file():
        raise FileNotFoundError(f"missing {PROBE_JS}; cd ts && npx tsc")
    args = [
        "node",
        str(PROBE_JS),
        cmd,
        "--network",
        "devnet",
        "--position",
        position,
        "--strategy",
        strategy,
        "--x-withdraw-bps",
        str(x_bps),
        "--y-withdraw-bps",
        str(y_bps),
        "--top-up-sol",
        str(top_up_sol),
        "--top-up-usdc",
        str(top_up_usdc),
        "--max-active-bin-slippage",
        "3",
        "--slippage-bps",
        str(slippage_bps),
        "--send-group",
        send_group,
        "--rpc",
        _rpc(),
        "--pool",
        _pool(),
    ]
    if send:
        args.append("--send")
    proc = subprocess.run(
        args,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    err = (proc.stderr or "").strip()
    if err:
        _log(err[-2000:])
    raw = (proc.stdout or "").strip().splitlines()
    if not raw:
        raise RuntimeError(
            f"probe empty stdout exit={proc.returncode} stderr={err[-800:]}"
        )
    payload = json.loads(raw[-1])
    if proc.returncode != 0 and not payload.get("ok"):
        return payload
    if proc.returncode != 0:
        raise RuntimeError(
            f"probe exit={proc.returncode} payload={payload} stderr={err[-500:]}"
        )
    return payload


def _tx_meta(signature: str) -> dict[str, Any]:
    import requests

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {
                "encoding": "json",
                "commitment": "confirmed",
                "maxSupportedTransactionVersion": 0,
            },
        ],
    }
    r = requests.post(_rpc(), json=body, timeout=30)
    r.raise_for_status()
    val = (r.json() or {}).get("result")
    if not val:
        return {"signature": signature, "missing": True}
    meta = val.get("meta") or {}
    return {
        "signature": signature,
        "explorer": f"https://explorer.solana.com/tx/{signature}?cluster=devnet",
        "slot": val.get("slot"),
        "feeLamports": meta.get("fee"),
        "computeUnitsConsumed": meta.get("computeUnitsConsumed"),
        "err": meta.get("err"),
    }


def _snap_py(position: dict | None) -> dict[str, Any]:
    import meteora_ops
    import money_ops

    owner = money_ops.owner()
    kw = money_ops.ops_kwargs()
    pool = meteora_ops.pool_info(**kw)
    bal = meteora_ops.balances(owner, **kw)
    pos = position or money_ops.get_primary_position(include_empty=True)
    fees = (pos or {}).get("fees") or {}
    return {
        "activeId": pool.get("activeId"),
        "usdcPerSol": pool.get("usdcPerSol"),
        "position": None
        if pos is None
        else {
            "pubkey": pos.get("pubkey"),
            "lowerBinId": pos.get("lowerBinId"),
            "upperBinId": pos.get("upperBinId"),
            "sol": pos.get("sol"),
            "usdc": pos.get("usdc"),
            "fees": {"sol": fees.get("sol"), "usdc": fees.get("usdc")},
        },
        "wallet": {
            "sol": (bal.get("sol") or {}).get("ui"),
            "usdc": (bal.get("usdc") or {}).get("ui"),
        },
    }


def _save(name: str, payload: Any) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _log(f"wrote {path}")
    return path


def _market_swap(side: str, amount: float) -> dict[str, Any] | None:
    """Swap from the market wallet to generate pool volume/fees."""
    from scenarios.harness import market_keypair_path, MARKET_PUBKEY_EXPECTED

    kp = market_keypair_path()
    exec_js = ROOT / "ts" / "dist" / "exec.js"
    args = [
        "node",
        str(exec_js),
        "exec-swap",
        "--network",
        "devnet",
        "--send",
        "--wallet",
        str(kp),
        "--owner",
        MARKET_PUBKEY_EXPECTED,
        "--side",
        side,
        "--amount",
        str(amount),
        "--rpc",
        _rpc(),
        "--pool",
        _pool(),
    ]
    proc = subprocess.run(
        args, cwd=str(ROOT), capture_output=True, text=True, timeout=180
    )
    _log((proc.stderr or "")[-1500:])
    raw = (proc.stdout or "").strip().splitlines()
    if not raw:
        _log(f"market swap empty stdout exit={proc.returncode}")
        return None
    try:
        return json.loads(raw[-1])
    except json.JSONDecodeError:
        _log(f"market swap not json: {raw[-1][:300]}")
        return None


def main() -> int:
    os.environ.setdefault(
        "WALLET_KEYPAIR_PATH",
        os.path.expanduser("~/.config/solana/meteora-devnet.json"),
    )
    os.environ.setdefault("MAX_POSITION_USD", "3")
    # Force isolated state so we never touch live state/.
    os.environ.pop("METEORA_STATE_DIR", None)

    from scenarios.harness import (
        bootstrap_live,
        close_all_positions,
        ensure_scenario_state_dir,
        open_oor_position,
        restore_sol_from_market,
    )
    import money_ops

    owner = bootstrap_live()
    state = ensure_scenario_state_dir()
    _log(f"LITE_10 owner={owner} state={state} pool={_pool()} rpc_host=devnet")

    results: dict[str, Any] = {"owner": owner, "state_dir": str(state), "experiments": {}}

    def capture(tag: str, fn) -> dict[str, Any]:
        try:
            return fn()
        except Exception as e:
            _log(f"{tag} FAILED: {e}")
            try:
                snap = _snap_py(None)
            except Exception:
                snap = {"error": "snapshot failed"}
            return {"error": str(e), "after": snap}

    def run_oor_rebalance(tag: str, side: str, *, x_bps: int, y_bps: int) -> dict[str, Any]:
        close_all_positions(_log)
        restore_sol_from_market(target_sol=0.4)
        pos, open_sigs = open_oor_position(side=side, budget_usd=1.5, reply=_log)
        time.sleep(2)
        before = _snap_py(pos)
        quote = _probe_cmd(
            "rebalance-quote",
            pos["pubkey"],
            send=False,
            x_bps=x_bps,
            y_bps=y_bps,
        )
        _save(f"lite10_{tag}_quote.json", quote)
        if not quote.get("ok"):
            return {
                "side": side,
                "open_signatures": open_sigs,
                "before": before,
                "quote": quote,
                "exec": None,
                "after": _snap_py(None),
                "error": quote.get("error"),
            }
        exe = _probe_cmd(
            "rebalance-exec",
            pos["pubkey"],
            send=True,
            x_bps=x_bps,
            y_bps=y_bps,
        )
        _save(f"lite10_{tag}_exec.json", exe)
        time.sleep(2)
        after = _snap_py(None)
        sigs = list(exe.get("signatures") or [])
        metas = [_tx_meta(s) for s in sigs]
        return {
            "side": side,
            "x_bps": x_bps,
            "y_bps": y_bps,
            "open_signatures": open_sigs,
            "before": before,
            "quote": quote,
            "exec": exe,
            "after": after,
            "tx_meta": metas,
        }

    # --- Experiment 1: OOR up, full withdraw bps, no top-up ---
    _log("=== EXP 1: side=up x/yWithdrawBps=10000 ===")
    results["experiments"]["exp1_up_10000"] = capture(
        "exp1", lambda: run_oor_rebalance("exp1", "up", x_bps=10_000, y_bps=10_000)
    )

    # Extra: 0/0 — the actual "redeposit around active" setting in the SDK.
    _log("=== EXP 1b: side=up x/yWithdrawBps=0 (redeposit all) ===")
    results["experiments"]["exp1b_up_0"] = capture(
        "exp1b", lambda: run_oor_rebalance("exp1b", "up", x_bps=0, y_bps=0)
    )

    # --- Experiment 2: OOR down ---
    _log("=== EXP 2: side=down x/yWithdrawBps=10000 ===")
    results["experiments"]["exp2_down_10000"] = capture(
        "exp2", lambda: run_oor_rebalance("exp2", "down", x_bps=10_000, y_bps=10_000)
    )
    _log("=== EXP 2b: side=down x/yWithdrawBps=0 ===")
    results["experiments"]["exp2b_down_0"] = capture(
        "exp2b", lambda: run_oor_rebalance("exp2b", "down", x_bps=0, y_bps=0)
    )

    def run_exp5() -> dict[str, Any]:
        close_all_positions(_log)
        restore_sol_from_market(target_sol=0.4)
        pos, open_sigs = open_oor_position(side="up", budget_usd=1.5, reply=_log)
        before_fees = _snap_py(pos)
        swap_payloads = []
        for side, amt in (("sol-to-usdc", 0.02), ("usdc-to-sol", 1.0)):
            try:
                p = _market_swap(side, amt)
                swap_payloads.append(p)
                time.sleep(2)
            except Exception as e:
                _log(f"market swap {side} failed: {e}")
                swap_payloads.append({"error": str(e), "side": side})
        mid_fees = _snap_py(None)
        pk = (mid_fees.get("position") or {}).get("pubkey") or pos["pubkey"]
        exe5 = _probe_cmd("rebalance-exec", pk, send=True, x_bps=0, y_bps=0)
        _save("lite10_exp5_exec.json", exe5)
        time.sleep(2)
        return {
            "open_signatures": open_sigs,
            "before": before_fees,
            "market_swaps": swap_payloads,
            "after_swaps": mid_fees,
            "exec": exe5,
            "after": _snap_py(None),
            "tx_meta": [_tx_meta(s) for s in (exe5.get("signatures") or [])],
        }

    _log("=== EXP 5: try to accrue fees then rebalance ===")
    results["experiments"]["exp5_fees"] = capture("exp5", run_exp5)

    # --- Experiment 6: groups from quotes already saved ---
    results["experiments"]["exp6_groups"] = {
        "from_exp1_quote": (results["experiments"]["exp1_up_10000"].get("quote") or {}).get(
            "instructionGroups"
        ),
        "from_exp1b_quote": (results["experiments"]["exp1b_up_0"].get("quote") or {}).get(
            "instructionGroups"
        ),
    }

    def run_exp6() -> dict[str, Any]:
        g = results["experiments"]["exp6_groups"]["from_exp1b_quote"] or {}
        if not g.get("twoOnChainGroups"):
            return {
                "skipped": True,
                "reason": "initBinArrayInstructions empty — one on-chain group",
            }
        _log("=== EXP 6: two groups — send only initBinArray ===")
        close_all_positions(_log)
        restore_sol_from_market(target_sol=0.4)
        pos, open_sigs = open_oor_position(side="up", budget_usd=1.5, reply=_log)
        before6 = _snap_py(pos)
        exe6 = _probe_cmd(
            "rebalance-exec",
            pos["pubkey"],
            send=True,
            x_bps=0,
            y_bps=0,
            send_group="init",
        )
        _save("lite10_exp6_init_only.json", exe6)
        time.sleep(2)
        return {
            "open_signatures": open_sigs,
            "before": before6,
            "exec": exe6,
            "after": _snap_py(None),
        }

    results["experiments"]["exp6_init_only"] = capture("exp6", run_exp6)

    def run_control() -> dict[str, Any]:
        close_all_positions(_log)
        restore_sol_from_market(target_sol=0.4)
        pos, open_sigs = open_oor_position(side="up", budget_usd=1.5, reply=_log)
        before_old = _snap_py(pos)
        replies: list[str] = []
        try:
            payload = money_ops.rebalance_position(
                pos, reply=lambda m: (replies.append(m), _log(m))
            )
            old_ok = payload
        except Exception as e:
            old_ok = {"error": str(e)}
            _log(f"old ladder error: {e}")
        time.sleep(2)
        after_old = _snap_py(None)
        old_sigs: list[str] = []
        if isinstance(old_ok, dict):
            old_sigs = list(old_ok.get("signatures") or [])
        return {
            "open_signatures": open_sigs,
            "before": before_old,
            "payload": old_ok if isinstance(old_ok, dict) else {"raw": str(old_ok)},
            "replies": replies,
            "after": after_old,
            "tx_meta": [_tx_meta(s) for s in old_sigs],
        }

    _log("=== CONTROL: old close→swap→open on same ~$1.5 OOR ===")
    results["experiments"]["control_old_ladder"] = capture("control", run_control)

    try:
        close_all_positions(_log)
    except Exception as e:
        _log(f"final cleanup failed: {e}")
    _save("lite10_all.json", results)
    _log("LITE_10 done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
