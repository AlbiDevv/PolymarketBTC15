import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exchange_client.base import Orderbook, OrderbookLevel
from lab.shadow_engine import ShadowMakerEngine


def _book(bid: float = 0.40, ask: float = 0.42, bid_size: float = 10.0, ask_size: float = 10.0) -> Orderbook:
    return Orderbook(
        market_id="tok",
        bids=[OrderbookLevel(bid, bid_size)],
        asks=[OrderbookLevel(ask, ask_size)],
        timestamp=0.0,
    )


def test_taker_fill_walks_orderbook_depth_for_effective_price():
    engine = ShadowMakerEngine(ttl_sec=30, reprice_sec=5, max_reprices=3)
    book = Orderbook(
        market_id="tok",
        bids=[OrderbookLevel(0.39, 20.0)],
        asks=[OrderbookLevel(0.42, 5.0), OrderbookLevel(0.44, 5.0)],
        timestamp=0.0,
    )

    preview = engine.simulate_taker_fill_size(book, "BUY", 10.0)

    assert preview is not None
    assert preview.full_size is True
    assert preview.filled_size == 10.0
    assert round(preview.avg_price, 6) == 0.43
    assert round(preview.slippage_usdc, 6) == 0.1


def test_taker_fill_applies_latency_penalty_after_stale_threshold():
    engine = ShadowMakerEngine(
        ttl_sec=30,
        reprice_sec=5,
        max_reprices=3,
        latency_penalty_bps=10.0,
        max_event_age_ms=1000,
    )
    book = Orderbook(
        market_id="tok",
        bids=[OrderbookLevel(0.39, 20.0)],
        asks=[OrderbookLevel(0.42, 10.0)],
        timestamp=0.0,
    )

    fresh = engine.simulate_taker_fill_size(book, "BUY", 5.0, event_age_ms=500)
    stale = engine.simulate_taker_fill_size(book, "BUY", 5.0, event_age_ms=2500)

    assert fresh is not None
    assert stale is not None
    assert round(fresh.avg_price, 6) == 0.42
    assert round(stale.avg_price, 6) == 0.42042
    assert round(stale.latency_penalty_per_share, 6) == 0.00042
    assert round(stale.latency_penalty_usdc, 6) == 0.0021


def test_taker_fill_by_notional_rejects_insufficient_depth():
    engine = ShadowMakerEngine(ttl_sec=30, reprice_sec=5, max_reprices=3)
    book = Orderbook(
        market_id="tok",
        bids=[OrderbookLevel(0.39, 20.0)],
        asks=[OrderbookLevel(0.50, 2.0)],
        timestamp=0.0,
    )

    preview = engine.simulate_taker_fill_notional(book, "BUY", 10.0)

    assert preview is not None
    assert preview.full_size is False
    assert preview.notional == 1.0


def test_quote_entry_price_improves_without_crossing():
    engine = ShadowMakerEngine(ttl_sec=30, reprice_sec=5, max_reprices=3)
    assert engine.quote_entry_price(_book(0.40, 0.42), "BUY", 0.01) == 0.41
    assert engine.quote_entry_price(_book(0.41, 0.42), "BUY", 0.01) == 0.41
    assert engine.quote_entry_price(_book(0.40, 0.42), "SELL", 0.01) == 0.41


def test_queue_ahead_depletes_on_book_change():
    engine = ShadowMakerEngine(ttl_sec=30, reprice_sec=5, max_reprices=3)
    now = datetime.now(timezone.utc)
    order = engine.create_order(
        portfolio_key="H2_base",
        market_id="m1",
        market_db_id=1,
        token_id="tok",
        event_id="e1",
        side="YES",
        action="BUY",
        price=0.40,
        size=5.0,
        queue_ahead=10.0,
        hypothesis="H2",
        edge=0.03,
        now=now,
    )

    depleted = engine.observe_book(order, _book(0.40, 0.42, bid_size=6.0))
    assert depleted == 4.0
    assert order.queue_ahead == 6.0


def test_partial_fill_progression_after_queue_is_cleared():
    engine = ShadowMakerEngine(ttl_sec=30, reprice_sec=5, max_reprices=3)
    now = datetime.now(timezone.utc)
    order = engine.create_order(
        portfolio_key="H2_base",
        market_id="m1",
        market_db_id=1,
        token_id="tok",
        event_id="e1",
        side="YES",
        action="BUY",
        price=0.40,
        size=4.0,
        queue_ahead=2.0,
        hypothesis="H2",
        edge=0.03,
        now=now,
    )

    fill = engine.process_trade(order, trade_price=0.40, trade_size=3.0, aggressor_side="SELL")
    assert fill is not None
    assert fill.filled_size == 1.0
    assert order.status == "partial"
    assert order.size_remaining == 3.0

    fill2 = engine.process_trade(order, trade_price=0.40, trade_size=10.0, aggressor_side="SELL")
    assert fill2 is not None
    assert fill2.filled_size == 3.0
    assert order.status == "filled"
    assert order.size_remaining == 0.0


def test_reprice_and_forced_taker_exit_decision():
    engine = ShadowMakerEngine(ttl_sec=30, reprice_sec=5, max_reprices=3, latency_penalty_bps=10.0, max_event_age_ms=1000)
    now = datetime.now(timezone.utc)
    order = engine.create_order(
        portfolio_key="H2_base",
        market_id="m1",
        market_db_id=1,
        token_id="tok",
        event_id="e1",
        side="YES",
        action="SELL",
        price=0.41,
        size=4.0,
        queue_ahead=1.0,
        hypothesis="H2",
        edge=0.03,
        now=now,
    )

    decision = engine.reprice_decision(order, now=now + timedelta(seconds=5), is_exit=True)
    assert decision.should_reprice is True

    order.reprices = 3
    order.expires_at = now
    decision2 = engine.reprice_decision(order, now=now + timedelta(seconds=6), is_exit=True)
    assert decision2.expired is True
    assert decision2.forced_taker_exit is True

    forced_fill = engine.force_taker_fill(
        order,
        _book(0.40, 0.42, bid_size=5.0, ask_size=5.0),
        event_age_ms=2500,
    )
    assert forced_fill is not None
    assert forced_fill.fill_type == "forced_taker_exit"
    assert forced_fill.latency_ms == 2500
