"""
Local orderbook state management.

Maintains an in-memory copy of the orderbook for subscribed tokens.
Supports both snapshot (from REST) and delta updates (from WebSocket).
Detects stale data and triggers re-sync.

Inspired by discountry/polymarket-trading-bot OrderbookSnapshot pattern
but adapted to our architecture.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from loguru import logger

from exchange_client.base import Orderbook, OrderbookLevel


@dataclass
class LocalOrderbook:
    """In-memory orderbook for one token."""
    token_id: str
    bids: dict[float, float] = field(default_factory=dict)  # price → size
    asks: dict[float, float] = field(default_factory=dict)
    last_update_ts: float = 0.0
    snapshot_ts: float = 0.0
    update_count: int = 0

    def apply_snapshot(self, ob: Orderbook):
        """Replace entire book from a REST snapshot."""
        self.bids.clear()
        self.asks.clear()
        for lvl in ob.bids:
            if lvl.size > 0:
                self.bids[lvl.price] = lvl.size
        for lvl in ob.asks:
            if lvl.size > 0:
                self.asks[lvl.price] = lvl.size
        self.snapshot_ts = time.time()
        self.last_update_ts = time.time()

    def apply_delta(self, side: Literal["bid", "ask"], price: float, size: float):
        """Apply an incremental update from WebSocket."""
        book = self.bids if side == "bid" else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size
        self.last_update_ts = time.time()
        self.update_count += 1

    def to_orderbook(self) -> Orderbook:
        """Convert to the standard Orderbook dataclass."""
        sorted_bids = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)
        sorted_asks = sorted(self.asks.items(), key=lambda x: x[0])
        return Orderbook(
            market_id=self.token_id,
            bids=[OrderbookLevel(price=p, size=s) for p, s in sorted_bids],
            asks=[OrderbookLevel(price=p, size=s) for p, s in sorted_asks],
            timestamp=self.last_update_ts,
        )

    @property
    def is_stale(self) -> bool:
        """Book is stale if no update for > 60 seconds."""
        return (time.time() - self.last_update_ts) > 60.0

    @property
    def age_sec(self) -> float:
        return time.time() - self.last_update_ts


class OrderbookManager:
    """
    Manages multiple LocalOrderbook instances.
    Provides snapshot + delta API.
    """

    def __init__(self, stale_threshold_sec: float = 60.0):
        self._books: dict[str, LocalOrderbook] = {}
        self._stale_threshold = stale_threshold_sec

    def get(self, token_id: str) -> LocalOrderbook | None:
        return self._books.get(token_id)

    def get_or_create(self, token_id: str) -> LocalOrderbook:
        if token_id not in self._books:
            self._books[token_id] = LocalOrderbook(token_id=token_id)
        return self._books[token_id]

    def apply_snapshot(self, token_id: str, ob: Orderbook):
        book = self.get_or_create(token_id)
        book.apply_snapshot(ob)
        logger.debug(
            f"Orderbook snapshot: {token_id[:12]}... "
            f"bids={len(book.bids)} asks={len(book.asks)}"
        )

    def apply_delta(self, token_id: str, side: Literal["bid", "ask"],
                     price: float, size: float):
        book = self.get_or_create(token_id)
        book.apply_delta(side, price, size)

    def get_orderbook(self, token_id: str) -> Orderbook | None:
        """Get current orderbook as standard Orderbook, or None if missing."""
        book = self._books.get(token_id)
        if book is None:
            return None
        return book.to_orderbook()

    def stale_tokens(self) -> list[str]:
        """Return token IDs with stale orderbooks."""
        return [
            tid for tid, book in self._books.items()
            if book.is_stale
        ]

    def remove(self, token_id: str):
        self._books.pop(token_id, None)

    def invalidate_all(self):
        """After WS disconnect: force REST refresh (treat as stale)."""
        for book in self._books.values():
            book.last_update_ts = 0.0

    @property
    def subscribed_count(self) -> int:
        return len(self._books)
