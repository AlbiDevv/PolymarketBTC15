"""
Execution cost models — v3.0 addition.

TZ v3.0 §8: EV must not use abstract market price. The calculation must include
side-aware prices, platform fee, partial fill probability, and expected slippage.

Production implementation should use parameterizable modules:
  fee_model(), execution_cost_model(), fill_model()
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from exchange_client.base import Orderbook


@dataclass
class ExecutionEstimate:
    """Full execution cost breakdown for a single trade."""
    side: Literal["YES", "NO"]
    raw_price: float          # mid-price or theoretical entry
    effective_price: float    # after slippage & spread
    fee: float                # platform fee in absolute terms per $1 payout
    slippage: float           # expected slippage cost
    fill_probability: float   # 0-1, chance of fill at limit price
    total_cost: float         # effective_price + fee + slippage
    net_payout: float         # 1.0 - total_cost (if winning)


class FeeModel:
    """
    Platform fee adapter. Fee must not be hardcoded as a constant.
    Read from config or platform API.
    """

    def __init__(self, default_fee_pct: float = 0.02):
        self._default = default_fee_pct
        self._market_overrides: dict[str, float] = {}

    def set_market_fee(self, market_id: str, fee_pct: float):
        self._market_overrides[market_id] = fee_pct

    def get_fee(self, market_id: str = "") -> float:
        return self._market_overrides.get(market_id, self._default)

    def fee_on_profit(self, profit: float, market_id: str = "") -> float:
        """Polymarket charges fee on profit only, not on losing trades."""
        if profit <= 0:
            return 0.0
        return profit * self.get_fee(market_id)


class ExecutionCostModel:
    """
    Models slippage and effective execution price.
    Takes into account orderbook depth, intended size, and latency.
    """

    def __init__(
        self,
        latency_ticks: int = 1,
        tick_size: float = 0.01,
    ):
        self._latency_ticks = latency_ticks
        self._tick_size = tick_size

    def estimate_slippage(self, orderbook: Orderbook, size: float, side: Literal["YES", "NO"]) -> float:
        """
        Estimate execution slippage based on orderbook depth.
        Returns slippage as price delta (always positive).
        """
        levels = orderbook.asks if side == "YES" else orderbook.bids
        if not levels:
            return self._tick_size * 5

        remaining = size
        vwap_num = 0.0
        for lvl in levels:
            fill = min(remaining, lvl.size)
            vwap_num += fill * lvl.price
            remaining -= fill
            if remaining <= 0:
                break

        if remaining > 0:
            return self._tick_size * 10  # very thin book

        vwap = vwap_num / size
        ref_price = orderbook.best_ask if side == "YES" else orderbook.best_bid
        slippage = abs(vwap - ref_price) + self._latency_ticks * self._tick_size
        return max(slippage, 0)

    def effective_entry_price(
        self, orderbook: Orderbook, size: float, side: Literal["YES", "NO"]
    ) -> float:
        slippage = self.estimate_slippage(orderbook, size, side)
        if side == "YES":
            return orderbook.best_ask + slippage
        else:
            return (1 - orderbook.best_bid) + slippage


class FillModel:
    """
    Estimates probability of a limit order being filled.
    Depends on queue position, spread, and time-to-fill.
    """

    def __init__(self, base_fill_rate: float = 0.80, spread_sensitivity: float = 5.0):
        self._base_rate = base_fill_rate
        self._spread_sensitivity = spread_sensitivity

    def estimate_fill_probability(
        self,
        orderbook: Orderbook,
        limit_price: float,
        side: Literal["YES", "NO"],
    ) -> float:
        """
        Estimate fill probability for a limit order.
        Tighter spreads and better queue → higher fill rate.
        """
        spread = orderbook.spread
        if spread <= 0:
            return self._base_rate

        if side == "YES":
            distance = limit_price - orderbook.best_bid  # how aggressive
        else:
            distance = orderbook.best_ask - (1 - limit_price)

        aggressiveness = max(0, distance / spread)
        fill_prob = self._base_rate * min(1.0, 0.5 + aggressiveness * 0.5)

        return min(fill_prob, 0.99)


def estimate_execution(
    orderbook: Orderbook,
    side: Literal["YES", "NO"],
    size: float,
    fee_model: FeeModel | None = None,
    exec_model: ExecutionCostModel | None = None,
    fill_model: FillModel | None = None,
    market_id: str = "",
) -> ExecutionEstimate:
    """
    Full execution estimate combining all three models.
    This is the entry point for v3.0 execution-aware EV calculation.
    """
    if fee_model is None:
        fee_model = FeeModel()
    if exec_model is None:
        exec_model = ExecutionCostModel()
    if fill_model is None:
        fill_model = FillModel()

    raw_price = orderbook.best_ask if side == "YES" else (1 - orderbook.best_bid)
    slippage = exec_model.estimate_slippage(orderbook, size, side)
    effective = raw_price + slippage
    fee_rate = fee_model.get_fee(market_id)
    fee_abs = fee_rate  # per $1 payout on profit

    effective = min(effective, 0.99)
    net_payout = 1.0 - effective - fee_abs

    fill_prob = fill_model.estimate_fill_probability(orderbook, effective, side)

    return ExecutionEstimate(
        side=side,
        raw_price=raw_price,
        effective_price=effective,
        fee=fee_abs,
        slippage=slippage,
        fill_probability=fill_prob,
        total_cost=effective + fee_abs,
        net_payout=max(net_payout, 0),
    )
