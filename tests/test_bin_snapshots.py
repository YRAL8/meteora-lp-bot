"""Hourly bin snapshots: the shape math, the address cache, and staying alive."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bin_snapshots as bs  # noqa: E402
import meteora_position as mp  # noqa: E402

POOL = "PoolAddr111"


def _uniform(active: int, span: int, value: float) -> dict[int, float]:
    return {b: value for b in range(active - span, active + span + 1)}


class ConcentrationMathTests(unittest.TestCase):
    def test_matches_the_full_range_formula(self) -> None:
        # A full-range pool holds V/4 per unit of log-price; one bin spans h.
        m = bs.concentration(1_000.0, bin_step=4, tvl=1_000_000.0)
        self.assertAlmostEqual(m, 4 * 1_000.0 / (0.0004 * 1_000_000.0), places=9)
        self.assertAlmostEqual(m, 10.0, places=9)

    def test_wider_bins_mean_less_concentration(self) -> None:
        narrow = bs.concentration(1_000.0, bin_step=4, tvl=1_000_000.0)
        wide = bs.concentration(1_000.0, bin_step=10, tvl=1_000_000.0)
        self.assertLess(wide, narrow)

    def test_refuses_nonsense_instead_of_dividing_by_zero(self) -> None:
        self.assertIsNone(bs.concentration(1_000.0, bin_step=4, tvl=0.0))
        self.assertIsNone(bs.concentration(1_000.0, bin_step=0, tvl=1_000_000.0))


class SummarizeTests(unittest.TestCase):
    def test_band_widths_follow_the_bin_step(self) -> None:
        out = bs.summarize(_uniform(0, 300, 1_000.0), active_id=0, bin_step=4, tvl=1e6)
        # ±1% at 0.04% per bin is 25 bins each side, plus the active one.
        self.assertEqual(out["bands"]["1"]["bins"], 51)
        self.assertEqual(out["bands"]["2"]["bins"], 101)
        self.assertEqual(out["bands"]["5"]["bins"], 251)

    def test_band_value_and_share(self) -> None:
        out = bs.summarize(_uniform(0, 300, 1_000.0), active_id=0, bin_step=4, tvl=1e6)
        self.assertAlmostEqual(out["bands"]["1"]["value_usd"], 51_000.0, places=2)
        self.assertAlmostEqual(out["bands"]["1"]["share_of_tvl"], 0.051, places=6)

    def test_one_fat_bin_does_not_move_the_price_point(self) -> None:
        bins = _uniform(0, 300, 1_000.0)
        bins[0] = 900_000.0  # a whale parked exactly at the price
        out = bs.summarize(bins, active_id=0, bin_step=4, tvl=1e6)
        # median over the neighbourhood, so the spike is visible in the bands
        # but does not masquerade as the whole pool's shape
        self.assertAlmostEqual(out["value_per_bin_usd"], 1_000.0, places=2)
        self.assertGreater(out["bands"]["0.5"]["value_usd"], 900_000.0)

    def test_empty_bins_count_as_zero_not_missing(self) -> None:
        bins = {b: 1_000.0 for b in range(-10, 11)}  # only ±0.4% is populated
        out = bs.summarize(bins, active_id=0, bin_step=4, tvl=1e6)
        self.assertEqual(out["bands"]["2"]["bins"], 101)
        self.assertEqual(out["bands"]["2"]["occupied"], 21)
        self.assertAlmostEqual(out["bands"]["2"]["value_usd"], 21_000.0, places=2)

    def test_buckets_keep_the_whole_span(self) -> None:
        bins = _uniform(0, bs.SPAN_BINS, 1_000.0)
        out = bs.summarize(bins, active_id=0, bin_step=4, tvl=1e6)
        b = out["buckets"]
        self.assertEqual(b["from_bin"], -bs.SPAN_BINS)
        self.assertAlmostEqual(sum(b["values"]), out["value_in_span_usd"], places=2)


def _binarray_bytes(index: int, rows: dict[int, tuple[int, int]]) -> bytes:
    """One BinArray account: {bin_id: (amount_x, amount_y)}."""
    buf = bytearray(56 + 70 * 144)
    buf[0:8] = mp.anchor_account_discriminator("BinArray")
    buf[8:16] = index.to_bytes(8, "little", signed=True)
    buf[24:56] = bytes(range(32))  # lb_pair, not checked here
    for bin_id, (ax, ay) in rows.items():
        i = bin_id - index * mp.BINS_PER_BIN_ARRAY
        off = 56 + i * 144
        buf[off : off + 8] = int(ax).to_bytes(8, "little")
        buf[off + 8 : off + 16] = int(ay).to_bytes(8, "little")
        buf[off + 32 : off + 48] = (1).to_bytes(16, "little")  # liquidity_supply
    return bytes(buf)


class ReadBinsTests(unittest.TestCase):
    def _read(self, td: str, calls: list[str]):
        index = 0
        account = _binarray_bytes(index, {0: (10**9, 0), 1: (0, 250 * 10**6)})

        def fake_gpa(_s, _rpc, _pool, *, want_indexes, sleep_s):
            calls.append("gpa")
            return {i: f"pk{i}" for i in want_indexes}

        def fake_gma(_s, _rpc, pubkeys, *, sleep_s):
            calls.append("gma")
            return {pk: (account if pk == "pk0" else None) for pk in pubkeys}

        with patch.dict(os.environ, {"METEORA_STATE_DIR": td}), patch.object(
            mp, "fetch_binarrays_for_lb_pair_indexes", fake_gpa
        ), patch.object(mp, "solana_get_multiple_accounts", fake_gma):
            return bs.read_bins(
                None,
                pool=POOL,
                rpc_url="rpc",
                active_id=0,
                bin_step=4,
                price=100.0,
                dec_x=9,
                dec_y=6,
                sleep_s=0.0,
            )

    def test_amounts_become_usd_with_the_right_decimals(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bins = self._read(td, [])
        self.assertAlmostEqual(bins[0], 100.0, places=6)  # 1 SOL at $100
        self.assertAlmostEqual(bins[1], 250.0, places=6)  # 250 USDC

    def test_addresses_are_cached_so_getprogramaccounts_runs_once(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as td:
            self._read(td, calls)
            self.assertEqual(calls.count("gpa"), 1)
            self._read(td, calls)
            self.assertEqual(calls.count("gpa"), 1, "second run must reuse the cache")
            self.assertEqual(calls.count("gma"), 2)
            cached = json.loads((Path(td) / bs.PUBKEY_CACHE).read_text(encoding="utf-8"))
        self.assertEqual(cached["pool"], POOL)

    def test_a_cache_from_another_pool_is_ignored(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as td:
            (Path(td)).mkdir(parents=True, exist_ok=True)
            (Path(td) / bs.PUBKEY_CACHE).write_text(
                json.dumps({"pool": "SomeOtherPool", "indexes": {"0": "stale"}}),
                encoding="utf-8",
            )
            self._read(td, calls)
        self.assertEqual(calls.count("gpa"), 1)


POOL_META = {
    "current_price": 100.0,
    "tvl": 1_000_000.0,
    "pool_config": {"bin_step": 4},
    "token_x": {"decimals": 9},
    "token_y": {"decimals": 6},
}


class SnapshotTargetTests(unittest.TestCase):
    """Research may watch mainnet while the money side is pinned to devnet."""

    def test_defaults_to_the_pool_the_bot_trades(self) -> None:
        import bot_config

        with patch.dict(os.environ, {"DLMM_LB_PAIR": "TradedPool", "SNAPSHOT_LB_PAIR": ""}):
            self.assertEqual(bot_config.snapshot_pool(), "TradedPool")

    def test_override_wins_for_pool_and_rpc(self) -> None:
        import bot_config

        with patch.dict(
            os.environ,
            {
                "DLMM_LB_PAIR": "TradedPool",
                "SNAPSHOT_LB_PAIR": "WatchedPool",
                "SNAPSHOT_RPC_URL": "https://watched.example",
            },
        ):
            self.assertEqual(bot_config.snapshot_pool(), "WatchedPool")
            self.assertEqual(bot_config.snapshot_rpc(), "https://watched.example")

    def test_blank_override_is_not_an_address(self) -> None:
        import bot_config

        with patch.dict(os.environ, {"DLMM_LB_PAIR": "TradedPool", "SNAPSHOT_LB_PAIR": "   "}):
            self.assertEqual(bot_config.snapshot_pool(), "TradedPool")


class TakeSnapshotTests(unittest.TestCase):
    def test_writes_one_row_with_the_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"METEORA_STATE_DIR": td}), patch.object(
                mp, "solana_get_account_data", lambda *a, **k: b""
            ), patch.object(
                mp, "decode_lbpair_state", lambda _d: mp.LbPairState(True, 0, 4)
            ):
                row = bs.take_snapshot(
                    pool=POOL,
                    fetch_pool=lambda _a: POOL_META,
                    read_bins_fn=lambda *a, **k: _uniform(0, bs.SPAN_BINS, 1_000.0),
                )
                text = (Path(td) / bs.FILENAME).read_text(encoding="utf-8")
        self.assertNotIn("error", row)
        self.assertEqual(row["active_bin"], 0)
        self.assertEqual(row["bin_step"], 4)
        self.assertAlmostEqual(row["m_at_price"], 10.0, places=3)
        self.assertEqual(len([ln for ln in text.splitlines() if ln.strip()]), 1)
        self.assertEqual(json.loads(text)["pool"], POOL)

    def test_a_dead_api_writes_an_error_row_and_never_raises(self) -> None:
        def boom(_addr: str) -> dict:
            raise TimeoutError("api down")

        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"METEORA_STATE_DIR": td}):
                row = bs.take_snapshot(pool=POOL, fetch_pool=boom)
                text = (Path(td) / bs.FILENAME).read_text(encoding="utf-8")
        self.assertIn("TimeoutError", row["error"])
        self.assertEqual(len([ln for ln in text.splitlines() if ln.strip()]), 1)

    def test_a_dead_rpc_writes_an_error_row_and_never_raises(self) -> None:
        def boom(*_a, **_k):
            raise ConnectionError("rpc down")

        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"METEORA_STATE_DIR": td}), patch.object(
                mp, "solana_get_account_data", boom
            ):
                row = bs.take_snapshot(pool=POOL, fetch_pool=lambda _a: POOL_META)
        self.assertIn("ConnectionError", row["error"])


if __name__ == "__main__":
    unittest.main()
