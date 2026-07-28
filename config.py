import os


SOLANA_MAINNET_RPC_DEFAULT = "https://api.mainnet-beta.solana.com"

# Meteora DLMM program id (mainnet).
DLMM_PROGRAM_ID = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"

# Default pool requested in the task (SOL-USDC).
DEFAULT_LB_PAIR = "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"


def solana_rpc_url() -> str:
    return os.environ.get("SOLANA_RPC_URL", SOLANA_MAINNET_RPC_DEFAULT)


def dlmm_lb_pair() -> str:
    return os.environ.get("DLMM_LB_PAIR", DEFAULT_LB_PAIR)


def request_sleep_s() -> float:
    try:
        return float(os.environ.get("REQUEST_SLEEP_S", "0.25"))
    except Exception:
        return 0.25

