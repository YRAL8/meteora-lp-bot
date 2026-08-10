#!/usr/bin/env python3
"""LITE_3: docker-compose declares the named state volume (no PyYAML required)."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

COMPOSE = ROOT / "docker-compose.yml"


class DockerComposeStateVolumeTests(unittest.TestCase):
    def test_named_volume_mounted_on_app_state(self) -> None:
        self.assertTrue(COMPOSE.is_file(), "docker-compose.yml must exist at repo root")
        text = COMPOSE.read_text(encoding="utf-8")
        # Service mount: named volume → /app/state
        self.assertRegex(
            text,
            re.compile(
                r"^\s*-\s*meteora-lp-bot-state\s*:\s*/app/state\s*$",
                re.MULTILINE,
            ),
            "service must mount meteora-lp-bot-state on /app/state",
        )
        # Top-level volumes: declaration of the named volume
        self.assertRegex(
            text,
            re.compile(
                r"(?ms)^volumes:\s*\n(?:[ \t].*\n)*[ \t]+meteora-lp-bot-state\s*:",
            ),
            "top-level volumes: must declare meteora-lp-bot-state",
        )
        self.assertIn("env_file: .env", text)
        self.assertIn("WALLET_HOST_PATH", text)
        self.assertNotIn("VOLUME /app/state", text)


if __name__ == "__main__":
    unittest.main()
