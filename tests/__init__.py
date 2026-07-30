"""Offline test package defaults — hermetic wallet path (C9).

Must run before test modules import bot_config. Do not put personal paths here.
"""
from __future__ import annotations

import os
from pathlib import Path

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "offline_keypair.json"
os.environ["WALLET_KEYPAIR_PATH"] = str(_FIXTURE)

# If bot_config was already imported (rare), force the path used by wallet_pubkey.
try:
    import bot_config as _bc

    _bc.WALLET_KEYPAIR_PATH = str(_FIXTURE)
except Exception:
    pass
