"""Offline test package defaults — hermetic wallet path (C9).

Must run before test modules import bot_config. Do not put personal paths here.

Live/devnet scripts that import helpers from this package must call
`tests.live_guard.begin_live_script()` afterwards to restore the real wallet
(see SAVED_WALLET_KEYPAIR_PATH).
"""
from __future__ import annotations

import os
from pathlib import Path

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "offline_keypair.json"

# Preserve whatever the process had before we force the fixture, so live
# scripts can restore it after importing test helpers.
_prior = os.environ.get("WALLET_KEYPAIR_PATH")
if _prior:
    _prior_resolved = Path(os.path.expanduser(_prior)).resolve()
    SAVED_WALLET_KEYPAIR_PATH: str | None = (
        str(_prior_resolved) if _prior_resolved != _FIXTURE.resolve() else None
    )
else:
    SAVED_WALLET_KEYPAIR_PATH = None

os.environ["WALLET_KEYPAIR_PATH"] = str(_FIXTURE)

# If bot_config was already imported (rare), force the path used by wallet_pubkey.
try:
    import bot_config as _bc

    _bc.WALLET_KEYPAIR_PATH = str(_FIXTURE)
except Exception:
    pass
