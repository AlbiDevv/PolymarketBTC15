"""Legacy paper fill tests — updated to use new PaperBroker and DryRunBroker."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

from exchange_client.base import Orderbook, OrderbookLevel
from execution.broker import TradeIntent
from execution.paper_broker import PaperBroker, PaperBrokerConfig
from execution.live_broker import DryRunBroker


def _run(coro):
    return asyncio.run(coro)


def _make_ob() -> Orderbook:
    return Orderbook(
        market_id="test",
        bids=[OrderbookLevel(0.48, 200), OrderbookLevel(0.47, 300)],
        asks=[OrderbookLevel(0.52, 200), OrderbookLevel(0.53, 300)],
        timestamp=0,
    )


def _make_intent() -> TradeIntent:
    return TradeIntent(
        market_id="1", condition_id="cond_1", token_id="tok_yes",
        side="YES", price=0.55, stake_usd=2.0, contracts=3.85,
    )


def test_dry_run_buy_simulates_fill_like_paper():
    broker = DryRunBroker()
    result = _run(broker.execute(_make_intent(), _make_ob()))
    assert result.status == "PAPER_FILL"
    assert result.filled_contracts > 0
    assert result.order_id.startswith("dry_")


def test_paper_fill_returns_valid_result():
    broker = PaperBroker(PaperBrokerConfig(latency_levels=0, fee_rate=0.02))
    ob = _make_ob()
    intent = _make_intent()

    result = _run(broker.execute(intent, ob))
    assert result.order_id.startswith("paper_")
    if result.status in ("PAPER_FILL", "PARTIAL"):
        assert result.filled_contracts > 0
        assert result.avg_fill_price >= 0.52  # at least best ask


def test_paper_fill_empty_book_rejects():
    broker = PaperBroker(PaperBrokerConfig(latency_levels=0))
    empty_ob = Orderbook(market_id="test", bids=[], asks=[], timestamp=0)
    intent = _make_intent()
    result = _run(broker.execute(intent, empty_ob))
    assert result.status == "REJECTED"
    assert result.filled_contracts == 0
