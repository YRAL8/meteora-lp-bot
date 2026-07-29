import asyncio

# /pauza — monitor loop paused; manual money commands still work.
# /stop — full freeze; money commands blocked (except /withdraw confirm —
# emergency exit, same as Orca).
# /boevoy clears both.
bot_paused = False
bot_frozen = False

# Shared lock for /open, /addliquidity, /withdraw, /rebalance (Orca rebalance_lock).
money_lock = asyncio.Lock()
# Back-compat alias used during C1.
exec_lock = money_lock
