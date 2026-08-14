#!/usr/bin/env python3
"""LITE_11 live stand: working-width rebalance, bin map, top-up, resize, fees.

Devnet only. Isolates METEORA_STATE_DIR. Does not touch the live bot state/.
Do not run under ulimit -v 3000000 (Node fetch dies on DLMM.create).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROBE_JS = ROOT / "ts" / "dist" / "rebalance_probe.js"
LOG_DIR = ROOT / "logs"
WIDTH_WORK = 69
HALF_WORK = 34
BUDGET = 3.0
BUDGET_NARROW = 1.5


def _log(msg: str) -> None:
    print(msg, flush=True)


def _rpc() -> str:
    import bot_config

    return bot_config.effective_rpc()


def _pool() -> str:
    import bot_config

    return bot_config.pool_pubkey()


def _probe_cmd(cmd: str, position: str, extra: list[str] | None = None) -> dict[str, Any]:
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
        "--rpc",
        _rpc(),
        "--pool",
        _pool(),
    ]
    if extra:
        args.extend(extra)
    proc = subprocess.run(
        args, cwd=str(ROOT), capture_output=True, text=True, timeout=300
    )
    err = (proc.stderr or "").strip()
    if err:
        _log(err[-2500:])
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


def _rebalance(
    position: str,
    *,
    send: bool,
    x_bps: int = 0,
    y_bps: int = 0,
    top_up_sol: float = 0.0,
    top_up_usdc: float = 0.0,
    send_group: str = "all",
) -> dict[str, Any]:
    extra = [
        "--strategy",
        "Spot",
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
        "100",
        "--send-group",
        send_group,
    ]
    if send:
        extra.append("--send")
    return _probe_cmd("rebalance-exec" if send else "rebalance-quote", position, extra)


def _snapshot(position: str) -> dict[str, Any]:
    return _probe_cmd("snapshot", position)


def _resize(
    position: str, *, op: str, side: str, length: int, send: bool
) -> dict[str, Any]:
    extra = [
        "--resize-op",
        op,
        "--resize-side",
        side,
        "--resize-length",
        str(length),
    ]
    if send:
        extra.append("--send")
    return _probe_cmd("resize-exec", position, extra)


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


def _value_usdc(sol: float, usdc: float, price: float) -> float:
    return float(sol or 0) * float(price or 0) + float(usdc or 0)


def open_width_oor(
    *,
    side: str,
    width: int,
    budget_usd: float,
    reply: Callable[[str], Any],
) -> tuple[dict, list[str]]:
    """OOR pocket of ``width`` bins, entirely below (up) or above (down) active."""
    import money_ops
    import meteora_exec
    import meteora_ops
    from reopen_pending import set_reopen_pending

    set_reopen_pending(False)
    existing = money_ops.get_primary_position(include_empty=True)
    if existing is not None:
        money_ops.close_position_full(existing, reply=reply, record_cycle=False)

    pool = meteora_ops.pool_info(**money_ops.ops_kwargs())
    active = int(pool["activeId"])
    if side == "up":
        hi = active - 1
        lo = hi - width + 1
    elif side == "down":
        lo = active + 1
        hi = lo + width - 1
    else:
        raise ValueError(f"side must be up|down, got {side!r}")
    reply(f"activeId={active} open {side} width={width} bins [{lo},{hi}] budget=${budget_usd}")
    sug = money_ops.suggest_for_budget(budget_usd, min_bin_id=lo, max_bin_id=hi)
    need_sol = float(sug.get("needSol") or 0)
    need_usdc = float(sug.get("needUsdc") or 0)
    money_ops.apply_swap_suggestion(sug.get("swapSuggestion"), reply)
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
    reply(
        f"POS {pos['pubkey']} sol={pos.get('sol')} usdc={pos.get('usdc')} "
        f"bins=[{pos['lowerBinId']},{pos['upperBinId']}]"
    )
    return pos, sigs


def open_in_range(
    *,
    half: int,
    budget_usd: float,
    reply: Callable[[str], Any],
) -> tuple[dict, list[str]]:
    import money_ops
    import meteora_exec
    import meteora_ops
    from reopen_pending import set_reopen_pending

    set_reopen_pending(False)
    existing = money_ops.get_primary_position(include_empty=True)
    if existing is not None:
        money_ops.close_position_full(existing, reply=reply, record_cycle=False)

    pool = meteora_ops.pool_info(**money_ops.ops_kwargs())
    active = int(pool["activeId"])
    lo, hi = active - half, active + half
    reply(f"activeId={active} open in-range bins [{lo},{hi}] budget=${budget_usd}")
    sug = money_ops.suggest_for_budget(budget_usd, min_bin_id=lo, max_bin_id=hi)
    need_sol = float(sug.get("needSol") or 0)
    need_usdc = float(sug.get("needUsdc") or 0)
    money_ops.apply_swap_suggestion(sug.get("swapSuggestion"), reply)
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
    reply(
        f"POS {pos['pubkey']} sol={pos.get('sol')} usdc={pos.get('usdc')} "
        f"bins=[{pos['lowerBinId']},{pos['upperBinId']}]"
    )
    return pos, sigs


def daemon_swap(side: str, amount: float) -> dict[str, Any]:
    """Swap via meteora_exec.exec_swap — the daemon journal path, not a raw node CLI."""
    import money_ops
    import meteora_exec

    return meteora_exec.exec_swap(
        money_ops.owner(), side, amount, **money_ops.exec_kwargs()
    )


def main() -> int:
    os.environ["WALLET_KEYPAIR_PATH"] = os.path.expanduser(
        os.environ.get("WALLET_KEYPAIR_PATH")
        or "~/.config/solana/meteora-devnet.json"
    )
    os.environ["MAX_POSITION_USD"] = str(int(BUDGET) if BUDGET == int(BUDGET) else BUDGET)
    os.environ.pop("METEORA_STATE_DIR", None)

    from scenarios.harness import (
        bootstrap_live,
        close_all_positions,
        ensure_scenario_state_dir,
        restore_sol_from_market,
    )
    import bot_config
    import money_ops

    bot_config.MAX_POSITION_USD = float(BUDGET)
    owner = bootstrap_live()
    state = ensure_scenario_state_dir()
    bot_config.MAX_POSITION_USD = float(BUDGET)
    _log(
        f"LITE_11 owner={owner} state={state} pool={_pool()} "
        f"rpc_host=devnet MAX_POSITION_USD={bot_config.MAX_POSITION_USD}"
    )

    results: dict[str, Any] = {
        "owner": owner,
        "state_dir": str(state),
        "budgetUsd": BUDGET,
        "widthWork": WIDTH_WORK,
        "experiments": {},
    }

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

    def run_oor_rebalance(
        tag: str,
        *,
        side: str,
        width: int,
        budget: float,
        x_bps: int,
        y_bps: int,
        top_up_sol: float = 0.0,
        top_up_usdc: float = 0.0,
        swap_first: bool = False,
    ) -> dict[str, Any]:
        close_all_positions(_log)
        restore_sol_from_market(target_sol=0.7)
        pos, open_sigs = open_width_oor(
            side=side, width=width, budget_usd=budget, reply=_log
        )
        time.sleep(2)
        before = _snap_py(pos)
        swap_payloads: list[Any] = []
        top_sol, top_usdc = top_up_sol, top_up_usdc
        if swap_first:
            import meteora_ops

            pool = meteora_ops.pool_info(**money_ops.ops_kwargs())
            active = int(pool["activeId"])
            sug = money_ops.suggest_for_budget(
                budget,
                min_bin_id=active - HALF_WORK,
                max_bin_id=active + HALF_WORK,
            )
            _log(
                f"C suggest needSol={sug.get('needSol')} needUsdc={sug.get('needUsdc')} "
                f"swap={sug.get('swapSuggestion')}"
            )
            money_ops.apply_swap_suggestion(sug.get("swapSuggestion"), _log)
            swap_payloads.append(sug.get("swapSuggestion"))
            top_sol = float(sug.get("needSol") or 0)
            top_usdc = 0.0
            time.sleep(2)
            before = _snap_py(None)
        quote = _rebalance(
            pos["pubkey"],
            send=False,
            x_bps=x_bps,
            y_bps=y_bps,
            top_up_sol=top_sol,
            top_up_usdc=top_usdc,
        )
        _save(f"lite11_{tag}_quote.json", quote)
        if not quote.get("ok"):
            return {
                "side": side,
                "width": width,
                "open_signatures": open_sigs,
                "before": before,
                "quote": quote,
                "error": quote.get("error"),
                "after": _snap_py(None),
            }
        exe = _rebalance(
            pos["pubkey"],
            send=True,
            x_bps=x_bps,
            y_bps=y_bps,
            top_up_sol=top_sol,
            top_up_usdc=top_usdc,
        )
        _save(f"lite11_{tag}_exec.json", exe)
        time.sleep(2)
        snap = _snapshot(pos["pubkey"])
        _save(f"lite11_{tag}_bins.json", snap)
        sigs = list(exe.get("signatures") or [])
        return {
            "side": side,
            "width": width,
            "budget": budget,
            "x_bps": x_bps,
            "y_bps": y_bps,
            "topUpSol": top_sol,
            "topUpUsdc": top_usdc,
            "swap_first": swap_first,
            "swap_payloads": swap_payloads,
            "open_signatures": open_sigs,
            "before": before,
            "quote": quote,
            "exec": exe,
            "after": _snap_py(None),
            "bins": (snap.get("before") or {}).get("bins")
            or (exe.get("after") or {}).get("bins"),
            "tx_meta": [_tx_meta(s) for s in sigs],
        }

    _log("=== B: 5-bin OOR up 0/0 (stitch with LITE_10 1b) ===")
    results["experiments"]["b_narrow_5"] = capture(
        "b_narrow",
        lambda: run_oor_rebalance(
            "b5", side="up", width=5, budget=BUDGET_NARROW, x_bps=0, y_bps=0
        ),
    )

    _log("=== A: 69-bin OOR up 0/0 ===")
    results["experiments"]["a_work_69"] = capture(
        "a_work",
        lambda: run_oor_rebalance(
            "a69", side="up", width=WIDTH_WORK, budget=BUDGET, x_bps=0, y_bps=0
        ),
    )

    _log("=== C: swap then 69-bin 0/0 with top-up ===")
    results["experiments"]["c_topup"] = capture(
        "c_topup",
        lambda: run_oor_rebalance(
            "c69",
            side="up",
            width=WIDTH_WORK,
            budget=BUDGET,
            x_bps=0,
            y_bps=0,
            swap_first=True,
        ),
    )

    def run_d() -> dict[str, Any]:
        close_all_positions(_log)
        restore_sol_from_market(target_sol=0.7)
        pos, open_sigs = open_in_range(half=HALF_WORK, budget_usd=BUDGET, reply=_log)
        time.sleep(2)
        before = _snap_py(pos)
        grow = _resize(pos["pubkey"], op="increase", side="upper", length=22, send=True)
        _save("lite11_d_increase.json", grow)
        time.sleep(2)
        mid = _snap_py(None)
        shrink = _resize(pos["pubkey"], op="decrease", side="upper", length=22, send=True)
        _save("lite11_d_decrease.json", shrink)
        time.sleep(2)
        after = _snap_py(None)
        sigs = list(grow.get("signatures") or []) + list(shrink.get("signatures") or [])
        return {
            "open_signatures": open_sigs,
            "before": before,
            "increase": grow,
            "mid": mid,
            "decrease": shrink,
            "after": after,
            "tx_meta": [_tx_meta(s) for s in sigs],
        }

    _log("=== D: increase past 70 then decrease ===")
    results["experiments"]["d_resize"] = capture("d_resize", run_d)

    def run_e() -> dict[str, Any]:
        close_all_positions(_log)
        restore_sol_from_market(target_sol=0.7)
        pos, open_sigs = open_in_range(half=HALF_WORK, budget_usd=BUDGET, reply=_log)
        time.sleep(2)
        before = _snap_py(pos)
        swaps: list[Any] = []
        for side, amt in (("sol-to-usdc", 0.03), ("usdc-to-sol", 2.0)):
            try:
                p = daemon_swap(side, amt)
                swaps.append(p)
                _log(f"daemon swap {side} ok={p.get('ok')} sigs={p.get('signatures')}")
                time.sleep(2)
            except Exception as e:
                _log(f"daemon swap {side} failed: {e}")
                swaps.append({"error": str(e), "side": side})
        mid = _snap_py(None)
        pk = (mid.get("position") or {}).get("pubkey") or pos["pubkey"]
        exe = _rebalance(pk, send=True, x_bps=0, y_bps=0)
        _save("lite11_e_exec.json", exe)
        time.sleep(2)
        after = _snap_py(None)
        return {
            "open_signatures": open_sigs,
            "before": before,
            "market_swaps": swaps,
            "after_swaps": mid,
            "exec": exe,
            "after": after,
            "tx_meta": [_tx_meta(s) for s in (exe.get("signatures") or [])],
        }

    _log("=== E: in-range fees via daemon exec_swap then rebalance 0/0 ===")
    results["experiments"]["e_fees"] = capture("e_fees", run_e)

    def run_f() -> dict[str, Any]:
        close_all_positions(_log)
        restore_sol_from_market(target_sol=0.7)
        pos, open_sigs = open_width_oor(
            side="up", width=WIDTH_WORK, budget_usd=BUDGET, reply=_log
        )
        time.sleep(2)
        before = _snap_py(pos)
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
        after = _snap_py(None)
        old_sigs: list[str] = []
        if isinstance(old_ok, dict):
            old_sigs = list(old_ok.get("signatures") or [])
        for line in replies:
            if "explorer.solana.com/tx/" in line:
                sig = line.split("tx/")[-1].split("?")[0].split("<")[0]
                if sig and sig not in old_sigs and len(sig) >= 32:
                    old_sigs.append(sig)
        return {
            "open_signatures": open_sigs,
            "before": before,
            "payload": old_ok if isinstance(old_ok, dict) else {"raw": str(old_ok)},
            "replies": replies,
            "after": after,
            "tx_meta": [_tx_meta(s) for s in old_sigs],
        }

    _log("=== F: old ladder same $3 / 69 bins ===")
    results["experiments"]["f_control"] = capture("f_control", run_f)

    # G is derived from A (and B) snapshots — no extra txs.
    def _loss(tag: str) -> dict[str, Any] | None:
        row = results["experiments"].get(tag) or {}
        b, a = row.get("before") or {}, row.get("after") or {}
        bp, ap = b.get("position") or {}, a.get("position") or {}
        price = float(b.get("usdcPerSol") or a.get("usdcPerSol") or 0)
        if not price:
            return None
        before_v = _value_usdc(bp.get("sol") or 0, bp.get("usdc") or 0, price)
        after_v = _value_usdc(ap.get("sol") or 0, ap.get("usdc") or 0, price)
        return {
            "priceUsed": price,
            "beforeUsdc": before_v,
            "afterUsdc": after_v,
            "lossUsdc": before_v - after_v,
            "lossPct": (before_v - after_v) / before_v * 100 if before_v else None,
        }

    results["experiments"]["g_conversion"] = {
        "from_a_work_69": _loss("a_work_69"),
        "from_b_narrow_5": _loss("b_narrow_5"),
        "from_c_topup": _loss("c_topup"),
    }

    try:
        close_all_positions(_log)
    except Exception as e:
        _log(f"final cleanup failed: {e}")
    _save("lite11_all.json", results)
    _log("LITE_11 done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
