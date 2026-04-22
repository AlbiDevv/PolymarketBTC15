from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from config import LabMarketQualityConfig, LabPortfolioConfig
from exchange_client.base import Market, Orderbook

from .utils import time_to_resolution_days


@dataclass
class MarketQualityAssessment:
    score: float
    reasons: list[str] = field(default_factory=list)
    hard_reject: bool = False
    time_to_resolution_hours: float | None = None
    spread: float = 0.0
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    estimated_fee_rate: float = 0.0
    estimated_slippage: float = 0.0
    expected_net_edge: float = 0.0


def top5_depth_usd(orderbook: Orderbook, side: str) -> float:
    levels = orderbook.bids if side == "bid" else orderbook.asks
    return sum(level.price * level.size for level in levels[:5])


def estimate_slippage(orderbook: Orderbook) -> float:
    if orderbook.best_bid <= 0 or orderbook.best_ask <= 0:
        return 0.0
    return max(0.0, orderbook.spread / 2.0)


def assess_market_quality(
    cfg: LabMarketQualityConfig,
    portfolio: LabPortfolioConfig,
    market: Market,
    orderbook: Orderbook,
    *,
    now: datetime | None = None,
    expected_edge: float = 0.0,
    fee_rate: float = 0.0,
) -> MarketQualityAssessment:
    reasons: list[str] = []
    hard_reject = False
    score = 100.0

    horizon_days = time_to_resolution_days(market.end_date, now)
    horizon_hours = (horizon_days * 24.0) if horizon_days is not None else None
    bid_depth = top5_depth_usd(orderbook, "bid")
    ask_depth = top5_depth_usd(orderbook, "ask")
    slippage = estimate_slippage(orderbook)

    question_lc = (market.question or "").lower()
    category_lc = (market.category or "").strip().lower()

    if cfg.require_end_date and horizon_days is None:
        reasons.append("missing_end_date")
        hard_reject = True
    if cfg.require_yes_no_pair and len(market.tokens) < 2:
        reasons.append("missing_yes_no_pair")
        hard_reject = True
    if horizon_days is not None and horizon_days < 0:
        reasons.append("expired_market")
        hard_reject = True
    if horizon_days is not None and horizon_days > portfolio.max_horizon_days:
        reasons.append("outside_horizon")
        hard_reject = True

    if market.volume_24h < portfolio.min_daily_volume:
        reasons.append("low_volume")
        hard_reject = True
    if orderbook.spread > portfolio.max_spread:
        reasons.append("wide_spread")
        hard_reject = True
    if bid_depth < portfolio.min_depth_usd or ask_depth < portfolio.min_depth_usd:
        reasons.append("low_depth")
        hard_reject = True

    social_hits = [
        keyword for keyword in cfg.hard_block_keywords_social
        if keyword.lower() in question_lc
    ]
    if social_hits:
        reasons.append("keyword_social_metric")
        hard_reject = True

    dispute_hits = [
        keyword for keyword in cfg.hard_block_keywords_dispute
        if keyword.lower() in question_lc
    ]
    if dispute_hits:
        reasons.append("keyword_dispute_resolution")
        hard_reject = True

    if not category_lc:
        score -= cfg.weak_category_penalty
    if category_lc in {"politics", "world", "news", "geopolitics"}:
        has_measurable_wording = any(marker in question_lc for marker in (" by ", " on ", " before ", " after ", "?"))
        if not has_measurable_wording:
            score -= cfg.politics_penalty

    expected_net_edge = expected_edge - fee_rate - slippage
    if expected_edge > 0 and expected_net_edge <= 0:
        reasons.append("fee_too_high")
        score -= cfg.fee_penalty

    min_score = portfolio.min_quality_score or cfg.min_score_default
    if score < min_score:
        reasons.append("low_quality_score")
        hard_reject = True

    return MarketQualityAssessment(
        score=max(0.0, score),
        reasons=list(dict.fromkeys(reasons)),
        hard_reject=hard_reject,
        time_to_resolution_hours=horizon_hours,
        spread=orderbook.spread,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        estimated_fee_rate=fee_rate,
        estimated_slippage=slippage,
        expected_net_edge=expected_net_edge,
    )
