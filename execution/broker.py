"""
Execution broker abstraction.

Strategy layer produces a TradeIntent. Broker executes it.
Paper broker simulates fills locally. Live broker sends to exchange.
Dry broker logs only.

Units convention enforced here:
  - stake:     always USD (dollar amount to risk)
  - contracts: always shares (stake / price)
  - price:     always 0..1 (probability / token price)
  - side:      "YES" or "NO" (which outcome token to buy)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from exchange_client.base import Orderbook


@dataclass
class TradeIntent:
    """What the strategy layer wants to do. No network calls yet."""
    market_id: str
    condition_id: str
    token_id: str
    side: Literal["YES", "NO"]
    price: float          # BUY: max pay; SELL: min acceptable (floor), use ~0 for market
    stake_usd: float      # dollar amount to risk (BUY); ignored for SELL (use contracts)
    contracts: float      # shares = stake / price for BUY; shares to close for SELL
    action: Literal["BUY", "SELL"] = "BUY"
    hypothesis_id: str = ""
    edge: float = 0.0
    order_ttl_sec: int = 300
    entry_price: float | None = None  # long entry — for profit-based fee on SELL (paper)

    @property
    def notional(self) -> float:
        return self.contracts * self.price


@dataclass
class FillResult:
    """Standardized fill report from any broker."""
    order_id: str
    status: str                     # FILLED, PARTIAL, REJECTED, DRY_RUN, PAPER_FILL
    filled_contracts: float = 0.0   # shares actually filled
    avg_fill_price: float = 0.0     # VWAP of filled portion
    filled_usd: float = 0.0        # filled_contracts * avg_fill_price
    unfilled_contracts: float = 0.0
    fees_usd: float = 0.0
    slippage: float = 0.0          # price delta from intended
    latency_ms: float = 0.0
    reason: str = ""
    raw_response: dict = field(default_factory=dict)


class ExecutionBroker(ABC):
    """
    Abstract broker interface.
    
    Strategy calls broker.execute(intent, orderbook).
    Broker returns FillResult without strategy knowing paper vs live.
    """

    @abstractmethod
    async def execute(self, intent: TradeIntent, orderbook: Orderbook) -> FillResult:
        ...

    @abstractmethod
    async def cancel(self, order_id: str) -> bool:
        ...

    @abstractmethod
    async def cancel_all(self) -> int:
        ...

    @property
    @abstractmethod
    def mode(self) -> str:
        """Returns 'dry_run', 'paper', or 'live'."""
        ...
