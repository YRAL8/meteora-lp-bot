import asyncio
import logging

import bot_mode_state

log = logging.getLogger(__name__)

# /pauza — monitor loop paused; manual money commands still work.
# /stop — full freeze; money commands blocked (except /withdraw confirm —
# emergency exit, same as Orca).
# /boevoy clears both.
# RAM flags are the live source during a process; disk is restored at startup.
bot_paused = False
bot_frozen = False

# Shared lock for /open, /addliquidity, /withdraw, /rebalance (Orca rebalance_lock).
money_lock = asyncio.Lock()
# Back-compat alias used during C1.
exec_lock = money_lock


def persist_mode() -> None:
    """Write current flags to disk. Never raises."""
    try:
        bot_mode_state.save_mode(bot_paused, bot_frozen)
    except Exception:
        log.exception("failed to persist bot mode — RAM flags kept")


def restore_mode() -> str:
    """Load flags from disk. Missing/corrupt → pause. Never raises.

    Returns the boot line (also logged).
    """
    global bot_paused, bot_frozen
    try:
        paused, frozen, source = bot_mode_state.load_mode()
    except Exception:
        log.exception("mode restore failed — pausing")
        paused, frozen, source = True, False, "corrupt"
    bot_paused = paused
    bot_frozen = frozen
    line = bot_mode_state.format_boot_mode_line(
        paused=paused, frozen=frozen, source=source
    )
    log.info("%s", line)
    try:
        bot_mode_state.save_mode(bot_paused, bot_frozen)
    except Exception:
        log.exception("failed to persist restored bot mode")
    return line
