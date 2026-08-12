#!/usr/bin/env python3
"""Thin bridge to ts/dist/exec.js (sign + send on devnet by default).

Owns the send journal: streams stderr for ``@@JOURNAL`` markers, appends pending
rows before the child can finish the send, and resolves unresolved signatures
via JSON-RPC ``getSignatureStatuses`` (no Node journal).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests

import config
import exec_journal_io
import meteora_ops

ROOT = Path(__file__).resolve().parent
EXEC_JS = ROOT / "ts" / "dist" / "exec.js"

# Resolve budgets (former resolve-journal CLI in exec.ts).
RESOLVE_PER_SIG_TIMEOUT_MS = 25_000
RESOLVE_WALL_MS = 170_000
RESOLVE_POLL_MS = 1_500
# Startup re-poll before a new send (former resolveJournalOnStartup).
STARTUP_RESOLVE_TIMEOUT_MS = 15_000
STARTUP_RESOLVE_POLL_MS = 1_500


def exec_journal_path() -> Path:
    return exec_journal_io.journal_path()


def exec_journal_lock_path() -> Path:
    return exec_journal_io.lock_path()


def list_unresolved_journal() -> List[Dict[str, Any]]:
    """Latest row per signature still pending/unknown (no RPC)."""
    return exec_journal_io.list_unresolved()


class MeteoraExecError(RuntimeError):
    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        msg = payload.get("error") or json.dumps(payload, ensure_ascii=False)
        stage = payload.get("stage")
        super().__init__(f"{msg}" + (f" (stage={stage})" if stage else ""))


def _ensure_exec_built() -> None:
    if EXEC_JS.is_file():
        return
    raise FileNotFoundError(
        f"missing {EXEC_JS}; build with: (cd ts && npx tsc -p tsconfig.json)"
    )


def rpc_host_for_log(rpc_url: str) -> str:
    """Host only — RPC URLs often carry api-key= in the query."""
    try:
        host = urlparse(rpc_url).hostname
        return host or "(rpc)"
    except Exception:
        return "(rpc)"


def _default_rpc(network: str, rpc: Optional[str] = None) -> str:
    if rpc:
        return rpc
    if network == "devnet":
        return os.environ.get(
            "SOLANA_DEVNET_RPC_URL", "https://api.devnet.solana.com"
        )
    return os.environ.get("SOLANA_RPC_URL", config.solana_rpc_url())


def explorer_tx_url(signature: str, network: str) -> str:
    base = f"https://explorer.solana.com/tx/{signature}"
    return f"{base}?cluster=devnet" if network == "devnet" else base


def is_transient_rpc_error(msg: str) -> bool:
    """Port of former TS ``isTransientRpcError`` — do not mark these failed."""
    low = (msg or "").lower()
    return (
        "429" in msg
        or "too many requests" in low
        or "rate limit" in low
        or "fetch failed" in low
        or "socket hang up" in low
        or "network error" in low
        or "econnreset" in low
        or "econnrefused" in low
        or "etimedout" in low
        or "timeout" in low
        or "eai_again" in low
        or "502" in low
        or "503" in low
        or "504" in low
    )


def _rpc_get_signature_statuses(
    rpc_url: str,
    signature: str,
    *,
    session: Optional[requests.Session] = None,
    timeout_s: float = 20.0,
) -> Any:
    """Call getSignatureStatuses; raise RuntimeError with message on transport/RPC errors."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignatureStatuses",
        "params": [[signature], {"searchTransactionHistory": True}],
    }
    sess = session or requests
    try:
        resp = sess.post(
            rpc_url,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=timeout_s,
        )
        resp.raise_for_status()
        raw = resp.json()
    except requests.RequestException as exc:
        raise RuntimeError(str(exc)) from exc
    except ValueError as exc:
        raise RuntimeError(f"invalid JSON from RPC: {exc}") from exc
    if raw.get("error"):
        raise RuntimeError(f"RPC error: {raw['error']}")
    value = (raw.get("result") or {}).get("value") or [None]
    return value[0] if value else None


