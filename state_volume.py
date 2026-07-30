"""Detect whether /app/state is a real persistent mount (not layer / anon volume)."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import state_paths

ROOT = state_paths.ROOT
# Resolved at call time via property-like helpers so METEORA_STATE_DIR works.


def _state_dir() -> Path:
    return state_paths.state_dir()


STATE_DIR = state_paths.state_dir()  # import-time default; ensure_* re-resolves
VOLUME_MARKER_NAME = ".volume_ok"

# Docker anonymous volumes: 64 hex chars under /var/lib/docker/volumes/<id>/_data
_ANON_VOLUME_ROOT = re.compile(
    r"^/var/lib/docker/volumes/([0-9a-f]{64})/_data/?$"
)
_NAMED_VOLUME_ROOT = re.compile(
    r"^/var/lib/docker/volumes/([^/]+)/_data/?$"
)

log = logging.getLogger(__name__)


def running_in_container() -> bool:
    if Path("/.dockerenv").is_file():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False
    return "docker" in cgroup or "containerd" in cgroup or "kubepods" in cgroup


def parse_mountinfo_root(
    mountinfo_text: str, *, mount_point: str = "/app/state"
) -> str | None:
    """Return the 'root' field (4th) for the mount whose mount point is ``mount_point``.

    /proc/self/mountinfo fields (space-separated):
    mount_id parent_id major:minor root mount_point ...
    """
    for line in mountinfo_text.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[4] == mount_point:
            return parts[3]
    return None


def classify_mount_root(root: str | None) -> tuple[bool, str, str]:
    """Return (ok, kind, detail) for a mountinfo root under /app/state."""
    if root is None:
        return (
            False,
            "layer",
            "ALARM: /app/state is not a mount — state lives in the container "
            "layer and will vanish on recreate. Mount -v …:/app/state",
        )
    anon = _ANON_VOLUME_ROOT.match(root)
    if anon:
        return (
            False,
            "anonymous",
            "ALARM: /app/state is an anonymous Docker volume "
            f"({anon.group(1)[:12]}…) — forgot -v; state dies on recreate",
        )
    named = _NAMED_VOLUME_ROOT.match(root)
    if named:
        return (
            True,
            "named",
            f"OK: named Docker volume {named.group(1)}",
        )
    # Bind-mount from host (or other path): root is the path inside the source.
    return True, "bind", f"OK: host/bind path root={root}"


def read_state_mount_root(
    *,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    mount_point: str = "/app/state",
) -> str | None:
    try:
        text = mountinfo_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_mountinfo_root(text, mount_point=mount_point)


def ensure_state_dir(
    *,
    force_container: bool | None = None,
    mountinfo_text: str | None = None,
    mount_point: str | None = None,
) -> tuple[bool, str]:
    """Ensure state/ exists; return (ok, human status line).

    Outside a container the mount check is skipped (host run).
    Inside: classify /app/state via mountinfo. Marker is a second signal for wipe.
    """
    state = _state_dir()
    marker = state / VOLUME_MARKER_NAME
    state.mkdir(parents=True, exist_ok=True)
    marker_existed = marker.is_file()
    if not marker_existed:
        marker.write_text(
            "meteora-lp-bot state volume marker — must survive container recreate\n",
            encoding="utf-8",
        )

    in_ctr = (
        running_in_container() if force_container is None else force_container
    )
    if not in_ctr:
        marker_note = "marker OK" if marker_existed else "marker created"
        return True, f"host run — mount check skipped ({marker_note})"

    mp = mount_point or os.environ.get("METEORA_STATE_MOUNT", "/app/state")
    if mountinfo_text is not None:
        root = parse_mountinfo_root(mountinfo_text, mount_point=mp)
    else:
        root = read_state_mount_root(mount_point=mp)

    ok, _kind, detail = classify_mount_root(root)
    if not marker_existed:
        detail = f"{detail}; marker was missing (volume may have been wiped)"
    return ok, detail
