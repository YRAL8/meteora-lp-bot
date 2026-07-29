#!/usr/bin/env python3
"""Thin bridge to ts/dist/exec.js (sign + send on devnet by default)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

ROOT = Path(__file__).resolve().parent
EXEC_JS = ROOT / "ts" / "dist" / "exec.js"


class MeteoraExecError(RuntimeError):
    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        msg = payload.get("error") or json.dumps(payload, ensure_ascii=False)
        stage = payload.get("stage")
        super().__init__(f"{msg}" + (f" (stage={stage})" if stage else ""))


def _ensure_exec_built() -> None:
    if EXEC_JS.is_file():
        return
    raise FileNotFoundError(
        f"missing {EXEC_JS}; build with: (cd ts && npx tsc -p tsconfig.json)"
    )


def run_exec(
    args: List[str],
    *,
    rpc: Optional[str] = None,
    pool: Optional[str] = None,
    network: str = "devnet",
    send: bool = False,
    timeout_s: float = 300.0,
    extra_env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run `node ts/dist/exec.js ...` and return parsed JSON from stdout."""
    _ensure_exec_built()
    cmd = ["node", str(EXEC_JS), *args, "--network", network]
    if send:
        cmd.append("--send")
    if rpc:
        cmd.extend(["--rpc", rpc])
    elif network == "devnet" and "SOLANA_DEVNET_RPC_URL" not in os.environ:
        cmd.extend(["--rpc", "https://api.devnet.solana.com"])
    elif network == "mainnet" and "SOLANA_RPC_URL" not in os.environ:
        cmd.extend(["--rpc", config.solana_rpc_url()])
    if pool:
        cmd.extend(["--pool", pool])
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    # Propagate priority fee default into child (TS reads PRIORITY_FEE_MICROLAMPORTS).
    env.setdefault(
        "PRIORITY_FEE_MICROLAMPORTS",
        os.getenv("PRIORITY_FEE_MICROLAMPORTS", "50000"),
    )
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
    )
    stderr = (proc.stderr or "").strip()
    if stderr:
        print(stderr, file=sys.stderr)
    stdout = (proc.stdout or "").strip()
    if not stdout:
        raise RuntimeError(
            f"exec produced empty stdout (exit={proc.returncode}). stderr={stderr[:500]}"
        )
    line = stdout.splitlines()[-1]
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"exec stdout is not JSON: {line[:300]!r} (exit={proc.returncode})"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"exec JSON root must be object, got {type(payload)}")
    if not payload.get("ok", False):
        raise MeteoraExecError(payload)
    if proc.returncode != 0:
        raise RuntimeError(
            f"exec exit={proc.returncode} but ok:true payload={payload.get('action')}"
        )
    return payload


