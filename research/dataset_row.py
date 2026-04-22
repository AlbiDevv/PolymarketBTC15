"""
Typed row matching dataset_spec_v1.yaml (minimal dataclass for pipelines).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Side = Literal["YES", "NO"]
Source = Literal["historical", "paper", "mixed"]


@dataclass
class ResearchDatasetRowV1:
    dataset_version: str
    feature_version: str
    split_version: str
    market_id: str
    event_id: str | None
    token_id: str
    side: Side
    decision_ts: datetime
    category: str
    time_to_resolution_sec: float
    tte_bucket: str
    spread: float
    spread_bucket: str
    liquidity_proxy: float
    liquidity_bucket: str
    depth_bid: float | None
    depth_ask: float | None
    p_market: float
    mid_price_if_needed_for_reference: float | None
    execution_price_assumption: str
    entry_fee_assumption: float
    exit_fee_assumption: float
    source: Source
    resolved_outcome_for_side: int
    resolved_ts: datetime | None
    short_term_return: float | None = None
    recent_volatility: float | None = None
    orderbook_imbalance: float | None = None
    quality_flags: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    # native_yes | native_no | complement_fallback | legacy_unlabeled
    p_market_source: str = "legacy_unlabeled"
