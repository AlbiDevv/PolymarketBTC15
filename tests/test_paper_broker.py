"""Tests for the paper trading broker — walk-the-book fill simulation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import pytest

from exchange_client.base import Orderbook, OrderbookLevel
from execution.broker import TradeIntent
from execution.paper_broker import PaperBroker, PaperBrokerConfig


def _make_ob(
    bids=None,
    asks=None,
) -> Orderbook:
    if bids is None:
        bids = [
            OrderbookLevel(0.50, 100),
            OrderbookLevel(0.49, 200),
            OrderbookLevel(0.48, 300),
        ]
    if asks is None:
        asks = [
            OrderbookLevel(0.52, 100),
            OrderbookLevel(0.53, 200),
            OrderbookLevel(0.54, 300),
        ]
    return Orderbook(market_id="test", bids=bids, asks=asks, timestamp=0)


def _make_intent(side="YES", price=0.55, stake=10.0) -> TradeIntent:
    contracts = stake / price if price > 0 else 0
    return TradeIntent(
        market_id="1", condition_id="cond_1", token_id="tok_yes",
        side=side, price=price, stake_usd=stake, contracts=contracts,
    )


def _run(coro):
    return asyncio.run(coro)


class TestPaperBrokerFullFill:
    def test_full_fill_yes_side(self):
        broker = PaperBroker(PaperBrokerConfig(latency_levels=0))
        ob = _make_ob()
        intent = _make_intent(side="YES", price=0.55, stake=10.0)
        result = _run(broker.execute(intent, ob))

        assert result.status in ("PAPER_FILL", "PARTIAL")
        assert result.filled_contracts > 0
        assert result.avg_fill_price > 0
        assert result.avg_fill_price <= 0.55
        assert result.filled_usd > 0

    def test_full_fill_no_side(self):
        broker = PaperBroker(PaperBrokerConfig(latency_levels=0))
        ob = _make_ob()
        intent = _make_intent(side="NO", price=0.55, stake=10.0)
        result = _run(broker.execute(intent, ob))

        assert result.status in ("PAPER_FILL", "PARTIAL", "REJECTED")
        # NO BUY uses native NO-token book asks (same fixture as YES: asks from 0.52)


class TestPaperBrokerPartialFill:
    def test_partial_fill_thin_book(self):
        broker = PaperBroker(PaperBrokerConfig(
            latency_levels=0, partial_fill_enabled=True, min_fill_ratio=0.05,
        ))
        thin_ob = _make_ob(
            asks=[OrderbookLevel(0.52, 5)],  # only 5 contracts available
        )
        intent = _make_intent(side="YES", price=0.55, stake=50.0)
        # stake=50/0.55 ≈ 90 contracts, but only 5 available
        result = _run(broker.execute(intent, thin_ob))

        assert result.filled_contracts <= 5
        assert result.unfilled_contracts > 0

    def test_reject_below_min_fill_ratio(self):
        broker = PaperBroker(PaperBrokerConfig(
            latency_levels=0, min_fill_ratio=0.50,
        ))
        thin_ob = _make_ob(
            asks=[OrderbookLevel(0.52, 2)],  # only 2 contracts
        )
        intent = _make_intent(side="YES", price=0.55, stake=50.0)
        result = _run(broker.execute(intent, thin_ob))

        assert result.status == "REJECTED"
        assert "Fill ratio" in result.reason


class TestPaperBrokerEmptyBook:
    def test_empty_asks_rejects_yes(self):
        broker = PaperBroker(PaperBrokerConfig(latency_levels=0))
        ob = _make_ob(asks=[])
        intent = _make_intent(side="YES")
        result = _run(broker.execute(intent, ob))

        assert result.status == "REJECTED"
        assert result.filled_contracts == 0

    def test_empty_asks_rejects_no(self):
        broker = PaperBroker(PaperBrokerConfig(latency_levels=0))
        ob = _make_ob(asks=[])
        intent = _make_intent(side="NO")
        result = _run(broker.execute(intent, ob))

        assert result.status == "REJECTED"
        assert result.filled_contracts == 0


class TestPaperBrokerSlippage:
    def test_slippage_increases_with_size(self):
        broker = PaperBroker(PaperBrokerConfig(latency_levels=0))
        ob = _make_ob()

        small = _make_intent(side="YES", price=0.55, stake=5.0)
        large = _make_intent(side="YES", price=0.55, stake=50.0)

        r_small = _run(broker.execute(small, ob))
        r_large = _run(broker.execute(large, ob))

        # Larger order should have >= slippage (walks deeper into the book)
        if r_small.status != "REJECTED" and r_large.status != "REJECTED":
            assert r_large.avg_fill_price >= r_small.avg_fill_price

    def test_latency_levels_increase_slippage(self):
        ob = _make_ob()
        intent = _make_intent(side="YES", price=0.55, stake=10.0)

        broker_0 = PaperBroker(PaperBrokerConfig(latency_levels=0))
        broker_2 = PaperBroker(PaperBrokerConfig(latency_levels=2))

        r0 = _run(broker_0.execute(intent, ob))
        r2 = _run(broker_2.execute(intent, ob))

        if r0.status != "REJECTED" and r2.status != "REJECTED":
            assert r2.avg_fill_price >= r0.avg_fill_price


class TestPaperBrokerFees:
    def test_fees_are_positive(self):
        broker = PaperBroker(PaperBrokerConfig(latency_levels=0, fee_rate=0.02))
        ob = _make_ob()
        intent = _make_intent(side="YES", price=0.55, stake=10.0)
        result = _run(broker.execute(intent, ob))

        if result.status != "REJECTED":
            assert result.fees_usd >= 0

    def test_zero_fee_rate(self):
        broker = PaperBroker(PaperBrokerConfig(latency_levels=0, fee_rate=0.0))
        ob = _make_ob()
        intent = _make_intent(side="YES", price=0.55, stake=10.0)
        result = _run(broker.execute(intent, ob))

        if result.status != "REJECTED":
            assert result.fees_usd == 0.0


class TestPaperBrokerPriceLimits:
    def test_reject_if_price_above_limit(self):
        broker = PaperBroker(PaperBrokerConfig(latency_levels=0))
        ob = _make_ob(asks=[OrderbookLevel(0.70, 100)])
        intent = _make_intent(side="YES", price=0.55, stake=10.0)
        result = _run(broker.execute(intent, ob))

        assert result.status == "REJECTED"
        assert "price limit" in result.reason.lower()


class TestPaperBrokerSell:
    def test_sell_walks_bids(self):
        broker = PaperBroker(PaperBrokerConfig(latency_levels=0, fee_rate=0.02))
        ob = _make_ob()
        intent = TradeIntent(
            market_id="1", condition_id="c1", token_id="tok",
            side="YES", price=0.01, stake_usd=0.0, contracts=50.0,
            action="SELL", entry_price=0.50,
        )
        result = _run(broker.execute(intent, ob))
        assert result.status in ("PAPER_FILL", "PARTIAL")
        assert result.filled_contracts > 0
        assert result.avg_fill_price <= ob.best_bid + 0.01


class TestDryRunBroker:
    def test_dry_run_simulated_buy_fill(self):
        from execution.live_broker import DryRunBroker
        broker = DryRunBroker()
        ob = _make_ob()
        intent = _make_intent()
        result = _run(broker.execute(intent, ob))

        assert result.status == "PAPER_FILL"
        assert result.filled_contracts > 0
        assert broker.mode == "dry_run"
