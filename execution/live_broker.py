"""
Live execution broker — sends real orders to Polymarket CLOB.

Wraps PolymarketClient.place_order() behind the ExecutionBroker interface.
Only used when mode == "live". Feature-flagged and disabled by default.
"""

from __future__ import annotations

import time

from loguru import logger

from exchange_client.base import Orderbook
from exchange_client.polymarket import PolymarketClient
from .broker import ExecutionBroker, TradeIntent, FillResult


POLYMARKET_MIN_SIZE = 5.0  # CLOB rejects orders < $5 notional


class LiveBroker(ExecutionBroker):
    def __init__(self, client: PolymarketClient, fee_rate: float = 0.02):
        self._client = client
        self._fee_rate = fee_rate

    @property
    def mode(self) -> str:
        return "live"

    async def execute(self, intent: TradeIntent, orderbook: Orderbook) -> FillResult:
        if intent.action == "SELL":
            return await self._execute_sell(intent, orderbook)
        return await self._execute_buy(intent, orderbook)

    async def _execute_buy(self, intent: TradeIntent, orderbook: Orderbook) -> FillResult:
        t0 = time.monotonic()
        order_id = f"live_{int(time.time() * 1000)}"

        if intent.stake_usd < POLYMARKET_MIN_SIZE:
            return FillResult(
                order_id=order_id, status="REJECTED",
                reason=f"Stake ${intent.stake_usd:.2f} below Polymarket minimum ${POLYMARKET_MIN_SIZE}",
            )

        if not intent.token_id:
            return FillResult(
                order_id=order_id, status="REJECTED",
                reason="Missing token_id for live order",
            )

        logger.info(
            f"LIVE ORDER: {intent.side} {intent.contracts:.2f} contracts "
            f"of {intent.token_id[:12]}... @ {intent.price:.4f}"
        )

        try:
            result = await self._client.place_order(
                token_id=intent.token_id,
                side="BUY",
                price=intent.price,
                size=intent.contracts,
            )
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.error(f"Live order failed: {e}")
            return FillResult(
                order_id=order_id, status="REJECTED",
                reason=str(e), latency_ms=elapsed_ms,
            )

        elapsed_ms = (time.monotonic() - t0) * 1000
        filled = result.filled_size
        avg_price = result.avg_fill_price or intent.price
        slippage = avg_price - intent.price if filled > 0 else 0.0

        status = result.status.upper()
        if status in ("MATCHED", "FILLED"):
            status = "FILLED"
        elif filled > 0 and filled < intent.contracts:
            status = "PARTIAL"
        elif status == "LIVE":
            status = "POSTED"  # limit order on the book, not yet filled

        return FillResult(
            order_id=result.order_id or order_id,
            status=status,
            filled_contracts=filled,
            avg_fill_price=avg_price,
            filled_usd=filled * avg_price,
            unfilled_contracts=max(0, intent.contracts - filled),
            slippage=slippage,
            latency_ms=elapsed_ms,
            raw_response={"original_status": result.status},
        )

    async def _execute_sell(self, intent: TradeIntent, orderbook: Orderbook) -> FillResult:
        """Sell outcome tokens (close long)."""
        t0 = time.monotonic()
        order_id = f"live_{int(time.time() * 1000)}"

        if not intent.token_id:
            return FillResult(
                order_id=order_id, status="REJECTED",
                reason="Missing token_id for live sell",
            )

        ref_px = max(
            orderbook.best_bid,
            orderbook.best_ask * 0.5 if orderbook.best_ask > 0 else 0.0,
            intent.price,
            0.01,
        )
        est_notional = intent.contracts * ref_px
        if est_notional < POLYMARKET_MIN_SIZE:
            return FillResult(
                order_id=order_id, status="REJECTED",
                reason=f"Sell notional ${est_notional:.2f} below minimum ${POLYMARKET_MIN_SIZE}",
            )

        logger.info(
            f"LIVE SELL: {intent.contracts:.2f} contracts of {intent.token_id[:12]}... "
            f"@ min {intent.price:.4f}"
        )

        try:
            result = await self._client.place_order(
                token_id=intent.token_id,
                side="SELL",
                price=max(intent.price, 0.01),
                size=intent.contracts,
            )
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.error(f"Live sell failed: {e}")
            return FillResult(
                order_id=order_id, status="REJECTED",
                reason=str(e), latency_ms=elapsed_ms,
            )

        elapsed_ms = (time.monotonic() - t0) * 1000
        filled = result.filled_size
        avg_price = result.avg_fill_price or intent.price
        slippage = avg_price - orderbook.best_bid if orderbook.best_bid > 0 else 0.0

        status = result.status.upper()
        if status in ("MATCHED", "FILLED"):
            status = "FILLED"
        elif filled > 0 and filled < intent.contracts:
            status = "PARTIAL"
        elif status == "LIVE":
            status = "POSTED"

        profit_per = (
            max(0.0, avg_price - intent.entry_price) * filled
            if intent.entry_price is not None
            else 0.0
        )
        fees_usd = profit_per * self._fee_rate

        return FillResult(
            order_id=result.order_id or order_id,
            status=status,
            filled_contracts=filled,
            avg_fill_price=avg_price,
            filled_usd=filled * avg_price,
            unfilled_contracts=max(0, intent.contracts - filled),
            slippage=slippage,
            latency_ms=elapsed_ms,
            fees_usd=fees_usd,
            raw_response={"original_status": result.status},
        )

    async def cancel(self, order_id: str) -> bool:
        return await self._client.cancel_order(order_id)

    async def cancel_all(self) -> int:
        return await self._client.cancel_all_orders()


