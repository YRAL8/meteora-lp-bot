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
