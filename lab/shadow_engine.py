from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from exchange_client.base import Orderbook


@dataclass
class ShadowOrderState:
    order_id: str
    portfolio_key: str
    market_id: str
    market_db_id: int
    token_id: str
    event_id: str | None
    side: Literal["YES", "NO"]
    action: Literal["BUY", "SELL"]
    price: float
    size_total: float
    size_remaining: float
    queue_ahead: float
    visible_size_same_side: float
    created_at: datetime
    expires_at: datetime
    reprices: int = 0
    last_repriced_at: datetime | None = None
    hypothesis: str = ""
    edge: float = 0.0
    order_kind: str = "maker"
    forced_exit: bool = False
    force_taker_allowed: bool = False
    status: str = "working"
    closed_at: datetime | None = None
    filled_size: float = 0.0
    filled_notional: float = 0.0
    reason: str = ""

    @property
    def avg_fill_price(self) -> float:
        return self.filled_notional / self.filled_size if self.filled_size > 0 else 0.0

    @property
    def quote_age_sec(self) -> float:
        reference = self.last_repriced_at or self.created_at
        return max(0.0, (datetime.now(reference.tzinfo) - reference).total_seconds())


@dataclass
class ShadowFill:
    filled_size: float
    price: float
    notional: float
    fill_type: str = "partial"
    fee_usdc: float = 0.0
    slippage_usdc: float = 0.0
    effective_fill_price: float | None = None
    fee_rate_bps: float = 0.0
    latency_ms: float = 0.0


@dataclass
class TakerFillPreview:
    filled_size: float
    avg_price: float
    notional: float
    slippage_per_share: float
    slippage_usdc: float
    full_size: bool
    latency_penalty_per_share: float = 0.0
    latency_penalty_usdc: float = 0.0


@dataclass
class RepriceDecision:
    should_reprice: bool
    expired: bool = False
    forced_taker_exit: bool = False


