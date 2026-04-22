"""
Discrete buckets for TTE, spread, liquidity, round-number zones, tails.
Boundaries are versioned — change `BUCKET_POLICY_VERSION` when adjusting.
"""

from __future__ import annotations

BUCKET_POLICY_VERSION = "v1"

# TTE seconds → bucket labels
TTE_EDGES_SEC = [
    (0, 86_400, "0-1d"),
    (86_400, 7 * 86_400, "1-7d"),
    (7 * 86_400, 30 * 86_400, "7-30d"),
    (30 * 86_400, 365 * 86_400, "30d-1y"),
]


def tte_bucket(time_to_resolution_sec: float | None) -> str:
    if time_to_resolution_sec is None or time_to_resolution_sec < 0:
        return "unknown"
    t = float(time_to_resolution_sec)
    for lo, hi, label in TTE_EDGES_SEC:
        if lo <= t < hi:
            return label
    return "1y+"


# Spread (absolute) on token book
SPREAD_EDGES = [(0.0, 0.02, "tight"), (0.02, 0.08, "medium"), (0.08, 1.0, "wide")]


def spread_bucket(spread: float | None) -> str:
    if spread is None or spread < 0:
        return "unknown"
    for lo, hi, label in SPREAD_EDGES:
        if lo <= spread < hi:
            return label
    return "wide"


# Liquidity proxy = volume_24h (or project-specific)
LIQ_EDGES = [(0, 1_000, "low"), (1_000, 50_000, "med"), (50_000, 1e18, "high")]


def liquidity_bucket(liquidity_proxy: float | None) -> str:
    if liquidity_proxy is None:
        return "unknown"
    v = float(liquidity_proxy)
    for lo, hi, label in LIQ_EDGES:
        if lo <= v < hi:
            return label
    return "high"


# H2-style round zones on YES-implied probability (use same for NO native mid)
ROUND_ZONES = [
    (0.08, 0.12, "near_0.10"),
    (0.45, 0.55, "near_0.50"),
    (0.88, 0.92, "near_0.90"),
]


def round_zone_bucket(p: float | None) -> str:
    if p is None:
        return "unknown"
    for lo, hi, label in ROUND_ZONES:
        if lo <= p <= hi:
            return label
    return "non_round"


TAIL_LOW = 0.10
TAIL_HIGH = 0.90


def tail_bucket(p: float | None) -> str:
    if p is None:
        return "unknown"
    if p < TAIL_LOW:
        return "low_tail"
    if p > TAIL_HIGH:
        return "high_tail"
    return "mid"
