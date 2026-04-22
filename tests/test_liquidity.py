import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LiquidityConfig
from exchange_client.liquidity import LiquidityFilter
from exchange_client.base import Market, Token, Orderbook, OrderbookLevel


def _make_market(volume: float = 1000) -> Market:
    return Market(
        id="test_market",
        question="Test?",
        category="test",
        end_date=None,
        resolution_source="test",
        active=True,
        volume_24h=volume,
        tokens=[Token(token_id="t1", outcome="Yes", price=0.5)],
    )


def _make_orderbook(
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
) -> Orderbook:
    if bids is None:
        bids = [(0.49, 100), (0.48, 200), (0.47, 300)]
    if asks is None:
        asks = [(0.51, 100), (0.52, 200), (0.53, 300)]
    return Orderbook(
        market_id="test",
        bids=[OrderbookLevel(p, s) for p, s in bids],
        asks=[OrderbookLevel(p, s) for p, s in asks],
        timestamp=0,
    )


def test_volume_filter_pass():
    f = LiquidityFilter(LiquidityConfig(min_daily_volume=500))
    result = f.check_market(_make_market(volume=1000))
    assert result.passed


def test_volume_filter_fail():
    f = LiquidityFilter(LiquidityConfig(min_daily_volume=500))
    result = f.check_market(_make_market(volume=100))
    assert not result.passed


def test_orderbook_depth_pass():
    f = LiquidityFilter(LiquidityConfig(min_depth_usd=10, max_price_impact=0.1))
    ob = _make_orderbook()
    result = f.check_orderbook(ob, intended_size=2.0)
    assert result.passed


def test_orderbook_too_thin():
    f = LiquidityFilter(LiquidityConfig(min_depth_usd=1000, max_price_impact=0.01))
    ob = _make_orderbook(
        bids=[(0.49, 1)],
        asks=[(0.51, 1)],
    )
    result = f.check_orderbook(ob, intended_size=2.0)
    assert not result.passed
