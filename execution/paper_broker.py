"""
Paper trading broker — realistic fill simulation.

Inspired by teddytennant/polymarket-bot PaperTradingEngine:
- Walks the orderbook level by level
- Partial fills when liquidity is insufficient
- Slippage proportional to size vs available depth
- BUY: YES/NO use native token orderbook asks
- SELL: walk bids on the same token book (closing long)
- Fee on profit (Polymarket model)

Never sends anything to the exchange.
"""

from __future__ import annotations

import time
import random
from dataclasses import dataclass

from loguru import logger

from exchange_client.base import Orderbook, OrderbookLevel
from .broker import ExecutionBroker, TradeIntent, FillResult


@dataclass
class PaperBrokerConfig:
    fee_rate: float = 0.02          # Polymarket charges ~2% on profit
    latency_levels: int = 1         # skip top N levels to simulate latency
    min_fill_ratio: float = 0.10    # reject BUY if less than 10% can fill
    min_exit_fill_ratio: float = 0.01  # SELL: accept almost any partial
    partial_fill_enabled: bool = True
    random_seed: int | None = None  # set for reproducible results


class PaperBroker(ExecutionBroker):
    """
    Simulates order execution against the current orderbook snapshot.

    BUY: walk asks (native token book for YES or NO).
    SELL: walk bids, limit price is minimum acceptable per share.
    """

    def __init__(self, config: PaperBrokerConfig | None = None):
        self._cfg = config or PaperBrokerConfig()
        self._rng = random.Random(self._cfg.random_seed)
        self._order_counter = 0

    @property
    def mode(self) -> str:
        return "paper"

    async def execute(self, intent: TradeIntent, orderbook: Orderbook) -> FillResult:
        if intent.action == "SELL":
            return await self._execute_sell(intent, orderbook)
        return await self._execute_buy(intent, orderbook)

    async def _execute_buy(self, intent: TradeIntent, orderbook: Orderbook) -> FillResult:
        t0 = time.monotonic()
        self._order_counter += 1
        order_id = f"paper_{int(time.time() * 1000)}_{self._order_counter}"

        levels = self._buy_levels(orderbook, intent.side)
        if not levels:
            return FillResult(
                order_id=order_id, status="REJECTED",
                reason="Empty orderbook for this side",
            )

        available_levels = levels[self._cfg.latency_levels:]
        if not available_levels:
            available_levels = levels

        remaining = intent.contracts
        filled_contracts = 0.0
        cost_usd = 0.0

        for lvl in available_levels:
            if remaining <= 0:
                break

            if not self._buy_price_acceptable(lvl.price, intent.price):
                break

            fill_at_level = min(remaining, lvl.size)
            filled_contracts += fill_at_level
            cost_usd += fill_at_level * lvl.price
            remaining -= fill_at_level

        if filled_contracts <= 0:
            return FillResult(
                order_id=order_id, status="REJECTED",
                reason="No fillable levels within price limit",
            )

        fill_ratio = filled_contracts / intent.contracts
        if fill_ratio < self._cfg.min_fill_ratio and self._cfg.partial_fill_enabled:
            return FillResult(
                order_id=order_id, status="REJECTED",
                unfilled_contracts=intent.contracts,
                reason=f"Fill ratio {fill_ratio:.1%} below minimum {self._cfg.min_fill_ratio:.0%}",
            )

        avg_price = cost_usd / filled_contracts
        slippage = avg_price - intent.price
        filled_usd = filled_contracts * avg_price

        potential_profit_per_contract = max(0, 1.0 - avg_price)
        fees = filled_contracts * potential_profit_per_contract * self._cfg.fee_rate

        is_partial = remaining > 0
        status = "PARTIAL" if (is_partial and self._cfg.partial_fill_enabled) else "PAPER_FILL"

        elapsed_ms = (time.monotonic() - t0) * 1000

        logger.debug(
            f"Paper BUY: {intent.side} {filled_contracts:.2f}/{intent.contracts:.2f} "
            f"@ {avg_price:.4f} (slip={slippage:+.4f}, fee=${fees:.3f})"
        )

        return FillResult(
            order_id=order_id,
            status=status,
            filled_contracts=filled_contracts,
            avg_fill_price=avg_price,
            filled_usd=filled_usd,
            unfilled_contracts=max(0, remaining),
            fees_usd=fees,
            slippage=slippage,
            latency_ms=elapsed_ms,
        )

    async def _execute_sell(self, intent: TradeIntent, orderbook: Orderbook) -> FillResult:
        """Close long: hit bids on the outcome token's book."""
        t0 = time.monotonic()
        self._order_counter += 1
        order_id = f"paper_{int(time.time() * 1000)}_{self._order_counter}"

        levels = orderbook.bids
        if not levels:
            return FillResult(
                order_id=order_id, status="REJECTED",
                reason="No bids to sell into",
            )

        available_levels = levels[self._cfg.latency_levels:]
        if not available_levels:
            available_levels = levels

        remaining = intent.contracts
        filled_contracts = 0.0
        proceeds_usd = 0.0
        ref_bid = orderbook.best_bid

        for lvl in available_levels:
            if remaining <= 0:
                break
            if not self._sell_price_acceptable(lvl.price, intent.price):
                break
            fill_at = min(remaining, lvl.size)
            filled_contracts += fill_at
            proceeds_usd += fill_at * lvl.price
            remaining -= fill_at

        if filled_contracts <= 0:
            return FillResult(
                order_id=order_id, status="REJECTED",
                reason="No fillable bids at or above limit price",
            )

        fill_ratio = filled_contracts / intent.contracts
        min_ratio = self._cfg.min_exit_fill_ratio
        if fill_ratio < min_ratio and self._cfg.partial_fill_enabled:
            return FillResult(
                order_id=order_id, status="REJECTED",
                unfilled_contracts=intent.contracts,
                reason=f"Exit fill ratio {fill_ratio:.1%} below minimum {min_ratio:.0%}",
            )

        avg_price = proceeds_usd / filled_contracts
        slippage = avg_price - ref_bid if ref_bid > 0 else 0.0

        entry = intent.entry_price
        if entry is not None:
            profit_per = max(0.0, avg_price - entry)
            fees = filled_contracts * profit_per * self._cfg.fee_rate
        else:
            fees = 0.0

        is_partial = remaining > 0
        status = "PARTIAL" if (is_partial and self._cfg.partial_fill_enabled) else "PAPER_FILL"

        elapsed_ms = (time.monotonic() - t0) * 1000

        logger.debug(
            f"Paper SELL: {intent.side} {filled_contracts:.2f}/{intent.contracts:.2f} "
            f"@ {avg_price:.4f} (fee=${fees:.3f})"
        )

        return FillResult(
            order_id=order_id,
            status=status,
            filled_contracts=filled_contracts,
            avg_fill_price=avg_price,
            filled_usd=proceeds_usd,
            unfilled_contracts=max(0, remaining),
            fees_usd=fees,
            slippage=slippage,
            latency_ms=elapsed_ms,
        )

    async def cancel(self, order_id: str) -> bool:
        logger.debug(f"[PAPER] Cancel {order_id}")
        return True

    async def cancel_all(self) -> int:
        logger.debug("[PAPER] Cancel all")
        return 0

    @staticmethod
    def _buy_levels(ob: Orderbook, side: str) -> list[OrderbookLevel]:
        """Native token book: always walk asks to buy outcome tokens."""
        return ob.asks

    @staticmethod
    def _buy_price_acceptable(level_price: float, limit_price: float) -> bool:
        return level_price <= limit_price + 0.005

    @staticmethod
    def _sell_price_acceptable(bid_price: float, floor_price: float) -> bool:
        return bid_price >= floor_price - 0.005
