"""Run only the hourly recorders: no Telegram, no wallet, no signing, no money.

The point of this entrypoint is time. A verdict about a pool needs a long series
of hours — how much it pays and how tightly its money is packed at the price —
and that series can start weeks before there is a wallet, a token, or a position.
"""
from __future__ import annotations

import asyncio
import logging

import bin_snapshots
import bot_config
import pool_snapshots

log = logging.getLogger("snapshots")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(name)s: %(message)s",
    )
    log.info(
        "recording pool %s via %s every %ds",
        bot_config.snapshot_pool(),
        bot_config.snapshot_rpc(),
        pool_snapshots.INTERVAL_SEC,
    )
    await asyncio.gather(
        pool_snapshots.snapshot_loop(),
        bin_snapshots.snapshot_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
