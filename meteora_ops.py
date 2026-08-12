#!/usr/bin/env python3
"""Thin bridge to the TypeScript dry-run CLI (ts/dist/cli.js).

No rebalance / Telegram / accounting logic here — only invoke node, parse JSON,
raise on ok:false. Human-readable __main__ for manual checks.
"""
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
CLI_JS = ROOT / "ts" / "dist" / "cli.js"

# Node prints this twice per CLI call when the native bigint addon is missing.
# Exact line only — any extra character must still reach the log.
BIGINT_NOISE_LINE = (
    "bigint: Failed to load bindings, pure JS will be used (try npm run rebuild?)"
)


def is_bigint_noise_line(line: str) -> bool:
    return line.strip() == BIGINT_NOISE_LINE


def emit_cli_stderr(stderr: str, *, file=None) -> None:
    """Print child stderr, dropping only the exact bigint-bindings noise line."""
    out = sys.stderr if file is None else file
    for line in stderr.splitlines():
        if is_bigint_noise_line(line):
            continue
        print(line, file=out)


class MeteoraOpsError(RuntimeError):
    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        msg = payload.get("error") or json.dumps(payload, ensure_ascii=False)
        stage = payload.get("stage")
        super().__init__(f"{msg}" + (f" (stage={stage})" if stage else ""))


def _ensure_cli_built() -> None:
    if CLI_JS.is_file():
        return
    raise FileNotFoundError(
        f"missing {CLI_JS}; build with: (cd ts && npx tsc -p tsconfig.json)"
    )


def run_cli(
    args: List[str],
    *,
    rpc: Optional[str] = None,
    pool: Optional[str] = None,
    timeout_s: float = 180.0,
) -> Dict[str, Any]:
    """Run `node ts/dist/cli.js ...` and return the parsed JSON object from stdout."""
    _ensure_cli_built()
    cmd = ["node", str(CLI_JS), *args]
    if rpc:
        cmd.extend(["--rpc", rpc])
    elif "SOLANA_RPC_URL" not in os.environ:
        cmd.extend(["--rpc", config.solana_rpc_url()])
    if pool:
        cmd.extend(["--pool", pool])
    elif any(a == "--pool" or a.startswith("--pool=") for a in args):
        pass
    else:
        cmd.extend(["--pool", config.dlmm_lb_pair()])

    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=os.environ.copy(),
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if stderr:
        emit_cli_stderr(stderr)

    if not stdout:
        raise RuntimeError(
            f"cli produced empty stdout (exit={proc.returncode}). stderr={stderr[:500]}"
        )

    line = stdout.splitlines()[-1]
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"cli stdout is not JSON: {line[:300]!r} (exit={proc.returncode})"
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"cli JSON root must be object, got {type(payload)}")

    if not payload.get("ok", False):
        raise MeteoraOpsError(payload)

    if proc.returncode != 0:
        raise RuntimeError(
            f"cli exit={proc.returncode} but ok:true payload={payload.get('action')}"
        )
    return payload


def pool_info(**kwargs: Any) -> Dict[str, Any]:
    return run_cli(["pool-info"], **kwargs)


def list_positions(owner: str, **kwargs: Any) -> Dict[str, Any]:
    return run_cli(["list-positions", "--owner", owner], **kwargs)


def balances(owner: str, **kwargs: Any) -> Dict[str, Any]:
    return run_cli(["balances", "--owner", owner], **kwargs)


def suggest_amounts(
    owner: str,
    *,
    budget_sol: Optional[float] = None,
    budget_usdc: Optional[float] = None,
    min_bin_id: Optional[int] = None,
    max_bin_id: Optional[int] = None,
    half_width: Optional[int] = None,
    strategy: str = "Spot",
    **kwargs: Any,
) -> Dict[str, Any]:
    args = ["suggest-amounts", "--owner", owner, "--strategy", strategy]
    if budget_sol is not None:
        args.extend(["--budget-sol", str(budget_sol)])
    if budget_usdc is not None:
        args.extend(["--budget-usdc", str(budget_usdc)])
    if min_bin_id is not None:
        args.extend(["--min-bin-id", str(min_bin_id)])
    if max_bin_id is not None:
        args.extend(["--max-bin-id", str(max_bin_id)])
    if half_width is not None:
        args.extend(["--half-width", str(half_width)])
    return run_cli(args, **kwargs)


