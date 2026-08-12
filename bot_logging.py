"""Console + rotating state-volume log, with secret redaction.

Console format stays ``%(asctime)s [%(levelname)s] %(message)s`` so
``docker logs`` looks as it did. The file handler is extra and must never
crash the bot: if the state dir is not writable we warn and continue.
"""
from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, urlparse

import bot_config
import state_paths

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

# 10 MiB × 5 files (active + 4 backups) — 50 MiB ceiling.
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 4

_API_KEY_QUERY = re.compile(
    r"([?&](?:api[-_]?key)=)[^&\s\"']+", re.IGNORECASE
)
_TG_BOT_URL = re.compile(
    r"(https://api\.telegram\.org/bot)[^/\s\"']+", re.IGNORECASE
)

_FILE_HANDLER_MARK = "_meteora_state_file"
_CONSOLE_HANDLER_MARK = "_meteora_console"


def _needles() -> list[str]:
    """Current secret strings. Recomputed each call so tests can patch config."""
    found: list[str] = []
    token = (bot_config.TELEGRAM_BOT_TOKEN or "").strip()
    if token and not bot_config.is_placeholder(token) and len(token) >= 8:
        found.append(token)
    for raw in (
        os.getenv("SOLANA_RPC_URL", ""),
        os.getenv("SOLANA_DEVNET_RPC_URL", ""),
        bot_config.effective_rpc(),
    ):
        url = (raw or "").strip()
        if not url:
            continue
        if "://" in url:
            found.append(url)
        parsed = urlparse(url)
        for key, values in parse_qs(parsed.query).items():
            if key.lower() in ("api-key", "api_key", "apikey"):
                found.extend(v for v in values if v)
    try:
        kp = bot_config.wallet_keypair_path()
        if kp.is_file():
            body = kp.read_text(encoding="utf-8", errors="replace").strip()
            if body:
                found.append(body)
    except OSError:
        pass
    # Longest first so a URL containing a key is replaced before the key alone.
    uniq: list[str] = []
    seen: set[str] = set()
    for n in sorted(found, key=len, reverse=True):
        if n and n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def redact_secrets(text: str) -> str:
    """Strip known secrets and URL query keys from a log fragment."""
    if not text:
        return text
    out = text
    for needle in _needles():
        if needle in out:
            out = out.replace(needle, "***")
    out = _API_KEY_QUERY.sub(r"\1***", out)
    out = _TG_BOT_URL.sub(r"\1***", out)
    return out


class RedactingFormatter(logging.Formatter):
    """Same layout as before, after secrets have been cut out (incl. tracebacks)."""

    def format(self, record: logging.LogRecord) -> str:
        original_msg = record.msg
        original_args = record.args
        try:
            rendered = record.getMessage()
            record.msg = redact_secrets(rendered)
            record.args = ()
            return redact_secrets(super().format(record))
        finally:
            record.msg = original_msg
            record.args = original_args


def configure_console_logging() -> None:
    """Idempotent console handler — same format ``docker logs`` already had."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in root.handlers:
        if getattr(handler, _CONSOLE_HANDLER_MARK, False):
            return
    if root.handlers:
        # Tests / a prior basicConfig already attached a stream handler.
        # Keep it; only stamp our formatter when it is still the default.
        return
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(LOG_FORMAT))
    setattr(handler, _CONSOLE_HANDLER_MARK, True)
    root.addHandler(handler)


def _file_handlers() -> list[logging.Handler]:
    return [
        h
        for h in logging.getLogger().handlers
        if getattr(h, _FILE_HANDLER_MARK, False)
    ]


def detach_state_file_log() -> None:
    """Remove our rotating handler (tests)."""
    root = logging.getLogger()
    for handler in _file_handlers():
        root.removeHandler(handler)
        handler.close()


def attach_state_file_log() -> bool:
    """Add RotatingFileHandler under the state volume. Never raises.

    Returns True if a file handler is now attached.
    """
    if _file_handlers():
        return True
    try:
        log_path = state_paths.bot_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            str(log_path),
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(RedactingFormatter(LOG_FORMAT))
        setattr(handler, _FILE_HANDLER_MARK, True)
        logging.getLogger().addHandler(handler)
        return True
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "state log file unavailable (%s) — console only",
            exc,
        )
        return False
