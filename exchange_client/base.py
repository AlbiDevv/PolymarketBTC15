from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Market:
    id: str
    question: str
    category: str
    end_date: str | None
    resolution_source: str
    active: bool
    volume_24h: float
    tokens: list[Token]
    event_id: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class Token:
    token_id: str
    outcome: str  # "Yes" or "No"
    price: float
    winner: bool | None = None


@dataclass
class OrderbookLevel:
    price: float
    size: float


@dataclass
class Orderbook:
    market_id: str
    bids: list[OrderbookLevel]
    asks: list[OrderbookLevel]
    timestamp: float

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    def depth(self, side: Literal["bid", "ask"], pct_from_mid: float = 0.02) -> float:
        mid = self.mid_price
        levels = self.bids if side == "bid" else self.asks
        total = 0.0
        for lvl in levels:
            if side == "bid" and lvl.price >= mid * (1 - pct_from_mid):
                total += lvl.price * lvl.size
            elif side == "ask" and lvl.price <= mid * (1 + pct_from_mid):
                total += lvl.price * lvl.size
        return total


@dataclass
class Trade:
    market_id: str
    side: Literal["YES", "NO"]
    price: float
    size: float
    timestamp: float


@dataclass
class Position:
    market_id: str
    token_id: str
    side: Literal["YES", "NO"]
    size: float
    avg_price: float


@dataclass
class OrderResult:
    order_id: str
    status: str
    filled_size: float = 0.0
    avg_fill_price: float = 0.0


class ExchangeClientBase(ABC):
    @abstractmethod
    async def get_markets(self, active_only: bool = True) -> list[Market]:
        ...

    @abstractmethod
    async def get_orderbook(self, token_id: str) -> Orderbook:
        ...

    @abstractmethod
    async def get_trade_history(
        self, market_id: str, limit: int = 100
    ) -> list[Trade]:
        ...

    @abstractmethod
    async def place_order(
        self,
        token_id: str,
        side: Literal["BUY", "SELL"],
        price: float,
        size: float,
    ) -> OrderResult:
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        ...

    @abstractmethod
    async def cancel_all_orders(self) -> int:
        ...