def build_open(
    owner: str,
    sol: float,
    usdc: float,
    *,
    min_bin_id: Optional[int] = None,
    max_bin_id: Optional[int] = None,
    strategy: str = "Spot",
    slippage_bps: Optional[int] = None,
    priority_fee: Optional[int] = None,
    position_pubkey: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = [
        "build-open",
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
    if position_pubkey is not None:
        args.extend(["--position-pubkey", position_pubkey])
    return run_cli(args, **kwargs)


def build_add(
    owner: str,
    position: str,
    sol: float,
    usdc: float,
    *,
    strategy: str = "Spot",
    slippage_bps: Optional[int] = None,
    priority_fee: Optional[int] = None,
    allow_multi_tx: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = [
        "build-add",
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
    return run_cli(args, **kwargs)


def build_claim_fees(
    owner: str,
    position: str,
    *,
    priority_fee: Optional[int] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = ["build-claim-fees", "--owner", owner, "--position", position]
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    return run_cli(args, **kwargs)


def build_withdraw(
    owner: str,
    position: str,
    bps: int,
    *,
    priority_fee: Optional[int] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = [
        "build-withdraw",
        "--owner",
        owner,
        "--position",
        position,
        "--bps",
        str(bps),
    ]
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    return run_cli(args, **kwargs)


def build_close(
    owner: str,
    position: str,
    *,
    priority_fee: Optional[int] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = ["build-close", "--owner", owner, "--position", position]
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    return run_cli(args, **kwargs)


def build_swap(
    owner: str,
    side: str,
    amount: float,
    *,
    slippage_bps: Optional[int] = None,
    priority_fee: Optional[int] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = [
        "build-swap",
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
    return run_cli(args, **kwargs)


def _print_human(payload: Dict[str, Any]) -> None:
    action = payload.get("action")
    print(f"ok action={action}")
    if action == "pool-info":
        print(
            f"  activeId={payload.get('activeId')} binStep={payload.get('binStep')} "
            f"usdcPerSol≈{payload.get('usdcPerSol')} "
            f"reserves={payload.get('reserves')}"
        )
        return
    if action == "list-positions":
        print(f"  owner={payload.get('owner')} count={payload.get('count')}")
        for p in payload.get("positions") or []:
            print(
                f"  - {p.get('pubkey')} bins=[{p.get('lowerBinId')},{p.get('upperBinId')}] "
                f"SOL={p.get('sol')} USDC={p.get('usdc')} fees={p.get('fees')}"
            )
        return
    if action == "balances":
        print(
            f"  owner={payload.get('owner')} sol={payload.get('sol')} "
            f"usdc={payload.get('usdc')} solAvailableForOpen={payload.get('solAvailableForOpen')}"
        )
        return
    if action == "suggest-amounts":
        print(
            f"  targetSolFraction={payload.get('targetSolFraction')} "
            f"needSol={payload.get('needSol')} needUsdc={payload.get('needUsdc')} "
            f"swap={payload.get('swapSuggestion')}"
        )
        return
    sims = payload.get("simulation") or []
    txs = payload.get("txs") or []
    print(f"  owner={payload.get('owner')} txs={len(txs)}")
    for t in txs:
        print(
            f"  tx[{t.get('index')}] kind={t.get('kind')} ixs={t.get('numInstructions')} "
            f"signers={t.get('signers')} programs={t.get('programIds')}"
        )
    for s in sims:
        print(
            f"  sim[{s.get('index')}] err={s.get('err')!r} "
            f"cu={s.get('unitsConsumed')} logsTail={s.get('logsTail')}"
        )
    for n in payload.get("notes") or []:
        print(f"  note: {n}")


def main() -> None:
    p = argparse.ArgumentParser(description="Python bridge to ts/dist/cli.js (simulate only)")
    p.add_argument(
        "command",
        choices=[
            "pool-info",
            "list-positions",
            "balances",
            "suggest-amounts",
            "build-open",
            "build-add",
            "build-claim-fees",
            "build-withdraw",
            "build-close",
            "build-swap",
        ],
    )
    p.add_argument("--owner")
    p.add_argument("--position")
    p.add_argument("--position-pubkey")
    p.add_argument("--sol", type=float)
    p.add_argument("--usdc", type=float)
    p.add_argument("--bps", type=int)
    p.add_argument("--min-bin-id", type=int)
    p.add_argument("--max-bin-id", type=int)
    p.add_argument("--half-width", type=int)
    p.add_argument("--budget-sol", type=float)
    p.add_argument("--budget-usdc", type=float)
    p.add_argument("--strategy", default="Spot")
    p.add_argument("--slippage-bps", type=int, default=None)
    p.add_argument("--priority-fee", type=int, default=None)
    p.add_argument("--allow-multi-tx", action="store_true")
    p.add_argument("--side", choices=["sol-to-usdc", "usdc-to-sol"])
    p.add_argument("--amount", type=float)
    p.add_argument("--rpc", default=None)
    p.add_argument("--pool", default=None)
    p.add_argument("--raw", action="store_true", help="print full JSON")
    args = p.parse_args()

    kwargs = {"rpc": args.rpc, "pool": args.pool}
    try:
        if args.command == "pool-info":
            out = pool_info(**kwargs)
        elif args.command == "list-positions":
            if not args.owner:
                p.error("--owner required")
            out = list_positions(args.owner, **kwargs)
        elif args.command == "balances":
            if not args.owner:
                p.error("--owner required")
            out = balances(args.owner, **kwargs)
        elif args.command == "suggest-amounts":
            if not args.owner:
                p.error("--owner required")
            out = suggest_amounts(
                args.owner,
                budget_sol=args.budget_sol,
                budget_usdc=args.budget_usdc,
                min_bin_id=args.min_bin_id,
                max_bin_id=args.max_bin_id,
                half_width=args.half_width,
                strategy=args.strategy,
                **kwargs,
            )
        elif args.command == "build-open":
            if not args.owner or args.sol is None or args.usdc is None:
                p.error("--owner --sol --usdc required")
            out = build_open(
                args.owner,
                args.sol,
                args.usdc,
                min_bin_id=args.min_bin_id,
                max_bin_id=args.max_bin_id,
                strategy=args.strategy,
                slippage_bps=args.slippage_bps,
                priority_fee=args.priority_fee,
                position_pubkey=args.position_pubkey,
                **kwargs,
            )
        elif args.command == "build-add":
            if not args.owner or not args.position or args.sol is None or args.usdc is None:
                p.error("--owner --position --sol --usdc required")
            out = build_add(
                args.owner,
                args.position,
                args.sol,
                args.usdc,
                strategy=args.strategy,
                slippage_bps=args.slippage_bps,
                priority_fee=args.priority_fee,
                allow_multi_tx=args.allow_multi_tx,
                **kwargs,
            )
        elif args.command == "build-claim-fees":
            if not args.owner or not args.position:
                p.error("--owner --position required")
            out = build_claim_fees(
                args.owner,
                args.position,
                priority_fee=args.priority_fee,
                **kwargs,
            )
        elif args.command == "build-withdraw":
            if not args.owner or not args.position or args.bps is None:
                p.error("--owner --position --bps required")
            out = build_withdraw(
                args.owner,
                args.position,
                args.bps,
                priority_fee=args.priority_fee,
                **kwargs,
            )
        elif args.command == "build-close":
            if not args.owner or not args.position:
                p.error("--owner --position required")
            out = build_close(
                args.owner,
                args.position,
                priority_fee=args.priority_fee,
                **kwargs,
            )
        elif args.command == "build-swap":
            if not args.owner or not args.side or args.amount is None:
                p.error("--owner --side --amount required")
            out = build_swap(
                args.owner,
                args.side,
                args.amount,
                slippage_bps=args.slippage_bps,
                priority_fee=args.priority_fee,
                **kwargs,
            )
        else:
            p.error("unknown command")
    except MeteoraOpsError as exc:
        print(json.dumps(exc.payload, ensure_ascii=False, indent=2))
        sys.exit(1)

    if args.raw:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        _print_human(out)


if __name__ == "__main__":
    main()
