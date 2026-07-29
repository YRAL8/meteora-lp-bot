"""Runtime range width for /open and /rebalance (Orca RANGE_WIDTH_PCT analogue)."""
from __future__ import annotations

import math
import os

# Same default as ts/src/build_lib.ts DEFAULT_RANGE_HALF.
DEFAULT_RANGE_HALF = int(os.getenv("DEFAULT_RANGE_HALF", "34"))

# User-facing percent. Orca uses 1..50; DLMM allows down to 0.05 because
# fine binSteps make sub-1% ranges the normal operating mode.
MIN_RANGE_PCT = 0.05
MAX_RANGE_PCT = 50.0

# User-facing percent (MIN_RANGE_PCT..MAX_RANGE_PCT).
range_width_pct: float = float(os.getenv("RANGE_WIDTH_PCT", "5"))

# Actual DLMM half-width in bins (activeId ± half).
half_width_bins: int = DEFAULT_RANGE_HALF


class RangeTooWideError(ValueError):
    """Requested percent exceeds what one ordinary position can hold."""

    def __init__(
        self,
        *,
        requested_pct: float,
        max_pct: float,
        max_half_width: int,
        max_bins_per_position: int,
        bin_step: int,
    ) -> None:
        self.requested_pct = requested_pct
        self.max_pct = max_pct
        self.max_half_width = max_half_width
        self.max_bins_per_position = max_bins_per_position
        self.bin_step = bin_step
        super().__init__(
            f"requested ±{requested_pct}% exceeds max ±{max_pct:.4f}% "
            f"for binStep={bin_step} (one position ≤ {max_bins_per_position} bins, "
            f"half ≤ {max_half_width})"
        )


class HalfWidthTooWideError(ValueError):
    """half_width_bins exceeds protocol max for one ordinary position."""

    def __init__(
        self,
        *,
        half_width: int,
        max_half_width: int,
        max_bins_per_position: int,
    ) -> None:
        self.half_width = half_width
        self.max_half_width = max_half_width
        self.max_bins_per_position = max_bins_per_position
        super().__init__(
            f"half_width={half_width} exceeds max {max_half_width} "
            f"(one position ≤ {max_bins_per_position} bins)"
        )


def max_half_width_bins(max_bins_per_position: int) -> int:
    """max_half such that width = 2*half+1 ≤ max_bins_per_position."""
    if max_bins_per_position < 1:
        raise ValueError("max_bins_per_position must be >= 1")
    return (int(max_bins_per_position) - 1) // 2


def max_pct_for_pool(bin_step: int, max_bins_per_position: int) -> float:
    """Largest ±pct% expressible with one ordinary position on this pool."""
    if bin_step <= 0:
        raise ValueError("binStep must be > 0")
    half = max_half_width_bins(max_bins_per_position)
    return ((1.0 + bin_step / 10_000.0) ** half - 1.0) * 100.0


def pct_to_half_width_bins(pct: float, bin_step: int) -> int:
    """Convert percent width to bin half-width.

    half_width_bins = round( ln(1 + pct/100) / ln(1 + binStep/10000) )
    """
    if bin_step <= 0:
        raise ValueError("binStep must be > 0")
    if not (MIN_RANGE_PCT <= pct <= MAX_RANGE_PCT):
        raise ValueError(f"pct must be in {MIN_RANGE_PCT}..{MAX_RANGE_PCT}")
    numer = math.log(1.0 + pct / 100.0)
    denom = math.log(1.0 + bin_step / 10_000.0)
    if denom <= 0:
        raise ValueError("invalid binStep for conversion")
    half = int(round(numer / denom))
    return max(1, half)


def apply_setrange(
    pct: float, bin_step: int, *, max_bins_per_position: int
) -> int:
    """Update runtime state from /setrange; returns new half_width_bins.

    Refuses (no silent clamp) when pct exceeds what one position can hold.
    """
    global range_width_pct, half_width_bins
    if not (MIN_RANGE_PCT <= pct <= MAX_RANGE_PCT):
        raise ValueError(f"pct must be in {MIN_RANGE_PCT}..{MAX_RANGE_PCT}")
    max_pct = max_pct_for_pool(bin_step, max_bins_per_position)
    max_half = max_half_width_bins(max_bins_per_position)
    if pct > max_pct + 1e-12:
        raise RangeTooWideError(
            requested_pct=pct,
            max_pct=max_pct,
            max_half_width=max_half,
            max_bins_per_position=max_bins_per_position,
            bin_step=bin_step,
        )
    half = pct_to_half_width_bins(pct, bin_step)
    if half > max_half:
        # Rounding edge: refuse rather than silent clamp.
        raise RangeTooWideError(
            requested_pct=pct,
            max_pct=max_pct,
            max_half_width=max_half,
            max_bins_per_position=max_bins_per_position,
            bin_step=bin_step,
        )
    range_width_pct = float(pct)
    half_width_bins = half
    return half


def assert_half_within_limit(half: int, max_bins_per_position: int) -> None:
    """Raise if half_width would build an oversize ordinary position."""
    max_half = max_half_width_bins(max_bins_per_position)
    if int(half) > max_half:
        raise HalfWidthTooWideError(
            half_width=int(half),
            max_half_width=max_half,
            max_bins_per_position=int(max_bins_per_position),
        )


def current_half_width() -> int:
    return int(half_width_bins)


def current_range_pct() -> float:
    return float(range_width_pct)
