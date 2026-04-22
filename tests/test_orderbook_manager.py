"""Tests for local orderbook state management."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time

from exchange_client.base import Orderbook, OrderbookLevel
from market_data.orderbook_manager import LocalOrderbook, OrderbookManager


def _make_snapshot() -> Orderbook:
    return Orderbook(
        market_id="tok_1",
        bids=[
            OrderbookLevel(0.50, 100),
            OrderbookLevel(0.49, 200),
        ],
        asks=[
            OrderbookLevel(0.52, 100),
            OrderbookLevel(0.53, 200),
        ],
        timestamp=time.time(),
    )


class TestLocalOrderbook:
    def test_snapshot_replaces_book(self):
        book = LocalOrderbook(token_id="tok_1")
        book.bids[0.45] = 999  # stale data
        book.apply_snapshot(_make_snapshot())

        assert 0.45 not in book.bids
        assert book.bids[0.50] == 100
        assert book.asks[0.52] == 100

    def test_delta_adds_level(self):
        book = LocalOrderbook(token_id="tok_1")
        book.apply_snapshot(_make_snapshot())
        book.apply_delta("bid", 0.48, 150)

        assert book.bids[0.48] == 150
        assert book.update_count == 1

    def test_delta_removes_level_on_zero_size(self):
        book = LocalOrderbook(token_id="tok_1")
        book.apply_snapshot(_make_snapshot())
        book.apply_delta("bid", 0.50, 0)

        assert 0.50 not in book.bids

    def test_delta_updates_existing_level(self):
        book = LocalOrderbook(token_id="tok_1")
        book.apply_snapshot(_make_snapshot())
        book.apply_delta("ask", 0.52, 500)

        assert book.asks[0.52] == 500

    def test_to_orderbook_sorted(self):
        book = LocalOrderbook(token_id="tok_1")
        book.apply_snapshot(_make_snapshot())
        book.apply_delta("bid", 0.51, 50)
        book.apply_delta("ask", 0.515, 75)

        ob = book.to_orderbook()
        assert ob.bids[0].price == 0.51  # highest bid first
        assert ob.asks[0].price == 0.515  # lowest ask first

    def test_stale_detection(self):
        book = LocalOrderbook(token_id="tok_1")
        book.last_update_ts = time.time() - 120  # 2 minutes ago
        assert book.is_stale

    def test_fresh_not_stale(self):
        book = LocalOrderbook(token_id="tok_1")
        book.apply_snapshot(_make_snapshot())
        assert not book.is_stale


class TestOrderbookManager:
    def test_get_or_create(self):
        mgr = OrderbookManager()
        book = mgr.get_or_create("tok_1")
        assert book.token_id == "tok_1"

        same = mgr.get_or_create("tok_1")
        assert same is book

    def test_apply_snapshot_and_get(self):
        mgr = OrderbookManager()
        mgr.apply_snapshot("tok_1", _make_snapshot())

        ob = mgr.get_orderbook("tok_1")
        assert ob is not None
        assert len(ob.bids) == 2
        assert len(ob.asks) == 2

    def test_get_missing_returns_none(self):
        mgr = OrderbookManager()
        assert mgr.get_orderbook("unknown") is None

    def test_stale_tokens(self):
        mgr = OrderbookManager()
        book = mgr.get_or_create("tok_stale")
        book.last_update_ts = time.time() - 120

        mgr.apply_snapshot("tok_fresh", _make_snapshot())

        stale = mgr.stale_tokens()
        assert "tok_stale" in stale
        assert "tok_fresh" not in stale

    def test_remove(self):
        mgr = OrderbookManager()
        mgr.apply_snapshot("tok_1", _make_snapshot())
        mgr.remove("tok_1")
        assert mgr.get("tok_1") is None

    def test_subscribed_count(self):
        mgr = OrderbookManager()
        assert mgr.subscribed_count == 0
        mgr.get_or_create("a")
        mgr.get_or_create("b")
        assert mgr.subscribed_count == 2


class TestOrderbookConsistency:
    """Verify that snapshot→delta→to_orderbook produces consistent state."""

    def test_apply_delta_after_snapshot(self):
        mgr = OrderbookManager()
        mgr.apply_snapshot("tok_1", _make_snapshot())

        mgr.apply_delta("tok_1", "ask", 0.52, 0)     # remove best ask
        mgr.apply_delta("tok_1", "ask", 0.515, 300)   # add new level

        ob = mgr.get_orderbook("tok_1")
        assert ob.best_ask == 0.515
        assert ob.asks[0].size == 300

    def test_crossed_book_detection(self):
        """If deltas create a crossed book (best_bid >= best_ask), detect it."""
        book = LocalOrderbook(token_id="tok_1")
        book.apply_snapshot(_make_snapshot())
        book.apply_delta("bid", 0.55, 100)  # bid above ask!

        ob = book.to_orderbook()
        # Crossed book: best_bid=0.55 >= best_ask=0.52
        assert ob.best_bid >= ob.best_ask
        # The spread would be negative — caller should check
        assert ob.spread < 0