def poll_signature_status(
    rpc_url: str,
    signature: str,
    *,
    timeout_ms: int = 90_000,
    poll_ms: int = 2_000,
    session: Optional[requests.Session] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Poll until confirmed/failed or timeout → unknown (transient stays unknown)."""
    start = time.monotonic()
    last_error: Optional[str] = None
    timeout_s = timeout_ms / 1000.0
    poll_s = poll_ms / 1000.0
    while time.monotonic() - start < timeout_s:
        try:
            st = _rpc_get_signature_statuses(
                rpc_url, signature, session=session, timeout_s=min(20.0, timeout_s)
            )
        except RuntimeError as exc:
            last_error = str(exc)
            sleep_fn(poll_s)
            continue
        if st is not None:
            if st.get("err"):
                return {
                    "outcome": "failed",
                    "slot": st.get("slot"),
                    "error": json.dumps(st.get("err")),
                }
            conf = st.get("confirmationStatus")
            if conf in ("confirmed", "finalized"):
                return {
                    "outcome": "confirmed",
                    "slot": st.get("slot"),
                    "error": None,
                }
        sleep_fn(poll_s)
    return {"outcome": "unknown", "slot": None, "error": last_error}


def _apply_send_statuses(payload: Dict[str, Any]) -> None:
    """Sync journal rows from exec JSON ``sends`` (confirmed/failed/unknown/not_sent)."""
    sends = payload.get("sends")
    if not isinstance(sends, list):
        return
    for s in sends:
        if not isinstance(s, dict):
            continue
        sig = str(s.get("signature") or "")
        status = str(s.get("status") or "")
        if not sig:
            continue
        # Handshake refuse: we know sendRawTransaction was not called.
        if status == "not_sent" or (
            status == "failed" and s.get("error") == "journal-refused"
        ):
            try:
                exec_journal_io.update_by_signature(
                    sig,
                    {
                        "status": "failed",
                        "slot": None,
                        "error": "journal-refused",
                    },
                )
            except KeyError:
                pass
            continue
        if status not in ("confirmed", "failed", "unknown"):
            continue
        patch: Dict[str, Any] = {
            "status": status,
            "slot": s.get("slot"),
            "error": s.get("error"),
        }
        try:
            exec_journal_io.update_by_signature(sig, patch)
        except KeyError:
            # Marker may have been missed; still record outcome.
            exec_journal_io.append_entry(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "action": str(payload.get("action") or "exec"),
                    "network": str(payload.get("network") or "devnet"),
                    "params": {},
                    "signature": sig,
                    **patch,
                }
            )


def _handle_journal_stderr_line(
    line: str,
    *,
    network: str,
    reply: Optional[Callable[[str], None]] = None,
) -> None:
    """Process one stderr line; on ``@@JOURNAL`` write pending and ack via stdin."""
    if not line.startswith("@@JOURNAL "):
        if line.strip() and not meteora_ops.is_bigint_noise_line(line):
            print(line, file=sys.stderr)
        return
    raw = line[len("@@JOURNAL ") :].strip()
    try:
        marker = json.loads(raw)
    except json.JSONDecodeError:
        print(line, file=sys.stderr)
        return
    if not isinstance(marker, dict):
        print(line, file=sys.stderr)
        return
    if marker.get("stage") != "sending":
        print(line, file=sys.stderr)
        return
    sig = str(marker.get("signature") or "")
    try:
        exec_journal_io.append_pending_from_marker(marker, network=network)
    except Exception as exc:  # noqa: BLE001 — any write failure refuses send
        print(f"journal marker write failed: {exc}", file=sys.stderr)
        if sig and reply is not None:
            reply(f"@@JOURNAL-FAIL {sig}\n")
        return
    if sig and reply is not None:
        reply(f"@@JOURNAL-OK {sig}\n")


def resolve_unresolved_signatures(
    *,
    rpc_url: str,
    timeout_ms: int = RESOLVE_PER_SIG_TIMEOUT_MS,
    poll_ms: int = RESOLVE_POLL_MS,
    wall_ms: int = RESOLVE_WALL_MS,
    network: str = "devnet",
    session: Optional[requests.Session] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Re-poll pending/unknown journal rows; update confirmed/failed in place."""
    before = exec_journal_io.list_unresolved()
    wall_start = time.monotonic()
    rpc_down_hint: Optional[str] = None
    for e in before:
        remaining_ms = wall_ms - int((time.monotonic() - wall_start) * 1000)
        # Need at least one poll interval left (tests may use a small wall_ms).
        if remaining_ms < poll_ms:
            print(
                "resolve-journal: wall budget exhausted — leaving rest unresolved",
                file=sys.stderr,
            )
            break
        this_timeout = min(timeout_ms, remaining_ms)
        sig = str(e.get("signature") or "")
        if not sig:
            continue
        out = poll_signature_status(
            rpc_url,
            sig,
            timeout_ms=this_timeout,
            poll_ms=poll_ms,
            session=session,
            sleep_fn=sleep_fn,
        )
        outcome = out["outcome"]
        err = out.get("error")
        if outcome == "unknown" and err and is_transient_rpc_error(str(err)):
            rpc_down_hint = (
                "RPC unreachable or flaky — retry /status journal when the node is up, "
                "or check explorer and /status journal-forget <sig> confirm if the tx is final."
            )
        if outcome == "confirmed":
            exec_journal_io.update_by_signature(
                sig, {"status": "confirmed", "slot": out.get("slot"), "error": None}
            )
        elif outcome == "failed":
            exec_journal_io.update_by_signature(
                sig,
                {
                    "status": "failed",
                    "slot": out.get("slot"),
                    "error": out.get("error"),
                },
            )
    after = exec_journal_io.list_unresolved()
    return {
        "ok": True,
        "action": "resolve-journal",
        "network": network,
        "journalPath": str(exec_journal_io.journal_path()),
        "before": [
            {
                "signature": x.get("signature"),
                "status": x.get("status"),
                "action": x.get("action"),
            }
            for x in before
        ],
        "stillUnresolved": [
            {
                "signature": x.get("signature"),
                "status": x.get("status"),
                "action": x.get("action"),
                "explorer": explorer_tx_url(str(x.get("signature") or ""), network),
            }
            for x in after
        ],
        "cleared": len(before) - len(after),
        "rpcHint": rpc_down_hint,
    }


def _gate_journal_before_send(
    *,
    network: str,
    rpc: Optional[str],
    force_ignore_journal: bool,
) -> None:
    rpc_url = _default_rpc(network, rpc)
    print(
        f"resolve-journal(startup) rpc={rpc_host_for_log(rpc_url)} "
        f"journal={exec_journal_io.journal_path()}",
        file=sys.stderr,
    )
    resolve_unresolved_signatures(
        rpc_url=rpc_url,
        timeout_ms=STARTUP_RESOLVE_TIMEOUT_MS,
        poll_ms=STARTUP_RESOLVE_POLL_MS,
        wall_ms=RESOLVE_WALL_MS,
        network=network,
    )
    if force_ignore_journal:
        print(
            "warning: --force-ignore-journal — skipping journal block check",
            file=sys.stderr,
        )
        return
    open_rows = exec_journal_io.list_unresolved()
    if open_rows:
        sigs = ", ".join(
            f"{e.get('signature')}({e.get('status')})" for e in open_rows
        )
        raise MeteoraExecError(
            {
                "ok": False,
                "action": "journal",
                "error": (
                    f"journal has unresolved entries: {sigs}. "
                    "Resolve manually or use --force-ignore-journal"
                ),
                "stage": "journal",
            }
        )


def run_exec(
    args: List[str],
    *,
    rpc: Optional[str] = None,
    pool: Optional[str] = None,
    network: str = "devnet",
    send: bool = False,
    timeout_s: float = 300.0,
    extra_env: Optional[Dict[str, str]] = None,
    force_ignore_journal: bool = False,
    executable: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run exec.js (or ``executable``) and return parsed JSON from stdout.

    When ``send`` is true, stderr is streamed line-by-line so ``@@JOURNAL``
    markers append ``pending`` before the child finishes sending.
    """
    if send:
        _gate_journal_before_send(
            network=network,
            rpc=rpc,
            force_ignore_journal=force_ignore_journal,
        )

    if executable is None:
        _ensure_exec_built()
        cmd = ["node", str(EXEC_JS), *args, "--network", network]
    else:
        cmd = [*executable, *args, "--network", network]
    if send:
        cmd.append("--send")
    if rpc:
        cmd.extend(["--rpc", rpc])
    elif network == "devnet" and "SOLANA_DEVNET_RPC_URL" not in os.environ:
        cmd.extend(["--rpc", "https://api.devnet.solana.com"])
    elif network == "mainnet" and "SOLANA_RPC_URL" not in os.environ:
        cmd.extend(["--rpc", config.solana_rpc_url()])
    if pool:
        cmd.extend(["--pool", pool])
    # Strip legacy flag if a caller still passes it to Node (ignored there).
    cmd = [c for c in cmd if c != "--force-ignore-journal"]
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    env.setdefault(
        "PRIORITY_FEE_MICROLAMPORTS",
        os.getenv("PRIORITY_FEE_MICROLAMPORTS", "50000"),
    )

    stderr_lines: List[str] = []
    stdout_chunks: List[str] = []
    stdin_lock = threading.Lock()

    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        bufsize=1,
    )
    assert (
        proc.stderr is not None
        and proc.stdout is not None
        and proc.stdin is not None
    )

    def _stdin_reply(text: str) -> None:
        with stdin_lock:
            if proc.stdin is None or proc.stdin.closed:
                return
            try:
                proc.stdin.write(text)
                proc.stdin.flush()
            except OSError as exc:
                print(f"journal handshake reply failed: {exc}", file=sys.stderr)

    def _read_stderr(pipe: Any) -> None:
        assert pipe is not None
        for line in iter(pipe.readline, ""):
            stderr_lines.append(line.rstrip("\n"))
            _handle_journal_stderr_line(
                line.rstrip("\n"), network=network, reply=_stdin_reply
            )
        pipe.close()

    def _read_stdout(pipe: Any) -> None:
        assert pipe is not None
        try:
            stdout_chunks.append(pipe.read())
        finally:
            pipe.close()

    # Do NOT use communicate() — it would also drain stderr and race the
    # journal reader (pending must be written as soon as @@JOURNAL appears).
    t_err = threading.Thread(target=_read_stderr, args=(proc.stderr,), daemon=True)
    t_out = threading.Thread(target=_read_stdout, args=(proc.stdout,), daemon=True)
    t_err.start()
    t_out.start()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
        t_err.join(timeout=2.0)
        t_out.join(timeout=2.0)
        raise
    finally:
        with stdin_lock:
            if proc.stdin is not None and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
    t_err.join(timeout=5.0)
    t_out.join(timeout=5.0)
    stderr = "\n".join(stderr_lines).strip()
    stdout = "".join(stdout_chunks).strip()
    if not stdout:
        raise RuntimeError(
            f"exec produced empty stdout (exit={proc.returncode}). stderr={stderr[:500]}"
        )
    line = stdout.splitlines()[-1]
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"exec stdout is not JSON: {line[:300]!r} (exit={proc.returncode})"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"exec JSON root must be object, got {type(payload)}")
    _apply_send_statuses(payload)
    if not payload.get("ok", False):
        raise MeteoraExecError(payload)
    if proc.returncode != 0:
        raise RuntimeError(
            f"exec exit={proc.returncode} but ok:true payload={payload.get('action')}"
        )
    return payload


def exec_open(
    owner: str,
    sol: float,
    usdc: float,
    *,
    send: bool = False,
    min_bin_id: Optional[int] = None,
    max_bin_id: Optional[int] = None,
    strategy: str = "Spot",
    slippage_bps: Optional[int] = None,
    priority_fee: Optional[int] = None,
    force_ignore_journal: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = [
        "exec-open",
        "--owner",
        owner,
        "--sol",
        str(sol),
        "--usdc",
        str(usdc),
        "--strategy",
        strategy,
    ]
    if min_bin_id is not None:
        args.extend(["--min-bin-id", str(min_bin_id)])
    if max_bin_id is not None:
        args.extend(["--max-bin-id", str(max_bin_id)])
    if slippage_bps is not None:
        args.extend(["--slippage-bps", str(slippage_bps)])
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    return run_exec(
        args, send=send, force_ignore_journal=force_ignore_journal, **kwargs
    )


def exec_add(
    owner: str,
    position: str,
    sol: float,
    usdc: float,
    *,
    send: bool = False,
    strategy: str = "Spot",
    slippage_bps: Optional[int] = None,
    priority_fee: Optional[int] = None,
    allow_multi_tx: bool = False,
    force_ignore_journal: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = [
        "exec-add",
        "--owner",
        owner,
        "--position",
        position,
        "--sol",
        str(sol),
        "--usdc",
        str(usdc),
        "--strategy",
        strategy,
    ]
    if slippage_bps is not None:
        args.extend(["--slippage-bps", str(slippage_bps)])
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    if allow_multi_tx:
        args.append("--allow-multi-tx")
    return run_exec(
        args, send=send, force_ignore_journal=force_ignore_journal, **kwargs
    )


def exec_claim_fees(
    owner: str,
    position: str,
    *,
    send: bool = False,
    priority_fee: Optional[int] = None,
    force_ignore_journal: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = ["exec-claim-fees", "--owner", owner, "--position", position]
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    return run_exec(
        args, send=send, force_ignore_journal=force_ignore_journal, **kwargs
    )


def exec_withdraw(
    owner: str,
    position: str,
    bps: int,
    *,
    send: bool = False,
    priority_fee: Optional[int] = None,
    force_ignore_journal: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = [
        "exec-withdraw",
        "--owner",
        owner,
        "--position",
        position,
        "--bps",
        str(bps),
    ]
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    return run_exec(
        args, send=send, force_ignore_journal=force_ignore_journal, **kwargs
    )


def exec_close(
    owner: str,
    position: str,
    *,
    send: bool = False,
    priority_fee: Optional[int] = None,
    force_ignore_journal: bool = False,
    crash_after_send: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = ["exec-close", "--owner", owner, "--position", position]
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    if crash_after_send:
        args.append("--crash-after-send")
    return run_exec(
        args, send=send, force_ignore_journal=force_ignore_journal, **kwargs
    )


def exec_close_empty(
    owner: str,
    position: str,
    *,
    send: bool = False,
    priority_fee: Optional[int] = None,
    force_ignore_journal: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Close zero-liquidity position via SDK closePositionIfEmpty."""
    args = ["exec-close-empty", "--owner", owner, "--position", position]
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    return run_exec(
        args, send=send, force_ignore_journal=force_ignore_journal, **kwargs
    )


def exec_swap(
    owner: str,
    side: str,
    amount: float,
    *,
    send: bool = False,
    slippage_bps: Optional[int] = None,
    priority_fee: Optional[int] = None,
    force_ignore_journal: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    args = [
        "exec-swap",
        "--owner",
        owner,
        "--side",
        side,
        "--amount",
        str(amount),
    ]
    if slippage_bps is not None:
        args.extend(["--slippage-bps", str(slippage_bps)])
    if priority_fee is not None:
        args.extend(["--priority-fee", str(priority_fee)])
    return run_exec(
        args, send=send, force_ignore_journal=force_ignore_journal, **kwargs
    )


def resolve_journal(
    *,
    timeout_ms: int = RESOLVE_PER_SIG_TIMEOUT_MS,
    network: str = "devnet",
    rpc: Optional[str] = None,
    pool: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
    timeout_s: float = 180.0,
) -> Dict[str, Any]:
    """Re-poll unresolved journal entries (no send). Chat-reachable unlock step 1.

    Per-signature poll budget defaults to 25_000 ms; wall budget RESOLVE_WALL_MS
    (170_000) so several stuck signatures still fit within timeout_s (180.0).
    Transient RPC errors leave rows as ``unknown`` (not ``failed``).
    """
    del pool, extra_env  # API compat; resolve no longer shells to Node.
    rpc_url = _default_rpc(network, rpc)
    print(
        f"resolve-journal rpc={rpc_host_for_log(rpc_url)} "
        f"journal={exec_journal_io.journal_path()}",
        file=sys.stderr,
    )
    # Bound wall by caller timeout_s as well.
    wall_ms = min(RESOLVE_WALL_MS, int(timeout_s * 1000))
    return resolve_unresolved_signatures(
        rpc_url=rpc_url,
        timeout_ms=int(timeout_ms),
        poll_ms=RESOLVE_POLL_MS,
        wall_ms=wall_ms,
        network=network,
    )


def forget_unresolved_journal(
    *,
    signature: str,
    rpc: Optional[str] = None,
    network: str = "devnet",
    confirm_window_sec: float = 90.0,
    reason: str = "owner cleared via Telegram",
) -> Dict[str, Any]:
    """Mark one unresolved journal signature as failed after an RPC poll.

    Mass forget is intentionally removed (C9). Does NOT send transactions.
    Writes under a file lock via temp+replace (C10).
    """
    import urllib.error
    import urllib.request
    from datetime import datetime, timezone

    import exec_journal_io

    sig = (signature or "").strip()
    if not sig:
        return {
            "ok": False,
            "error": "signature required",
            "cleared": 0,
        }

    if not rpc:
        if network == "devnet":
            rpc = os.environ.get("SOLANA_DEVNET_RPC_URL", "https://api.devnet.solana.com")
        else:
            rpc = os.environ.get("SOLANA_RPC_URL", config.solana_rpc_url())

    journal_path = exec_journal_io.journal_path()
    if not journal_path.is_file():
        return {"ok": False, "error": "journal empty", "cleared": 0}

    def _read_entries() -> List[Dict[str, Any]]:
        return exec_journal_io.read_entries(journal_path)

    def _atomic_write(entries: List[Dict[str, Any]]) -> None:
        exec_journal_io.write_entries_holding_lock(entries, journal_path)

    # Hold the lock across the RPC poll so a concurrent Python writer cannot
    # land a pending row that this rewrite would clobber (C12/C13).
    exec_journal_io.acquire()
    try:
        entries = _read_entries()

        target: Optional[Dict[str, Any]] = None
        target_idx = -1
        for i in range(len(entries) - 1, -1, -1):
            if str(entries[i].get("signature")) == sig:
                target = entries[i]
                target_idx = i
                break
        if target is None:
            return {
                "ok": False,
                "error": f"signature not in journal: {sig}",
                "cleared": 0,
            }
        if target.get("status") not in ("pending", "unknown"):
            return {
                "ok": False,
                "error": f"signature status is {target.get('status')!r}, not unresolved",
                "cleared": 0,
                "status": target.get("status"),
            }

        # Poll RPC before trusting the owner.
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignatureStatuses",
                "params": [[sig], {"searchTransactionHistory": True}],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            rpc,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            return {
                "ok": False,
                "error": f"RPC unavailable: {exc}",
                "cleared": 0,
                "refuse": "rpc",
            }

        if raw.get("error"):
            return {
                "ok": False,
                "error": f"RPC error: {raw['error']}",
                "cleared": 0,
                "refuse": "rpc",
            }

        st = (raw.get("result") or {}).get("value") or [None]
        st0 = st[0] if st else None
        if st0 is not None:
            if st0.get("err"):
                entries[target_idx] = {
                    **target,
                    "status": "failed",
                    "error": f"{reason}; on-chain err={json.dumps(st0.get('err'))}",
                }
                _atomic_write(entries)
                return {
                    "ok": True,
                    "cleared": 1,
                    "signature": sig,
                    "outcome": "failed_on_chain",
                }
            conf = st0.get("confirmationStatus")
            if conf in ("confirmed", "finalized"):
                return {
                    "ok": False,
                    "error": "signature is confirmed on-chain — resolve, do not forget",
                    "cleared": 0,
                    "refuse": "confirmed",
                    "signature": sig,
                    "confirmationStatus": conf,
                    "slot": st0.get("slot"),
                }
            return {
                "ok": False,
                "error": f"signature still in flight ({conf or 'unknown status'}) — retry later",
                "cleared": 0,
                "refuse": "processing",
                "signature": sig,
            }

        age_sec = None
        ts = target.get("ts")
        if ts:
            try:
                s = str(ts)
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                sent_at = datetime.fromisoformat(s)
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)
                age_sec = (
                    datetime.now(timezone.utc) - sent_at.astimezone(timezone.utc)
                ).total_seconds()
            except ValueError:
                age_sec = None
        if age_sec is None or age_sec < confirm_window_sec:
            return {
                "ok": False,
                "error": (
                    "signature not found yet and confirm window not elapsed — retry later"
                ),
                "cleared": 0,
                "refuse": "processing",
                "signature": sig,
                "age_sec": age_sec,
                "confirm_window_sec": confirm_window_sec,
            }

        entries[target_idx] = {
            **target,
            "status": "failed",
            "error": f"{reason}; not found after {age_sec:.0f}s",
        }
        _atomic_write(entries)
        return {
            "ok": True,
            "cleared": 1,
            "signature": sig,
            "outcome": "not_found_expired",
            "age_sec": age_sec,
        }
    finally:
        exec_journal_io.release()


def main() -> None:
    p = argparse.ArgumentParser(description="Python bridge to ts/dist/exec.js")
    p.add_argument(
        "command",
        choices=[
            "exec-open",
            "exec-add",
            "exec-claim-fees",
            "exec-withdraw",
            "exec-close",
            "exec-close-empty",
            "exec-swap",
        ],
    )
    p.add_argument("--owner")
    p.add_argument("--position")
    p.add_argument("--sol", type=float)
    p.add_argument("--usdc", type=float)
    p.add_argument("--bps", type=int)
    p.add_argument("--min-bin-id", type=int)
    p.add_argument("--max-bin-id", type=int)
    p.add_argument("--strategy", default="Spot")
    p.add_argument("--slippage-bps", type=int, default=None)
    p.add_argument("--priority-fee", type=int, default=None)
    p.add_argument("--allow-multi-tx", action="store_true")
    p.add_argument("--side", choices=["sol-to-usdc", "usdc-to-sol"])
    p.add_argument("--amount", type=float)
    p.add_argument("--network", default="devnet")
    p.add_argument("--rpc", default=None)
    p.add_argument("--pool", default=None)
    p.add_argument("--send", action="store_true")
    p.add_argument("--force-ignore-journal", action="store_true")
    p.add_argument("--raw", action="store_true")
    args = p.parse_args()

    kwargs = {
        "rpc": args.rpc,
        "pool": args.pool,
        "network": args.network,
        "send": args.send,
    }
    try:
        if args.command == "exec-open":
            if not args.owner or args.sol is None or args.usdc is None:
                p.error("--owner --sol --usdc required")
            out = exec_open(
                args.owner,
                args.sol,
                args.usdc,
                send=args.send,
                min_bin_id=args.min_bin_id,
                max_bin_id=args.max_bin_id,
                strategy=args.strategy,
                slippage_bps=args.slippage_bps,
                priority_fee=args.priority_fee,
                force_ignore_journal=args.force_ignore_journal,
                **kwargs,
            )
        elif args.command == "exec-add":
            if not args.owner or not args.position or args.sol is None or args.usdc is None:
                p.error("--owner --position --sol --usdc required")
            out = exec_add(
                args.owner,
                args.position,
                args.sol,
                args.usdc,
                send=args.send,
                strategy=args.strategy,
                slippage_bps=args.slippage_bps,
                priority_fee=args.priority_fee,
                allow_multi_tx=args.allow_multi_tx,
                force_ignore_journal=args.force_ignore_journal,
                **kwargs,
            )
        elif args.command == "exec-claim-fees":
            if not args.owner or not args.position:
                p.error("--owner --position required")
            out = exec_claim_fees(
                args.owner,
                args.position,
                send=args.send,
                priority_fee=args.priority_fee,
                force_ignore_journal=args.force_ignore_journal,
                **kwargs,
            )
        elif args.command == "exec-withdraw":
            if not args.owner or not args.position or args.bps is None:
                p.error("--owner --position --bps required")
            out = exec_withdraw(
                args.owner,
                args.position,
                args.bps,
                send=args.send,
                priority_fee=args.priority_fee,
                force_ignore_journal=args.force_ignore_journal,
                **kwargs,
            )
        elif args.command == "exec-close":
            if not args.owner or not args.position:
                p.error("--owner --position required")
            out = exec_close(
                args.owner,
                args.position,
                send=args.send,
                priority_fee=args.priority_fee,
                force_ignore_journal=args.force_ignore_journal,
                **kwargs,
            )
        elif args.command == "exec-close-empty":
            if not args.owner or not args.position:
                p.error("--owner --position required")
            out = exec_close_empty(
                args.owner,
                args.position,
                send=args.send,
                priority_fee=args.priority_fee,
                force_ignore_journal=args.force_ignore_journal,
                **kwargs,
            )
        elif args.command == "exec-swap":
            if not args.owner or not args.side or args.amount is None:
                p.error("--owner --side --amount required")
            out = exec_swap(
                args.owner,
                args.side,
                args.amount,
                send=args.send,
                slippage_bps=args.slippage_bps,
                priority_fee=args.priority_fee,
                force_ignore_journal=args.force_ignore_journal,
                **kwargs,
            )
        else:
            p.error("unknown command")
    except MeteoraExecError as exc:
        print(json.dumps(exc.payload, ensure_ascii=False, indent=2))
        sys.exit(1)

    if args.raw:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
