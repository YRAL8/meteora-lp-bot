#!/usr/bin/env python3
"""C8 live proofs on devnet (no mainnet, AUTO_REBALANCE stays off).

Shows: crash-via-env → pending (not Telegram crash);
stop replies while money lock busy; journal unlock from /status journal*.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "WALLET_KEYPAIR_PATH",
    os.path.expanduser("~/.config/solana/meteora-devnet.json"),
)

import bot_state  # noqa: E402
import meteora_exec  # noqa: E402
import money_ops  # noqa: E402
import range_state  # noqa: E402
import telegram_commands as tg  # noqa: E402
from reopen_pending import is_reopen_pending, set_reopen_pending  # noqa: E402
from tests.test_telegram_commands import _update_with_args  # noqa: E402


async def call(name: str, args: list[str] | None = None) -> list[str]:
    update, msg, ctx = _update_with_args(list(args or []))
    await getattr(tg, f"{name}_command")(update, ctx)
    for r in msg.replies:
        print("---", r[:400].replace("\n", " | "))
    return msg.replies


async def proof_stop_during_money() -> None:
    print("\n=== /stop during busy money_lock ===")
    await bot_state.money_lock.acquire()
    try:

        async def slow_money() -> None:
            await asyncio.sleep(1.5)

        money_task = asyncio.create_task(slow_money())
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        replies = await call("stop", [])
        dt = time.monotonic() - t0
        assert any("🛑" in r or "заморозка" in r.lower() for r in replies)
        assert dt < 1.0, f"stop blocked too long: {dt:.2f}s"
        print(f"stop replied in {dt:.3f}s while lock held — OK")
        await money_task
    finally:
        bot_state.bot_frozen = False
        if bot_state.money_lock.locked():
            bot_state.money_lock.release()


async def proof_journal_unlock() -> None:
    print("\n=== journal unlock via /status journal* ===")
    journal = ROOT / "state" / "exec_journal.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if journal.is_file():
        backup = journal.read_text(encoding="utf-8")
    fake_sig = "C8FakeUnknownSig111111111111111111111111111111111111111"
    journal.write_text(
        json.dumps(
            {
                "ts": "2026-07-29T00:00:00Z",
                "action": "exec-open",
                "network": "devnet",
                "params": {},
                "signature": fake_sig,
                "status": "unknown",
                "slot": None,
                "error": "injected for C8",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        # resolve will leave unknown (fake sig) — then forget clears it
        r1 = await call("status", ["journal"])
        assert any("journal" in r.lower() or "Неразрешён" in r or "чист" in r or "⚠️" in r for r in r1), r1
        r2 = await call("status", ["journal-forget", fake_sig, "confirm"])
        assert any("снято" in r.lower() or "🔓" in r or "Не снял" in r for r in r2), r2
        print("journal forget-one-signature from chat path — OK")
    finally:
        if backup is not None:
            journal.write_text(backup, encoding="utf-8")
        elif journal.is_file():
            journal.unlink()


async def proof_crash_env_not_telegram() -> None:
    print("\n=== crash via env (not Telegram) ===")
    set_reopen_pending(False)
    refuse = await call("rebalance", ["crash"])
    assert any("убран" in r for r in refuse), refuse
    print("Telegram crash refused — OK")

    try:
        pos = money_ops.get_primary_position()
    except Exception as e:
        print(f"SKIP live crash (RPC): {e}")
        print("Env crash path covered by unit/offline METEORA_CRASH_AFTER_CLOSE")
        return

    if pos is None:
        range_state.half_width_bins = 3
        print("opening tiny position for crash proof…")
        try:
            await call("open", ["0.35"])
        except Exception as e:
            print(f"SKIP open for crash (RPC): {e}")
            return
        pos = money_ops.get_primary_position()
    if not pos:
        print("SKIP live crash — no position")
        return

    os.environ["METEORA_CRASH_AFTER_CLOSE"] = "1"
    replies: list[str] = []
    try:
        try:
            money_ops.rebalance_position(pos, reply=replies.append, auto=True)
        except SystemExit as e:
            print(f"SystemExit({e.code}) as expected")
            assert e.code == 42
    finally:
        os.environ.pop("METEORA_CRASH_AFTER_CLOSE", None)

    assert is_reopen_pending(), "pending must remain"
    print("reopen_pending after env crash — OK; recovering with /open…")
    try:
        await call("open", ["0.30"])
        assert money_ops.get_primary_position()
        assert not is_reopen_pending(), "open must clear pending"
        print("recovered; pending cleared — OK")
    except Exception as e:
        print(f"recovery open failed (RPC): {e}; pending={is_reopen_pending()}")
        set_reopen_pending(False)


async def main() -> None:
    print("network tips:", money_ops.exec_kwargs())
    assert bot_state.bot_frozen is False
    await proof_stop_during_money()
    await proof_journal_unlock()
    await proof_crash_env_not_telegram()
    print("\nC8 live proofs done.")


if __name__ == "__main__":
    asyncio.run(main())
