"""Persisted /pauza and /stop flags (survive container restart)."""
from __future__ import annotations

import json
import logging
from typing import Any

import state_paths

log = logging.getLogger(__name__)

FILENAME = "bot_mode.json"


def _path():
    return state_paths.path(FILENAME)


def format_boot_mode_line(*, paused: bool, frozen: bool, source: str) -> str:
    """Human line for the startup Telegram message and the log."""
    if frozen:
        mode = "заморозка (/stop)"
    elif paused:
        mode = "пауза (/pauza)"
    else:
        mode = "боевой"
    if source == "missing":
        why = "файла режима не было — не торгую, пока не скажешь /boevoy"
    elif source == "corrupt":
        why = "файл режима битый — не торгую, пока не скажешь /boevoy"
    else:
        why = "восстановлен с диска"
    return f"Режим: {mode} ({why})"


def load_mode() -> tuple[bool, bool, str]:
    """Return (paused, frozen, source). Missing/corrupt → paused, not combat."""
    path = _path()
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True, False, "missing"
    except Exception:
        log.warning("bot_mode.json unreadable — pausing", exc_info=True)
        return True, False, "corrupt"
    if not isinstance(raw, dict):
        log.warning("bot_mode.json is not an object — pausing")
        return True, False, "corrupt"
    try:
        paused = bool(raw.get("bot_paused"))
        frozen = bool(raw.get("bot_frozen"))
    except Exception:
        log.warning("bot_mode.json fields unreadable — pausing", exc_info=True)
        return True, False, "corrupt"
    return paused, frozen, "file"


def save_mode(paused: bool, frozen: bool) -> None:
    """Atomic write (temp file + replace), same pattern as monitor_timers."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bot_paused": bool(paused),
        "bot_frozen": bool(frozen),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)
