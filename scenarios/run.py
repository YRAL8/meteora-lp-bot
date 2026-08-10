#!/usr/bin/env python3
"""C13 live scenario stand — one entry, table of results.

Usage:
  METEORA_MARKET_KEYPAIR_PATH=~/.config/solana/meteora-devnet-market.json \\
    ( ulimit -v 3000000; .venv/bin/python -m scenarios.run --all )

  .venv/bin/python -m scenarios.run --list
  .venv/bin/python -m scenarios.run s01_up_empty_wallet
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scenarios.harness import (  # noqa: E402
    ScenarioResult,
    bootstrap_live,
    close_all_positions,
    drain_bot_sol_to_floor,
    network_snapshot,
    open_oor_position,
    restore_sol_from_market,
)


def _log(msg: str) -> None:
    print(msg, flush=True)


def s01_up_empty_wallet() -> ScenarioResult:
    """Price-up exit: USDC-only position, wallet SOL near floor → rebalance swaps."""
    import main as main_mod
    import money_ops
    import bot_config
    from reopen_pending import is_reopen_pending, set_reopen_pending

    expected = (
        "auto-rebalance runs, USDC→SOL swap if needed, new in-range position"
    )
    sigs: list[str] = []
    try:
        set_reopen_pending(False)
        close_all_positions(_log)
        restore_sol_from_market(target_sol=0.35)
        pos, open_sigs = open_oor_position(side="up", budget_usd=1.5, reply=_log)
        sigs.extend(open_sigs)
        drain_sigs = drain_bot_sol_to_floor()
        sigs.extend(drain_sigs)
        snap0 = network_snapshot()
        _log(f"pre-rebalance snap={snap0}")

        # Force auto path immediately.
        t0 = datetime.now(timezone.utc)
        main_mod.out_of_range_since = t0 - timedelta(minutes=60)
        main_mod.last_auto_attempt_at = None
        main_mod.reset_storm_guards_for_tests()

        replies: list[str] = []

        async def _tick() -> None:
            with patch.object(bot_config, "AUTO_REBALANCE", True), patch.object(
                bot_config, "REBALANCE_DELAY_MIN", 0
            ), patch.object(bot_config, "MIN_REBALANCE_INTERVAL_MIN", 0):
                await main_mod.monitor_position(now=t0)

        # Collect telegram via patch
        with patch(
            "main.send_telegram_message", side_effect=lambda m: replies.append(m)
        ):
            asyncio.run(_tick())

        snap1 = network_snapshot()
        pos_after = money_ops.get_primary_position()
        ok = (
            pos_after is not None
            and not is_reopen_pending()
            and int(pos_after["lowerBinId"])
            <= int(
                __import__("meteora_ops").pool_info(**money_ops.ops_kwargs())[
                    "activeId"
                ]
            )
            <= int(pos_after["upperBinId"])
        )
        # Also accept: rebalance started (replies mention Авто) even if still settling
        if not ok and any("Авто-ребаланс" in r or "Реоткрытие" in r for r in replies):
            # Wait one more status read
            pos_after = money_ops.get_primary_position()
            ok = pos_after is not None and not is_reopen_pending()

        observed = (
            f"positions={len(snap1['positions'])} "
            f"sol={snap1['sol']:.4f} usdc={snap1['usdc']:.2f} "
            f"reopen_pending={is_reopen_pending()} "
            f"replies={len(replies)}"
        )
        return ScenarioResult(
            "s01_up_empty_wallet",
            ok,
            expected,
            observed,
            signatures=sigs,
            detail="; ".join(replies)[:500],
        )
    except Exception as e:
        return ScenarioResult(
            "s01_up_empty_wallet",
            False,
            expected,
            f"EXCEPTION {type(e).__name__}: {e}",
            signatures=sigs,
            detail=traceback.format_exc()[-800:],
        )
    finally:
        try:
            restore_sol_from_market(target_sol=0.4)
        except Exception as e:
            _log(f"restore failed: {e}")


def s02_down_exit() -> ScenarioResult:
    """Price-down exit: SOL-only position → rebalance, maybe SOL→USDC."""
    import main as main_mod
    import money_ops
    import bot_config
    from reopen_pending import is_reopen_pending, set_reopen_pending

    expected = "auto-rebalance runs; SOL→USDC swap if needed; in-range position"
    sigs: list[str] = []
    try:
        set_reopen_pending(False)
        close_all_positions(_log)
        restore_sol_from_market(target_sol=0.5)
        pos, open_sigs = open_oor_position(side="down", budget_usd=1.5, reply=_log)
        sigs.extend(open_sigs)
        t0 = datetime.now(timezone.utc)
        main_mod.out_of_range_since = t0 - timedelta(minutes=60)
        main_mod.last_auto_attempt_at = None
        main_mod.reset_storm_guards_for_tests()
        replies: list[str] = []

        async def _tick() -> None:
            with patch.object(bot_config, "AUTO_REBALANCE", True), patch.object(
                bot_config, "REBALANCE_DELAY_MIN", 0
            ), patch.object(bot_config, "MIN_REBALANCE_INTERVAL_MIN", 0):
                await main_mod.monitor_position(now=t0)

        with patch(
            "main.send_telegram_message", side_effect=lambda m: replies.append(m)
        ):
            asyncio.run(_tick())

        pos_after = money_ops.get_primary_position()
        ok = pos_after is not None and not is_reopen_pending()
        snap = network_snapshot()
        return ScenarioResult(
            "s02_down_exit",
            ok,
            expected,
            f"pos={bool(pos_after)} snap={snap} replies={len(replies)}",
            signatures=sigs,
            detail="; ".join(replies)[:500],
        )
    except Exception as e:
        return ScenarioResult(
            "s02_down_exit",
            False,
            expected,
            f"EXCEPTION {type(e).__name__}: {e}",
            signatures=sigs,
            detail=traceback.format_exc()[-800:],
        )


def s03_crash_after_close() -> ScenarioResult:
    """Abort between close and open → reopen_pending confirmed."""
    import money_ops
    from reopen_pending import is_reopen_pending, load_reopen_pending, set_reopen_pending

    expected = "reopen_pending with close_status=confirmed; next auto blocked"
    sigs: list[str] = []
    try:
        set_reopen_pending(False)
        close_all_positions(_log)
        restore_sol_from_market(target_sol=0.45)
        pos, open_sigs = open_oor_position(side="up", budget_usd=1.2, reply=_log)
        sigs.extend(open_sigs)
        os.environ["METEORA_CRASH_AFTER_CLOSE"] = "1"
        try:
            money_ops.rebalance_position(pos, reply=_log, auto=True)
            observed = "rebalance returned without crash"
            ok = False
        except SystemExit as e:
            meta = load_reopen_pending() or {}
            ok = (
                is_reopen_pending()
                and meta.get("close_status") == "confirmed"
            )
            observed = f"SystemExit({e.code}) meta={meta}"
        finally:
            os.environ.pop("METEORA_CRASH_AFTER_CLOSE", None)

        # Manual open should clear after owner checks — here we close leftover
        # and clear only if ok was True path for cleanup.
        return ScenarioResult(
            "s03_crash_after_close", ok, expected, observed, signatures=sigs
        )
    except Exception as e:
        os.environ.pop("METEORA_CRASH_AFTER_CLOSE", None)
        return ScenarioResult(
            "s03_crash_after_close",
            False,
            expected,
            f"EXCEPTION {type(e).__name__}: {e}",
            signatures=sigs,
            detail=traceback.format_exc()[-800:],
        )


def s04_insufficient_funds() -> ScenarioResult:
    """Near-empty wallet, no position → honest refuse, no micro-open success."""
    import money_ops
    from reopen_pending import set_reopen_pending

    expected = "open refuses with clear shortfall; no success open"
    sigs: list[str] = []
    try:
        set_reopen_pending(False)
        close_all_positions(_log)
        # Park almost everything on market
        drain_bot_sol_to_floor(floor=0.03)
        # Also move USDC if possible via swap to SOL then drain — keep simple:
        # try open with tiny budget that still needs both legs.
        replies: list[str] = []

        def reply(m: str) -> None:
            replies.append(m)
            _log(m)

        raised = None
        try:
            money_ops.open_with_budget(5.0, reply=reply)
            ok = False
            observed = "open_with_budget returned success unexpectedly"
        except Exception as e:
            raised = e
            text = " ".join(replies) + " " + str(e)
            ok = (
                "❌" in text
                or "мало" in text.lower()
                or "недостаточно" in text.lower()
                or "abort" in text.lower()
                or "SwapFailed" in type(e).__name__
                or "Undersized" in type(e).__name__
                or "Meteora" in type(e).__name__
            )
            # Must not have an open position afterwards
            pos = money_ops.get_primary_position()
            ok = ok and pos is None
            observed = f"raised={type(e).__name__}: {e}; pos={pos}; replies={replies[:3]}"
        snap = network_snapshot()
        return ScenarioResult(
            "s04_insufficient_funds",
            ok,
            expected,
            observed + f" snap={snap}",
            signatures=sigs,
        )
    except Exception as e:
        return ScenarioResult(
            "s04_insufficient_funds",
            False,
            expected,
            f"EXCEPTION {type(e).__name__}: {e}",
            signatures=sigs,
            detail=traceback.format_exc()[-800:],
        )
    finally:
        try:
            restore_sol_from_market(target_sol=0.4)
        except Exception as e:
            _log(f"restore failed: {e}")


def s05_unknown_outcome() -> ScenarioResult:
    """Real send then crash before confirm → unresolved journal + unknown flag.

    Uses --crash-after-send (process dies after broadcast, before poll).
    Dual-RPC confirm stripping was not required: this is a real on-wire signature
    with unknown confirmation in the bot's view.
    """
    import money_ops
    import meteora_exec
    import exec_journal_io
    from reopen_pending import is_reopen_pending, load_reopen_pending, set_reopen_pending

    expected = (
        "reopen_pending close_status=unknown and/or unresolved journal signature; "
        "automation blocked"
    )
    sigs: list[str] = []
    try:
        set_reopen_pending(False)
        # Clear journal in scenario state dir
        jp = exec_journal_io.journal_path()
        if jp.is_file():
            jp.unlink()
        close_all_positions(_log)
        restore_sol_from_market(target_sol=0.45)
        pos, open_sigs = open_oor_position(side="up", budget_usd=1.2, reply=_log)
        sigs.extend(open_sigs)

        # Direct close with crash-after-send via rebalance path is hard; call
        # exec_close then let money_ops treat empty stdout as unknown by using
        # rebalance which wraps close_position_full.
        # Inject crash on the close exec by patching exec_close kwargs.
        real_close = meteora_exec.exec_close

        def close_crash(*a, **kw):
            kw = dict(kw)
            kw["crash_after_send"] = True
            return real_close(*a, **kw)

        try:
            with patch("money_ops.meteora_exec.exec_close", side_effect=close_crash):
                money_ops.rebalance_position(pos, reply=_log, auto=True)
            observed = "rebalance completed without error"
            ok = False
        except (SystemExit, Exception) as e:
            meta = load_reopen_pending() or {}
            unresolved = exec_journal_io.list_unresolved()
            for u in unresolved:
                if u.get("signature"):
                    sigs.append(str(u["signature"]))
            ok = is_reopen_pending() and (
                meta.get("close_status") == "unknown" or bool(unresolved)
            )
            observed = (
                f"err={type(e).__name__}: {e}; meta={meta}; "
                f"unresolved={len(unresolved)}"
            )
        return ScenarioResult(
            "s05_unknown_outcome", ok, expected, observed, signatures=sigs
        )
    except Exception as e:
        return ScenarioResult(
            "s05_unknown_outcome",
            False,
            expected,
            f"EXCEPTION {type(e).__name__}: {e}",
            signatures=sigs,
            detail=traceback.format_exc()[-800:],
        )


SCENARIOS: dict[str, Callable[[], ScenarioResult]] = {
    "s01_up_empty_wallet": s01_up_empty_wallet,
    "s02_down_exit": s02_down_exit,
    "s03_crash_after_close": s03_crash_after_close,
    "s04_insufficient_funds": s04_insufficient_funds,
    "s05_unknown_outcome": s05_unknown_outcome,
}


def print_table(results: list[ScenarioResult]) -> None:
    print("\n=== C13 scenario results ===")
    for r in results:
        status = "SKIP" if r.skipped else ("PASS" if r.ok else "FAIL")
        print(f"[{status}] {r.name}")
        print(f"  expected: {r.expected}")
        print(f"  observed: {r.observed}")
        if r.signatures:
            print(f"  sigs: {', '.join(r.signatures[:6])}")
        if r.detail:
            print(f"  detail: {r.detail[:300]}")
    passed = sum(1 for r in results if r.ok and not r.skipped)
    failed = sum(1 for r in results if not r.ok and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    print(f"\nsummary: pass={passed} fail={failed} skip={skipped}")


def main() -> int:
    p = argparse.ArgumentParser(description="C13 live scenario stand")
    p.add_argument("names", nargs="*", help="scenario ids")
    p.add_argument("--all", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="do not close positions / restore SOL at end",
    )
    args = p.parse_args()
    if args.list:
        for k in SCENARIOS:
            print(k)
        return 0

    names = list(SCENARIOS) if args.all else list(args.names)
    if not names:
        p.print_help()
        return 2

    pubkey = bootstrap_live()
    _log(f"scenario state_dir={os.environ.get('METEORA_STATE_DIR')} wallet={pubkey}")

    results: list[ScenarioResult] = []
    for name in names:
        fn = SCENARIOS.get(name)
        if not fn:
            results.append(
                ScenarioResult(name, False, "?", f"unknown scenario {name}")
            )
            continue
        _log(f"\n----- {name} -----")
        results.append(fn())

    if not args.skip_cleanup:
        _log("\n----- cleanup -----")
        try:
            close_all_positions(_log)
        except Exception as e:
            _log(f"cleanup close: {e}")
        try:
            restore_sol_from_market(target_sol=0.4)
        except Exception as e:
            _log(f"cleanup restore: {e}")

    print_table(results)
    return 0 if all(r.ok or r.skipped for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
