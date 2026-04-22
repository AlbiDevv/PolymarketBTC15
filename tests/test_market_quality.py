import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LabMarketQualityConfig, LabPortfolioConfig
from exchange_client.base import Market, Orderbook, OrderbookLevel, Token
from lab.market_quality import assess_market_quality


def _market(question: str, *, end_days: int = 2, volume: float = 8000.0) -> Market:
    return Market(
        id="m1",
        question=question,
        category="politics",
        end_date=(datetime.now(timezone.utc) + timedelta(days=end_days)).isoformat(),
        resolution_source="",
        active=True,
        volume_24h=volume,
        tokens=[
            Token(token_id="yes", outcome="Yes", price=0.95),
            Token(token_id="no", outcome="No", price=0.05),
        ],
        event_id="e1",
    )


def _book(spread: float = 0.02, depth: float = 1000.0) -> Orderbook:
    return Orderbook(
        market_id="yes",
        bids=[OrderbookLevel(0.94, depth)],
        asks=[OrderbookLevel(0.96, depth)],
        timestamp=0.0,
    )


def test_market_quality_rejects_social_keyword_markets():
    cfg = LabMarketQualityConfig()
    portfolio = LabPortfolioConfig(key="Late_balanced", hypotheses=["H6"], track="late_stage")
    assessment = assess_market_quality(
        cfg,
        portfolio,
        _market("Will Elon post 220 tweets this week?"),
        _book(),
        expected_edge=0.03,
        fee_rate=0.02,
    )
    assert assessment.hard_reject is True
    assert "keyword_social_metric" in assessment.reasons


def test_market_quality_rejects_negative_net_edge_after_fee():
    cfg = LabMarketQualityConfig()
    portfolio = LabPortfolioConfig(key="Late_balanced", hypotheses=["H6"], track="late_stage", min_quality_score=55.0)
    assessment = assess_market_quality(
        cfg,
        portfolio,
        _market("Will measured event happen by Friday?"),
        _book(spread=0.03),
        expected_edge=0.01,
        fee_rate=0.02,
    )
    assert "fee_too_high" in assessment.reasons
    assert assessment.expected_net_edge < 0
