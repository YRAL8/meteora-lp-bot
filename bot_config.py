"""Telegram bot configuration — loads .env without modifying config.py."""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

import config
from meteora_position import base58_encode

load_dotenv()

ROOT = Path(__file__).resolve().parent

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WALLET_KEYPAIR_PATH = os.getenv(
    "WALLET_KEYPAIR_PATH", "~/.config/solana/meteora-devnet.json"
).strip()

_TRUE_VALUES = {"true", "1", "yes", "y", "on"}
_PLACEHOLDER_MARKERS = ("YOUR_", "CHANGE_ME", "TODO", "PLACEHOLDER")


def is_placeholder(value: str) -> bool:
    if not value:
        return True
    upper = value.upper()
    return any(marker in upper for marker in _PLACEHOLDER_MARKERS)


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in _TRUE_VALUES


DRY_RUN = _env_bool("DRY_RUN", "true")
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "300"))
if POLL_INTERVAL_SEC < 1:
    raise ValueError("POLL_INTERVAL_SEC must be >= 1")

# --- Auto-rebalance (C4). Default OFF — never spend without an explicit .env opt-in. ---
AUTO_REBALANCE = _env_bool("AUTO_REBALANCE", "false")

# Minutes out of range before acting. 20 is the measured optimum (Binance SOLUSDT
# 5m candles, ±1.37%): ~62% of exits return within 20 min; longer wait loses more
# idle time than it saves in avoided swaps.
REBALANCE_DELAY_MIN = int(os.getenv("REBALANCE_DELAY_MIN", "20"))
if REBALANCE_DELAY_MIN < 0:
    raise ValueError("REBALANCE_DELAY_MIN must be >= 0")

# Meteora-only storm guards (Orca has neither — rebalances are rarer there).
MIN_REBALANCE_INTERVAL_MIN = int(os.getenv("MIN_REBALANCE_INTERVAL_MIN", "60"))
if MIN_REBALANCE_INTERVAL_MIN < 0:
    raise ValueError("MIN_REBALANCE_INTERVAL_MIN must be >= 0")

MAX_REBALANCES_PER_DAY = int(os.getenv("MAX_REBALANCES_PER_DAY", "6"))
if MAX_REBALANCES_PER_DAY < 1:
    raise ValueError("MAX_REBALANCES_PER_DAY must be >= 1")

# Periodic reminders while out-of-range and not acting (manual mode / low SOL /
# daily cap). Orca uses 1h after the 2026-07-28 silent-night incident.
REBALANCE_BLOCKED_REMINDER_HOURS = float(
    os.getenv("REBALANCE_BLOCKED_REMINDER_HOURS", "1")
)

MIN_SOL_BALANCE = float(os.getenv("MIN_SOL_BALANCE", "0.08"))

# Cap on position size in USD (open / add / rebalance reopen). Empty = unlimited
# (wallet≈position model, same as Orca). Set e.g. 50 for safe rehearsals.
# Required when AUTO_REBALANCE is on AND network is mainnet (see main.py).
_raw_max_pos = os.getenv("MAX_POSITION_USD", "").strip()
MAX_POSITION_USD: float | None
if not _raw_max_pos:
    MAX_POSITION_USD = None
else:
    MAX_POSITION_USD = float(_raw_max_pos)
    if MAX_POSITION_USD <= 0:
        raise ValueError("MAX_POSITION_USD must be > 0 when set")

# Priority fee (microlamports per CU). Matches ts DEFAULT; passed via --priority-fee.
PRIORITY_FEE_MICROLAMPORTS = int(os.getenv("PRIORITY_FEE_MICROLAMPORTS", "50000"))
if PRIORITY_FEE_MICROLAMPORTS < 0:
    raise ValueError("PRIORITY_FEE_MICROLAMPORTS must be >= 0")

# Payback diagnosis (warn only, never blocks). Swap cost ≈0.02% of position;
# in-range earn ≈0.0071%/h → payback ≈2.8h. If median cycle life stays below
# this, the width/pool choice is wrong — tell the owner, still rebalance.
REBALANCE_PAYBACK_HOURS = float(os.getenv("REBALANCE_PAYBACK_HOURS", "2.8"))
UNECONOMIC_LOOKBACK_CYCLES = int(os.getenv("UNECONOMIC_LOOKBACK_CYCLES", "5"))
if UNECONOMIC_LOOKBACK_CYCLES < 1:
    raise ValueError("UNECONOMIC_LOOKBACK_CYCLES must be >= 1")


def dry_run() -> bool:
    return DRY_RUN


def effective_network() -> str:
    """DRY_RUN=true always devnet; mainnet only with METEORA_ALLOW_MAINNET=1."""
    if DRY_RUN:
        return "devnet"
    if os.environ.get("METEORA_ALLOW_MAINNET") == "1":
        return "mainnet"
    return "devnet"


def effective_rpc(network: str | None = None) -> str:
    net = network or effective_network()
    if net == "devnet":
        return os.getenv("SOLANA_DEVNET_RPC_URL", "https://api.devnet.solana.com")
    return config.solana_rpc_url()


def pool_pubkey() -> str:
    return os.getenv("DLMM_LB_PAIR", config.dlmm_lb_pair())


def wallet_keypair_path() -> Path:
    return Path(os.path.expanduser(WALLET_KEYPAIR_PATH))


def wallet_pubkey() -> str:
    """Derive the base58 pubkey from a Solana JSON keypair file.

    No solana-keygen dependency: bytes[32:64] of the 64-byte secret array is
    the Ed25519 public key, verified to match `solana-keygen pubkey` output.
    """
    path = wallet_keypair_path()
    if not path.is_file():
        raise FileNotFoundError(f"wallet keypair not found: {path}")
    secret = json.loads(path.read_text())
    if not isinstance(secret, list) or len(secret) != 64:
        raise ValueError(f"unexpected keypair format in {path}")
    return base58_encode(bytes(secret[32:64]))


def explorer_cluster() -> str:
    return "devnet" if effective_network() == "devnet" else "mainnet"
