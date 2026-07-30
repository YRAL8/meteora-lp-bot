"""state/ volume marker — detects wiped ephemeral container storage."""
from __future__ import annotations

import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
VOLUME_MARKER = STATE_DIR / ".volume_ok"

log = logging.getLogger(__name__)


def ensure_state_dir() -> tuple[bool, str]:
    """Ensure state/ exists; return (looks_persistent, human status line).

    Persistent = marker file already present (survived a prior boot with a volume).
    First boot creates the marker and reports that the next restart must keep it.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if VOLUME_MARKER.is_file():
        return True, f"state/ marker OK ({VOLUME_MARKER.name})"
    VOLUME_MARKER.write_text(
        "meteora-lp-bot state volume marker — must survive container recreate\n",
        encoding="utf-8",
    )
    return (
        False,
        "state/ marker was MISSING at start — mount a volume on /app/state "
        "or state/ will be wiped on recreate",
    )
