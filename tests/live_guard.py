"""Shared bootstrap for live/devnet scripts under tests/.

Call `begin_live_script()` immediately after importing anything from the
`tests` package — that package forces an offline wallet fixture for unit
tests, which must not silently drive money scripts.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Default live path via expanduser — no hardcoded /home/<user>/… in the repo.
_DEFAULT_LIVE_WALLET = "~/.config/solana/meteora-devnet.json"


def offline_fixture_path() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "offline_keypair.json"


def begin_live_script(*, default_wallet: str = _DEFAULT_LIVE_WALLET) -> str:
    """Restore the owner wallet after `tests` package import; print banner.

    Returns the resolved wallet pubkey. Exits non-zero if the wallet would be
    the offline fixture (silent wrong-wallet must not recur).
    """
    import tests as tests_pkg

    fixture = offline_fixture_path().resolve()
    saved = getattr(tests_pkg, "SAVED_WALLET_KEYPAIR_PATH", None)
    candidate = (
        os.environ.get("METEORA_LIVE_WALLET")
        or saved
        or os.environ.get("WALLET_KEYPAIR_PATH")
        or os.path.expanduser(default_wallet)
    )
    candidate_path = Path(os.path.expanduser(str(candidate))).resolve()

    if candidate_path == fixture:
        print(
            "REFUSE: live script would use offline test fixture wallet "
            f"({fixture}). Set WALLET_KEYPAIR_PATH or METEORA_LIVE_WALLET "
            "to the real keypair before import, then call begin_live_script().",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if not candidate_path.is_file():
        print(
            f"REFUSE: live wallet file missing: {candidate_path}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    os.environ["WALLET_KEYPAIR_PATH"] = str(candidate_path)

    import bot_config

    bot_config.WALLET_KEYPAIR_PATH = str(candidate_path)
    # wallet_pubkey may cache nothing; path is what owner() reads.
    try:
        pubkey = bot_config.wallet_pubkey()
    except Exception as exc:
        print(f"REFUSE: cannot load live wallet: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    # Fixture keypair is a dummy byte array — if somehow same path slipped past
    # the resolve check, refuse when path still looks like the fixture name.
    if "offline_keypair.json" in str(candidate_path):
        print(
            "REFUSE: wallet path looks like the offline fixture.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    net = bot_config.effective_network()
    print(
        f"LIVE wallet={pubkey} network={net} keypair={candidate_path}",
        flush=True,
    )
    return pubkey
