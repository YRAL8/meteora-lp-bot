"""Single source for on-disk bot state paths (C12).

Override with env ``METEORA_STATE_DIR`` so offline tests never touch the real
``<repo>/state`` directory. Resolve paths at call time (or after env is set).
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def state_dir() -> Path:
    raw = os.environ.get("METEORA_STATE_DIR", "").strip()
    if raw:
        return Path(raw)
    return ROOT / "state"


def path(*parts: str) -> Path:
    return state_dir().joinpath(*parts)
