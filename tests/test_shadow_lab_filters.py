import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Settings
from exchange_client.base import Market, Orderbook, OrderbookLevel, Token
from lab.runtime import ShadowLabRunner


def _market(end_days: int, volume: float = 600.0) -> Market:
    end = (datetime.now(timezone.utc) + timedelta(days=end_days)).isoformat()
    return Market(
        id=f"m{end_days}",
        question="test market",
        category="politics",
        end_date=end,
        resolution_source="",
        active=True,
        volume_24h=volume,
        tokens=[
            Token(token_id=f"yes_{end_days}", outcome="Yes", price=0.4),
            Token(token_id=f"no_{end_days}", outcome="No", price=0.6),
        ],
        event_id=f"e{end_days}",
    )


def _book(spread: float = 0.10, depth_size: float = 200.0) -> Orderbook:
    bid = 0.40
    ask = bid + spread
    return Orderbook(
        market_id="tok",
        bids=[OrderbookLevel(bid, depth_size)],
        asks=[OrderbookLevel(ask, depth_size)],
        timestamp=0.0,
    )


def test_universe_filter_blocks_long_horizon_markets(tmp_path):
    settings = Settings()
    settings.database.url = f"sqlite:///{(tmp_path / 'lab.db').as_posix()}"
    runner = ShadowLabRunner(settings)
    try:
        assert runner._market_is_eligible_for_universe(_market(10)) is True
        assert runner._market_is_eligible_for_universe(_market(40)) is False
    finally:
        asyncio.run(runner._client.close())


def test_base_and_strict_pack_filters_route_differently():
    settings = Settings()
    base = next(p for p in settings.lab.portfolios if p.key == "H2_base")
    strict = next(p for p in settings.lab.portfolios if p.key == "H2_strict")

    candidate = _market(10, volume=800.0)
    orderbook = _book(spread=0.10, depth_size=150.0)

    assert ShadowLabRunner._market_passes_portfolio_filters(base, candidate, orderbook) is True
    assert ShadowLabRunner._market_passes_portfolio_filters(strict, candidate, orderbook) is False