class ShadowMakerEngine:
    def __init__(
        self,
        ttl_sec: int,
        reprice_sec: int,
        max_reprices: int,
        tick_default: float = 0.01,
        latency_penalty_bps: float = 0.0,
        max_event_age_ms: int = 0,
    ):
        self._ttl_sec = ttl_sec
        self._reprice_sec = reprice_sec
        self._max_reprices = max_reprices
        self._tick_default = tick_default
        self._latency_penalty_bps = max(0.0, float(latency_penalty_bps))
        self._max_event_age_ms = max(0, int(max_event_age_ms))
        self._counter = 0

    def _latency_penalty_per_share(self, action_side: Literal["BUY", "SELL"], reference_price: float, event_age_ms: float | None) -> float:
        if event_age_ms is None or self._max_event_age_ms <= 0 or self._latency_penalty_bps <= 0:
            return 0.0
        age_ms = max(0.0, float(event_age_ms))
        if age_ms <= self._max_event_age_ms:
            return 0.0
        return max(0.0, float(reference_price)) * (self._latency_penalty_bps / 10000.0)

    def next_order_id(self, portfolio_key: str) -> str:
        self._counter += 1
        return f"lab_{portfolio_key}_{self._counter}"

    def quote_entry_price(
        self,
        orderbook: Orderbook,
        action_side: Literal["BUY", "SELL"],
        tick_size: float | None = None,
    ) -> float:
        tick = tick_size or self._tick_default
        if action_side == "BUY":
            improved = orderbook.best_bid + tick
            price = improved if improved < orderbook.best_ask else orderbook.best_bid
            return round(price, 8)
        improved = orderbook.best_ask - tick
        price = improved if improved > orderbook.best_bid else orderbook.best_ask
        return round(price, 8)

    def visible_same_side_size(
        self,
        orderbook: Orderbook,
        action_side: Literal["BUY", "SELL"],
        price: float,
    ) -> float:
        levels = orderbook.bids if action_side == "BUY" else orderbook.asks
        for level in levels:
            if abs(level.price - price) < 1e-9:
                return level.size
        return 0.0

    def create_order(
        self,
        *,
        portfolio_key: str,
        market_id: str,
        market_db_id: int,
        token_id: str,
        event_id: str | None,
        side: Literal["YES", "NO"],
        action: Literal["BUY", "SELL"],
        price: float,
        size: float,
        queue_ahead: float,
        hypothesis: str,
        edge: float,
        now: datetime,
        forced_exit: bool = False,
        force_taker_allowed: bool = False,
        order_kind: str = "maker",
    ) -> ShadowOrderState:
        return ShadowOrderState(
            order_id=self.next_order_id(portfolio_key),
            portfolio_key=portfolio_key,
            market_id=market_id,
            market_db_id=market_db_id,
            token_id=token_id,
            event_id=event_id,
            side=side,
            action=action,
            price=price,
            size_total=size,
            size_remaining=size,
            queue_ahead=queue_ahead,
            visible_size_same_side=queue_ahead,
            created_at=now,
            expires_at=now + timedelta(seconds=self._ttl_sec),
            hypothesis=hypothesis,
            edge=edge,
            forced_exit=forced_exit,
            force_taker_allowed=force_taker_allowed,
            order_kind=order_kind,
        )

    def observe_book(self, order: ShadowOrderState, orderbook: Orderbook) -> float:
        if order.status != "working":
            return 0.0
        current_visible = self.visible_same_side_size(orderbook, order.action, order.price)
        if current_visible < order.visible_size_same_side:
            depleted = order.visible_size_same_side - current_visible
            order.queue_ahead = max(0.0, order.queue_ahead - depleted)
            order.visible_size_same_side = current_visible
            return depleted
        return 0.0

    def process_trade(
        self,
        order: ShadowOrderState,
        *,
        trade_price: float,
        trade_size: float,
        aggressor_side: str,
    ) -> ShadowFill | None:
        if order.status not in {"working", "partial"} or trade_size <= 0:
            return None

        aggressor = aggressor_side.upper()
        eligible = False
        if order.action == "BUY" and aggressor in {"SELL", "ASK"} and trade_price <= order.price + 1e-9:
            eligible = True
        elif order.action == "SELL" and aggressor in {"BUY", "BID"} and trade_price >= order.price - 1e-9:
            eligible = True

        if not eligible:
            return None

        available = trade_size
        if order.queue_ahead > 0:
            consumed = min(order.queue_ahead, available)
            order.queue_ahead -= consumed
            available -= consumed

        if available <= 0:
            return None

        fill_size = min(order.size_remaining, available)
        order.size_remaining -= fill_size
        order.filled_size += fill_size
        order.filled_notional += fill_size * order.price
        if order.size_remaining <= 1e-9:
            order.size_remaining = 0.0
            order.status = "filled"
        else:
            order.status = "partial"
        fill_type = "full" if order.size_remaining == 0 else "partial"
        if order.forced_exit:
            fill_type = "forced_taker_exit"
        return ShadowFill(
            filled_size=fill_size,
            price=order.price,
            notional=fill_size * order.price,
            fill_type=fill_type,
        )

    def cancel(self, order: ShadowOrderState, *, reason: str, now: datetime):
        if order.status in {"filled", "cancelled", "expired", "rejected"}:
            return
        order.status = "expired" if reason == "expired" else "cancelled"
        order.reason = reason
        order.closed_at = now

    def reprice_decision(self, order: ShadowOrderState, *, now: datetime, is_exit: bool) -> RepriceDecision:
        if order.status not in {"working", "partial"}:
            return RepriceDecision(False, expired=False)

        reference_ts = order.last_repriced_at or order.created_at
        if now < reference_ts + timedelta(seconds=self._reprice_sec):
            return RepriceDecision(False, expired=False)

        exhausted = order.reprices >= self._max_reprices
        if exhausted:
            if now < order.expires_at:
                return RepriceDecision(False, expired=False)
            return RepriceDecision(
                should_reprice=False,
                expired=True,
                forced_taker_exit=is_exit,
            )
        return RepriceDecision(should_reprice=True, expired=now >= order.expires_at)

    def apply_reprice(
        self,
        order: ShadowOrderState,
        *,
        new_price: float,
        new_queue_ahead: float,
        now: datetime,
        order_kind: str = "maker",
    ):
        order.price = new_price
        order.queue_ahead = new_queue_ahead
        order.visible_size_same_side = new_queue_ahead
        order.expires_at = now + timedelta(seconds=self._ttl_sec)
        order.last_repriced_at = now
        order.reprices += 1
        order.order_kind = order_kind
        order.status = "working"

    def force_taker_fill(
        self,
        order: ShadowOrderState,
        orderbook: Orderbook,
        *,
        event_age_ms: float | None = None,
    ) -> ShadowFill | None:
        preview = self.simulate_taker_fill_size(
            orderbook,
            order.action,
            order.size_remaining,
            event_age_ms=event_age_ms,
        )
        if preview is None:
            return None

        order.size_remaining -= preview.filled_size
        order.filled_size += preview.filled_size
        order.filled_notional += preview.notional
        order.order_kind = "forced_taker"
        order.forced_exit = True
        order.status = "filled" if order.size_remaining <= 1e-9 else "partial"
        return ShadowFill(
            filled_size=preview.filled_size,
            price=preview.avg_price,
            notional=preview.notional,
            fill_type="forced_taker_exit",
            slippage_usdc=preview.slippage_usdc,
            effective_fill_price=preview.avg_price,
            latency_ms=max(0.0, float(event_age_ms or 0.0)),
        )

    def simulate_taker_fill_size(
        self,
        orderbook: Orderbook,
        action_side: Literal["BUY", "SELL"],
        size: float,
        *,
        event_age_ms: float | None = None,
    ) -> TakerFillPreview | None:
        levels = orderbook.asks if action_side == "BUY" else orderbook.bids
        remaining = max(0.0, float(size))
        if remaining <= 0 or not levels:
            return None
        notional = 0.0
        filled = 0.0
        for level in levels:
            take = min(remaining, level.size)
            if take <= 0:
                continue
            filled += take
            notional += take * level.price
            remaining -= take
            if remaining <= 1e-9:
                break
        if filled <= 0:
            return None
        book_avg_price = notional / filled
        reference = orderbook.best_ask if action_side == "BUY" else orderbook.best_bid
        latency_penalty_per_share = self._latency_penalty_per_share(action_side, reference, event_age_ms)
        avg_price = (
            book_avg_price + latency_penalty_per_share
            if action_side == "BUY"
            else max(0.0, book_avg_price - latency_penalty_per_share)
        )
        slippage_per_share = max(0.0, avg_price - reference) if action_side == "BUY" else max(0.0, reference - avg_price)
        return TakerFillPreview(
            filled_size=filled,
            avg_price=avg_price,
            notional=avg_price * filled,
            slippage_per_share=slippage_per_share,
            slippage_usdc=slippage_per_share * filled,
            full_size=remaining <= 1e-9,
            latency_penalty_per_share=latency_penalty_per_share,
            latency_penalty_usdc=latency_penalty_per_share * filled,
        )

    def simulate_taker_fill_notional(
        self,
        orderbook: Orderbook,
        action_side: Literal["BUY", "SELL"],
        max_notional: float,
        *,
        event_age_ms: float | None = None,
    ) -> TakerFillPreview | None:
        levels = orderbook.asks if action_side == "BUY" else orderbook.bids
        remaining_notional = max(0.0, float(max_notional))
        if remaining_notional <= 0 or not levels:
            return None
        notional = 0.0
        filled = 0.0
        for level in levels:
            if level.price <= 0:
                continue
            take = min(level.size, remaining_notional / level.price)
            if take <= 0:
                continue
            spent = take * level.price
            filled += take
            notional += spent
            remaining_notional -= spent
            if remaining_notional <= 1e-9:
                break
        if filled <= 0:
            return None
        book_avg_price = notional / filled
        reference = orderbook.best_ask if action_side == "BUY" else orderbook.best_bid
        latency_penalty_per_share = self._latency_penalty_per_share(action_side, reference, event_age_ms)
        avg_price = (
            book_avg_price + latency_penalty_per_share
            if action_side == "BUY"
            else max(0.0, book_avg_price - latency_penalty_per_share)
        )
        slippage_per_share = max(0.0, avg_price - reference) if action_side == "BUY" else max(0.0, reference - avg_price)
        return TakerFillPreview(
            filled_size=filled,
            avg_price=avg_price,
            notional=avg_price * filled,
            slippage_per_share=slippage_per_share,
            slippage_usdc=slippage_per_share * filled,
            full_size=remaining_notional <= 1e-9,
            latency_penalty_per_share=latency_penalty_per_share,
            latency_penalty_usdc=latency_penalty_per_share * filled,
        )