class DryRunBroker(ExecutionBroker):
    """
    No exchange calls. BUY simulates a full fill (status PAPER_FILL) so the orchestrator
    can record positions like paper mode — required for dry_run end-to-end checks.
    SELL uses best bid touch like before.
    """

    @property
    def mode(self) -> str:
        return "dry_run"

    async def execute(self, intent: TradeIntent, orderbook: Orderbook) -> FillResult:
        if intent.action == "SELL":
            bid = orderbook.best_bid
            if bid <= 0:
                return FillResult(
                    order_id=f"dry_{int(time.time() * 1000)}",
                    status="REJECTED",
                    reason="No bid for dry-run exit",
                )
            entry = intent.entry_price or 0.0
            profit_per = max(0.0, bid - entry)
            fees = intent.contracts * profit_per * 0.02
            logger.info(
                f"[DRY RUN] SELL {intent.contracts:.2f} {intent.side} @ ~{bid:.4f} (touch)"
            )
            return FillResult(
                order_id=f"dry_{int(time.time() * 1000)}",
                status="PAPER_FILL",
                filled_contracts=intent.contracts,
                avg_fill_price=bid,
                filled_usd=intent.contracts * bid,
                fees_usd=fees,
                slippage=0.0,
            )

        ask = orderbook.best_ask
        if ask <= 0:
            return FillResult(
                order_id=f"dry_{int(time.time() * 1000)}",
                status="REJECTED",
                reason="No ask for dry-run BUY",
            )
        fill_price = min(ask, intent.price) if intent.price > 0 else ask
        filled = intent.contracts
        filled_usd = filled * fill_price
        potential_profit = max(0.0, 1.0 - fill_price)
        fees = filled * potential_profit * 0.02
        slip = fill_price - intent.price
        oid = f"dry_{int(time.time() * 1000)}"
        logger.info(
            f"[DRY RUN] BUY {intent.side} {filled:.2f} @ {fill_price:.4f} "
            f"(simulated fill, no chain tx) market={intent.market_id[:24]}..."
        )
        return FillResult(
            order_id=oid,
            status="PAPER_FILL",
            filled_contracts=filled,
            avg_fill_price=fill_price,
            filled_usd=filled_usd,
            fees_usd=fees,
            slippage=slip,
        )

    async def cancel(self, order_id: str) -> bool:
        return True

    async def cancel_all(self) -> int:
        return 0
