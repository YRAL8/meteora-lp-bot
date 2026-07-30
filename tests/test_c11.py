#!/usr/bin/env python3
"""C11: mountinfo volume classification (no Docker required)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import state_volume as sv  # noqa: E402


def _line(root: str, mount_point: str = "/app/state") -> str:
    # Minimal mountinfo line: id parent maj:min root mountpoint rest
    return f"123 1 0:0 {root} {mount_point} rw shared:1 - ext4 /dev/sda1 rw"


class MountinfoClassifyTests(unittest.TestCase):
    def test_no_mount(self) -> None:
        root = sv.parse_mountinfo_root("456 1 0:0 / / rw -", mount_point="/app/state")
        self.assertIsNone(root)
        ok, kind, detail = sv.classify_mount_root(None)
        self.assertFalse(ok)
        self.assertEqual(kind, "layer")
        self.assertIn("ALARM", detail)

    def test_anonymous_volume(self) -> None:
        anon = "a" * 64
        root = f"/var/lib/docker/volumes/{anon}/_data"
        text = _line(root)
        self.assertEqual(sv.parse_mountinfo_root(text), root)
        ok, kind, detail = sv.classify_mount_root(root)
        self.assertFalse(ok)
        self.assertEqual(kind, "anonymous")
        self.assertIn("ALARM", detail)

    def test_named_volume(self) -> None:
        root = "/var/lib/docker/volumes/c10test/_data"
        ok, kind, detail = sv.classify_mount_root(root)
        self.assertTrue(ok)
        self.assertEqual(kind, "named")
        self.assertIn("c10test", detail)

    def test_host_bind(self) -> None:
        root = "/home/someone/projects/meteora-lp-bot/state"
        ok, kind, detail = sv.classify_mount_root(root)
        self.assertTrue(ok)
        self.assertEqual(kind, "bind")
        self.assertIn(root, detail)

    def test_host_run_skips_mount_check(self) -> None:
        ok, line = sv.ensure_state_dir(force_container=False)
        self.assertTrue(ok)
        self.assertIn("host run", line)

    def test_container_anonymous_via_ensure(self) -> None:
        anon = "b" * 64
        text = _line(f"/var/lib/docker/volumes/{anon}/_data")
        ok, line = sv.ensure_state_dir(
            force_container=True, mountinfo_text=text, mount_point="/app/state"
        )
        self.assertFalse(ok)
        self.assertIn("ALARM", line)


if __name__ == "__main__":
    unittest.main()