def exec_open(
    owner: str,
    sol: float,
    usdc: float,
    *,
    send: bool = False,
    min_bin_id: Optional[int] = None,
    max_bin_id: Optional[int] = None,
    strategy: str = "Spot",
    slippage_bps: Optional[int] = None,
    priority_fee: Optional[int] = None,
    force_ignore_journal: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = [
        "exec-open",
        "--owner",
        owner,
        "--sol",
        str(sol),
        "--usdc",
        str(usdc),
        "--strategy",
        strategy,
    ]
    if min_bin_id is not None:
        args.extend(["--min-bin-id", str(min_bin_id)])
    if max_bin_id is not None:
        args.extend(["--max-bin-id", str(max_bin_id)])
    if slippage_bps is not None:
        args.extend(["--slippage-bps", str(slippage_bps)])
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    if force_ignore_journal:
        args.append("--force-ignore-journal")
    return run_exec(args, send=send, **kwargs)


def exec_add(
    owner: str,
    position: str,
    sol: float,
    usdc: float,
    *,
    send: bool = False,
    strategy: str = "Spot",
    slippage_bps: Optional[int] = None,
    priority_fee: Optional[int] = None,
    allow_multi_tx: bool = False,
    force_ignore_journal: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = [
        "exec-add",
        "--owner",
        owner,
        "--position",
        position,
        "--sol",
        str(sol),
        "--usdc",
        str(usdc),
        "--strategy",
        strategy,
    ]
    if slippage_bps is not None:
        args.extend(["--slippage-bps", str(slippage_bps)])
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    if allow_multi_tx:
        args.append("--allow-multi-tx")
    if force_ignore_journal:
        args.append("--force-ignore-journal")
    return run_exec(args, send=send, **kwargs)


def exec_claim_fees(
    owner: str,
    position: str,
    *,
    send: bool = False,
    priority_fee: Optional[int] = None,
    force_ignore_journal: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = ["exec-claim-fees", "--owner", owner, "--position", position]
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    if force_ignore_journal:
        args.append("--force-ignore-journal")
    return run_exec(args, send=send, **kwargs)


def exec_withdraw(
    owner: str,
    position: str,
    bps: int,
    *,
    send: bool = False,
    priority_fee: Optional[int] = None,
    force_ignore_journal: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = [
        "exec-withdraw",
        "--owner",
        owner,
        "--position",
        position,
        "--bps",
        str(bps),
    ]
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    if force_ignore_journal:
        args.append("--force-ignore-journal")
    return run_exec(args, send=send, **kwargs)


def exec_close(
    owner: str,
    position: str,
    *,
    send: bool = False,
    priority_fee: Optional[int] = None,
    force_ignore_journal: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = ["exec-close", "--owner", owner, "--position", position]
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    if force_ignore_journal:
        args.append("--force-ignore-journal")
    return run_exec(args, send=send, **kwargs)


def exec_close_empty(
    owner: str,
    position: str,
    *,
    send: bool = False,
    priority_fee: Optional[int] = None,
    force_ignore_journal: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Close zero-liquidity position via SDK closePositionIfEmpty."""
    args = ["exec-close-empty", "--owner", owner, "--position", position]
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    if force_ignore_journal:
        args.append("--force-ignore-journal")
    return run_exec(args, send=send, **kwargs)


def exec_swap(
    owner: str,
    side: str,
    amount: float,
    *,
    send: bool = False,
    slippage_bps: Optional[int] = None,
    priority_fee: Optional[int] = None,
    force_ignore_journal: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = [
        "exec-swap",
        "--owner",
        owner,
        "--side",
        side,
        "--amount",
        str(amount),
    ]
    if slippage_bps is not None:
        args.extend(["--slippage-bps", str(slippage_bps)])
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    if force_ignore_journal:
        args.append("--force-ignore-journal")
    return run_exec(args, send=send, **kwargs)


def resolve_journal(
    *,
    timeout_ms: int = 60_000,
    network: str = "devnet",
    rpc: Optional[str] = None,
    pool: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
    timeout_s: float = 120.0,
) -> Dict[str, Any]:
    """Re-poll unresolved journal entries (no send). Chat-reachable unlock step 1."""
    return run_exec(
        ["resolve-journal", "--timeout-ms", str(int(timeout_ms))],
        send=False,
        network=network,
        rpc=rpc,
        pool=pool,
        extra_env=extra_env,
        timeout_s=timeout_s,
    )


def forget_unresolved_journal(*, reason: str = "owner cleared via Telegram") -> Dict[str, Any]:
    """Mark remaining pending/unknown rows as failed so money paths unblock.

    Does NOT send anything. Owner must have checked explorer first.
    """
    journal_path = ROOT / "state" / "exec_journal.jsonl"
    if not journal_path.is_file():
        return {"ok": True, "cleared": 0, "stillUnresolved": []}
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    entries: List[Dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        entries.append(json.loads(line))
    # Latest row per signature wins (same as TS unresolvedEntries).
    by_sig: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        by_sig[str(e.get("signature"))] = e
    cleared = 0
    for sig, e in by_sig.items():
        if e.get("status") in ("pending", "unknown"):
            # Patch the last matching line in the full list.
            for i in range(len(entries) - 1, -1, -1):
                if entries[i].get("signature") == sig:
                    entries[i] = {
                        **entries[i],
                        "status": "failed",
                        "error": reason,
                    }
                    cleared += 1
                    break
    body = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)
    journal_path.write_text(body + ("\n" if body else ""), encoding="utf-8")
    remaining = [
        e
        for e in {e.get("signature"): e for e in entries}.values()
        if e.get("status") in ("pending", "unknown")
    ]
    return {
        "ok": True,
        "cleared": cleared,
        "stillUnresolved": [
            {"signature": e.get("signature"), "status": e.get("status")}
            for e in remaining
        ],
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Python bridge to ts/dist/exec.js")
    p.add_argument(
        "command",
        choices=[
            "exec-open",
            "exec-add",
            "exec-claim-fees",
            "exec-withdraw",
            "exec-close",
            "exec-close-empty",
            "exec-swap",
        ],
    )
    p.add_argument("--owner")
    p.add_argument("--position")
    p.add_argument("--sol", type=float)
    p.add_argument("--usdc", type=float)
    p.add_argument("--bps", type=int)
    p.add_argument("--min-bin-id", type=int)
    p.add_argument("--max-bin-id", type=int)
    p.add_argument("--strategy", default="Spot")
    p.add_argument("--slippage-bps", type=int, default=None)
    p.add_argument("--priority-fee", type=int, default=None)
    p.add_argument("--allow-multi-tx", action="store_true")
    p.add_argument("--side", choices=["sol-to-usdc", "usdc-to-sol"])
    p.add_argument("--amount", type=float)
    p.add_argument("--network", default="devnet")
    p.add_argument("--rpc", default=None)
    p.add_argument("--pool", default=None)
    p.add_argument("--send", action="store_true")
    p.add_argument("--force-ignore-journal", action="store_true")
    p.add_argument("--raw", action="store_true")
    args = p.parse_args()

    kwargs = {
        "rpc": args.rpc,
        "pool": args.pool,
        "network": args.network,
        "send": args.send,
    }
    try:
        if args.command == "exec-open":
            if not args.owner or args.sol is None or args.usdc is None:
                p.error("--owner --sol --usdc required")
            out = exec_open(
                args.owner,
                args.sol,
                args.usdc,
                send=args.send,
                min_bin_id=args.min_bin_id,
                max_bin_id=args.max_bin_id,
                strategy=args.strategy,
                slippage_bps=args.slippage_bps,
                priority_fee=args.priority_fee,
                force_ignore_journal=args.force_ignore_journal,
                **kwargs,
            )
        elif args.command == "exec-add":
            if not args.owner or not args.position or args.sol is None or args.usdc is None:
                p.error("--owner --position --sol --usdc required")
            out = exec_add(
                args.owner,
                args.position,
                args.sol,
                args.usdc,
                send=args.send,
                strategy=args.strategy,
                slippage_bps=args.slippage_bps,
                priority_fee=args.priority_fee,
                allow_multi_tx=args.allow_multi_tx,
                force_ignore_journal=args.force_ignore_journal,
                **kwargs,
            )
        elif args.command == "exec-claim-fees":
            if not args.owner or not args.position:
                p.error("--owner --position required")
            out = exec_claim_fees(
                args.owner,
                args.position,
                send=args.send,
                priority_fee=args.priority_fee,
                force_ignore_journal=args.force_ignore_journal,
                **kwargs,
            )
        elif args.command == "exec-withdraw":
            if not args.owner or not args.position or args.bps is None:
                p.error("--owner --position --bps required")
            out = exec_withdraw(
                args.owner,
                args.position,
                args.bps,
                send=args.send,
                priority_fee=args.priority_fee,
                force_ignore_journal=args.force_ignore_journal,
                **kwargs,
            )
        elif args.command == "exec-close":
            if not args.owner or not args.position:
                p.error("--owner --position required")
            out = exec_close(
                args.owner,
                args.position,
                send=args.send,
                priority_fee=args.priority_fee,
                force_ignore_journal=args.force_ignore_journal,
                **kwargs,
            )
        elif args.command == "exec-close-empty":
            if not args.owner or not args.position:
                p.error("--owner --position required")
            out = exec_close_empty(
                args.owner,
                args.position,
                send=args.send,
                priority_fee=args.priority_fee,
                force_ignore_journal=args.force_ignore_journal,
                **kwargs,
            )
        elif args.command == "exec-swap":
            if not args.owner or not args.side or args.amount is None:
                p.error("--owner --side --amount required")
            out = exec_swap(
                args.owner,
                args.side,
                args.amount,
                send=args.send,
                slippage_bps=args.slippage_bps,
                priority_fee=args.priority_fee,
                force_ignore_journal=args.force_ignore_journal,
                **kwargs,
            )
        else:
            p.error("unknown command")
    except MeteoraExecError as exc:
        print(json.dumps(exc.payload, ensure_ascii=False, indent=2))
        sys.exit(1)

    if args.raw:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
