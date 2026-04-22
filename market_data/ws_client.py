"""
WebSocket client for Polymarket CLOB real-time data.

Supported market-channel events:
- book
- price_change
- best_bid_ask
- last_trade_price
- tick_size_change

Features:
- Adaptive keepalive (adjusts interval based on connection quality)
- OrderbookBuffer (bridges short disconnections with cached state)
- Exponential reconnect with jitter
- ws_quality_metrics (message rate, gap detection, staleness tracking)
- Telegram-ready quality alerts via on_quality_alert callback
"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

from .orderbook_manager import OrderbookManager


# ──────────────────── Quality Metrics ────────────────────


@dataclass
class WSQualityMetrics:
    """Tracks WebSocket connection health in real-time."""
    # Sliding window for message rate calculation
    _message_timestamps: deque = field(default_factory=lambda: deque(maxlen=600))
    # Gap detection
    _last_message_ts: float = 0.0
    _gap_count: int = 0          # gaps > gap_threshold_sec
    _max_gap_sec: float = 0.0
    _total_gaps_sec: float = 0.0
    # Connection stats
    connect_count: int = 0
    disconnect_count: int = 0
    error_count: int = 0
    message_count: int = 0
    pong_count: int = 0
    # Staleness
    _stale_alerts_sent: int = 0
    # Config
    gap_threshold_sec: float = 5.0
    stale_threshold_sec: float = 30.0

    def record_message(self):
        now = time.time()
        self._message_timestamps.append(now)
        self.message_count += 1

        if self._last_message_ts > 0:
            gap = now - self._last_message_ts
            if gap > self.gap_threshold_sec:
                self._gap_count += 1
                self._total_gaps_sec += gap
                if gap > self._max_gap_sec:
                    self._max_gap_sec = gap
        self._last_message_ts = now

    def record_pong(self):
        self.pong_count += 1
        self._last_message_ts = time.time()

    def record_connect(self):
        self.connect_count += 1

    def record_disconnect(self):
        self.disconnect_count += 1

    def record_error(self):
        self.error_count += 1

    @property
    def messages_per_minute(self) -> float:
        if len(self._message_timestamps) < 2:
            return 0.0
        window = self._message_timestamps[-1] - self._message_timestamps[0]
        if window < 1.0:
            return 0.0
        return (len(self._message_timestamps) - 1) / window * 60.0

    @property
    def last_message_age_sec(self) -> float:
        if self._last_message_ts <= 0:
            return -1.0
        return time.time() - self._last_message_ts

    @property
    def is_stale(self) -> bool:
        return self.last_message_age_sec > self.stale_threshold_sec

    @property
    def health_score(self) -> float:
        """0.0 (dead) to 1.0 (perfect). Used for adaptive keepalive."""
        score = 1.0
        age = self.last_message_age_sec
        if age < 0:
            return 0.0
        # Penalize staleness
        if age > self.stale_threshold_sec:
            score -= 0.5
        elif age > self.gap_threshold_sec:
            score -= 0.2
        # Penalize low message rate
        rate = self.messages_per_minute
        if rate < 1.0:
            score -= 0.3
        elif rate < 5.0:
            score -= 0.1
        # Penalize frequent disconnections
        if self.disconnect_count > 5:
            score -= 0.2
        return max(0.0, min(1.0, score))

    def snapshot(self) -> dict:
        return {
            "connected": self.connect_count > self.disconnect_count,
            "message_count": self.message_count,
            "messages_per_minute": round(self.messages_per_minute, 1),
            "last_message_age_sec": round(self.last_message_age_sec, 1),
            "gap_count": self._gap_count,
            "max_gap_sec": round(self._max_gap_sec, 1),
            "total_gaps_sec": round(self._total_gaps_sec, 1),
            "connect_count": self.connect_count,
            "disconnect_count": self.disconnect_count,
            "error_count": self.error_count,
            "health_score": round(self.health_score, 2),
            "is_stale": self.is_stale,
        }

    def reset(self):
        self._message_timestamps.clear()
        self._last_message_ts = 0.0
        self._gap_count = 0
        self._max_gap_sec = 0.0
        self._total_gaps_sec = 0.0
        self.connect_count = 0
        self.disconnect_count = 0
        self.error_count = 0
        self.message_count = 0
        self.pong_count = 0
        self._stale_alerts_sent = 0


# ──────────────────── Orderbook Buffer ────────────────────


class OrderbookBuffer:
    """
    Caches recent orderbook snapshots per token to bridge short WS disconnections.
    When WS reconnects, stale books are marked but still available as fallback
    until a fresh snapshot arrives.
    """

    def __init__(self, max_age_sec: float = 120.0):
        self._max_age_sec = max_age_sec
        self._snapshots: dict[str, tuple[float, Any]] = {}  # token_id -> (timestamp, Orderbook)

    def store(self, token_id: str, orderbook: Any):
        self._snapshots[token_id] = (time.time(), orderbook)

    def get(self, token_id: str) -> Any | None:
        entry = self._snapshots.get(token_id)
        if entry is None:
            return None
        ts, ob = entry
        if time.time() - ts > self._max_age_sec:
            return None
        return ob

    def age_sec(self, token_id: str) -> float:
        entry = self._snapshots.get(token_id)
        if entry is None:
            return float("inf")
        return time.time() - entry[0]

    def invalidate(self, token_id: str):
        self._snapshots.pop(token_id, None)

    def clear(self):
        self._snapshots.clear()


# ──────────────────── WebSocket Client ────────────────────


class PolymarketWebSocket:
    MAX_RECONNECT_DELAY = 60
    INITIAL_RECONNECT_DELAY = 1

    # Adaptive keepalive bounds
    HEARTBEAT_MIN_SEC = 5
    HEARTBEAT_MAX_SEC = 25
    HEARTBEAT_DEFAULT_SEC = 10

    # Stale detection
    STALE_ALERT_INTERVAL_SEC = 60  # Min time between stale alerts

    def __init__(
        self,
        ws_url: str,
        ob_manager: OrderbookManager,
        on_error: Callable[[str], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
        on_quality_alert: Callable[[str, dict], Any] | None = None,
    ):
        self._ws_url = ws_url
        self._ob_manager = ob_manager
        self._on_error = on_error
        self._on_disconnect = on_disconnect
        self._on_event = on_event
        self._on_quality_alert = on_quality_alert
        self._ws = None
        self._subscribed_tokens: set[str] = set()
        self._running = False
        self._task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._stale_monitor_task: asyncio.Task | None = None
        self._reconnect_delay = self.INITIAL_RECONNECT_DELAY

        # Quality tracking
        self.quality = WSQualityMetrics()
        self._ob_buffer = OrderbookBuffer(max_age_sec=120.0)

        # Adaptive heartbeat state
        self._heartbeat_interval = self.HEARTBEAT_DEFAULT_SEC
        self._last_stale_alert_ts: float = 0.0

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._running

    @property
    def stats(self) -> dict:
        base = self.quality.snapshot()
        base["subscribed_tokens"] = len(self._subscribed_tokens)
        base["heartbeat_interval"] = self._heartbeat_interval
        return base

    async def connect(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._connection_loop())
        logger.info(f"WebSocket client starting: {self._ws_url}")

    async def close(self):
        self._running = False
        await self._stop_heartbeat()
        await self._stop_stale_monitor()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("WebSocket client closed")

    async def subscribe(self, token_ids: list[str]):
        token_ids = [tid for tid in token_ids if tid]
        if not token_ids:
            return
        new_tokens = set(token_ids) - self._subscribed_tokens
        if not new_tokens and self._running:
            return

        self._subscribed_tokens.update(token_ids)
        if not self._running:
            await self.connect()

        if self._ws:
            try:
                await self._send_market_subscription()
                logger.debug(f"WS subscribed to {len(self._subscribed_tokens)} token(s)")
            except Exception as e:
                logger.warning(f"WS subscribe failed: {e}")

    async def unsubscribe(self, token_ids: list[str]):
        for token_id in token_ids:
            self._subscribed_tokens.discard(token_id)
            self._ob_manager.remove(token_id)
            self._ob_buffer.invalidate(token_id)
        if self._ws and self._subscribed_tokens:
            try:
                await self._send_market_subscription(operation="unsubscribe")
            except Exception:
                pass

    async def _send_market_subscription(self, operation: str | None = None):
        if not self._ws or not self._subscribed_tokens:
            return
        payload: dict[str, Any] = {
            "type": "market",
            "assets_ids": sorted(self._subscribed_tokens),
            "custom_feature_enabled": True,
        }
        if operation:
            payload["operation"] = operation
        await self._ws.send(json.dumps(payload))

    # ──────────────── Adaptive Heartbeat ────────────────

    def _compute_heartbeat_interval(self) -> float:
        """Adjust keepalive interval based on connection health score."""
        score = self.quality.health_score
        # Low health → more frequent pings to detect problems faster
        # High health → less frequent to reduce overhead
        if score >= 0.8:
            return self.HEARTBEAT_MAX_SEC
        elif score >= 0.5:
            return self.HEARTBEAT_DEFAULT_SEC
        else:
            return self.HEARTBEAT_MIN_SEC

    async def _heartbeat_loop(self):
        while self._running:
            self._heartbeat_interval = self._compute_heartbeat_interval()
            await asyncio.sleep(self._heartbeat_interval)
            if not self._ws:
                continue
            try:
                await self._ws.send("PING")
            except Exception as e:
                logger.debug(f"WS heartbeat failed: {e}")
                return

    async def _stop_heartbeat(self):
        if not self._heartbeat_task:
            return
        self._heartbeat_task.cancel()
        try:
            await self._heartbeat_task
        except asyncio.CancelledError:
            pass
        self._heartbeat_task = None

    # ──────────────── Stale Monitor ────────────────

    async def _stale_monitor_loop(self):
        """Periodically check for staleness and emit quality alerts."""
        while self._running:
            await asyncio.sleep(10)
            if self.quality.is_stale and self._ws is not None:
                now = time.time()
                if now - self._last_stale_alert_ts >= self.STALE_ALERT_INTERVAL_SEC:
                    self._last_stale_alert_ts = now
                    age = self.quality.last_message_age_sec
                    logger.warning(
                        f"WS stale: no message for {age:.0f}s "
                        f"(health={self.quality.health_score:.2f})"
                    )
                    await self._emit_quality_alert(
                        "stale",
                        {
                            "age_sec": round(age, 1),
                            "health_score": self.quality.health_score,
                            "subscribed_tokens": len(self._subscribed_tokens),
                        },
                    )

    async def _stop_stale_monitor(self):
        if not self._stale_monitor_task:
            return
        self._stale_monitor_task.cancel()
        try:
            await self._stale_monitor_task
        except asyncio.CancelledError:
            pass
        self._stale_monitor_task = None

    async def _emit_quality_alert(self, alert_type: str, details: dict):
        if not self._on_quality_alert:
            return
        try:
            result = self._on_quality_alert(alert_type, details)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.debug(f"WS quality alert callback failed: {e}")

    # ──────────────── Connection Loop ────────────────

    async def _connection_loop(self):
        try:
            import websockets
        except ImportError:
            logger.error(
                "websockets package not installed. "
                "WebSocket data feed disabled. Install: pip install websockets"
            )
            self._running = False
            return

        while self._running:
            try:
                async with websockets.connect(
                    self._ws_url,
                    # Polymarket market channel expects text PING heartbeats.
                    # Disable the library ping to avoid duplicate keepalives
                    # causing false 1011 ping-timeout reconnects under load.
                    ping_interval=None,
                    ping_timeout=None,
                    open_timeout=30,
                    close_timeout=10,
                ) as ws:
                    self._ws = ws
                    self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
                    self.quality.record_connect()
                    logger.info(
                        f"WebSocket connected "
                        f"(attempt #{self.quality.connect_count}, "
                        f"health={self.quality.health_score:.2f})"
                    )

                    await self._stop_heartbeat()
                    self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                    if not self._stale_monitor_task or self._stale_monitor_task.done():
                        self._stale_monitor_task = asyncio.create_task(
                            self._stale_monitor_loop()
                        )

                    if self._subscribed_tokens:
                        await self._send_market_subscription()

                    async for raw_msg in ws:
                        if not self._running:
                            break
                        await self._process_message(raw_msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.quality.record_error()
                self.quality.record_disconnect()
                self._ws = None
                await self._stop_heartbeat()

                # Invalidate orderbooks but keep buffer for fallback
                self._ob_manager.invalidate_all()

                if self._on_disconnect:
                    try:
                        self._on_disconnect()
                    except Exception as cb_err:
                        logger.debug(f"on_disconnect callback: {cb_err}")

                logger.warning(
                    f"WebSocket disconnected: {e}. "
                    f"Reconnecting in {self._reconnect_delay}s... "
                    f"(errors={self.quality.error_count}, "
                    f"health={self.quality.health_score:.2f})"
                )
                if self._on_error:
                    self._on_error(str(e))

                await self._emit_quality_alert(
                    "disconnect",
                    {
                        "error": str(e),
                        "reconnect_delay": self._reconnect_delay,
                        "error_count": self.quality.error_count,
                        "disconnect_count": self.quality.disconnect_count,
                    },
                )

                jitter = random.uniform(0, min(2.0, self._reconnect_delay * 0.3))
                await asyncio.sleep(self._reconnect_delay + jitter)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    self.MAX_RECONNECT_DELAY,
                )

        self._ws = None

    @staticmethod
    def _parse_price(value: Any) -> float | None:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _parse_levels(cls, levels: list[dict]) -> list:
        from exchange_client.base import OrderbookLevel

        parsed = []
        for level in levels:
            if not isinstance(level, dict):
                continue
            price = cls._parse_price(level.get("price", level.get("p")))
            size = cls._parse_price(level.get("size", level.get("s")))
            if price is None or size is None:
                continue
            parsed.append(OrderbookLevel(price, size))
        return parsed

    async def _emit_event(self, event: dict[str, Any]):
        if not self._on_event:
            return
        try:
            result = self._on_event(event)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.debug(f"WS on_event callback failed: {e}")

    async def _process_message(self, raw: str):
        if raw in {"PONG", "pong"}:
            self.quality.record_pong()
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug(f"WS non-JSON message: {raw[:100]}")
            return

        self.quality.record_message()

        items = msg if isinstance(msg, list) else [msg]
        for item in items:
            if not isinstance(item, dict):
                continue
            for event in self._normalize_events(item):
                await self._apply_event(event)

    def _normalize_events(self, msg: dict[str, Any]) -> list[dict[str, Any]]:
        channel = msg.get("channel", "")
        data = msg.get("data", {}) if isinstance(msg.get("data"), dict) else {}
        event_type = (
            msg.get("event_type")
            or data.get("event_type")
            or msg.get("type")
            or data.get("type")
            or channel
        )

        token_id = (
            msg.get("asset_id")
            or msg.get("market")
            or data.get("asset_id")
            or data.get("market")
            or ""
        )
        market_id = msg.get("market") or data.get("market")
        timestamp = msg.get("timestamp") or data.get("timestamp") or time.time()

        if event_type == "book":
            return [{
                "event_type": "book",
                "asset_id": token_id,
                "market": market_id,
                "timestamp": timestamp,
                "bids": msg.get("bids", data.get("bids", [])),
                "asks": msg.get("asks", data.get("asks", [])),
            }]

        if event_type == "price_change":
            changes = msg.get("price_changes") or msg.get("changes") or data.get("price_changes") or data.get("changes") or []
            events = []
            for change in changes:
                if not isinstance(change, dict):
                    continue
                events.append({
                    "event_type": "price_change",
                    "asset_id": change.get("asset_id") or token_id,
                    "market": change.get("market") or market_id,
                    "timestamp": change.get("timestamp") or timestamp,
                    "price": self._parse_price(change.get("price")),
                    "size": self._parse_price(change.get("size")),
                    "side": str(change.get("side", "")).upper(),
                    "best_bid": self._parse_price(change.get("best_bid", msg.get("best_bid"))),
                    "best_ask": self._parse_price(change.get("best_ask", msg.get("best_ask"))),
                })
            return events

        if event_type == "best_bid_ask":
            return [{
                "event_type": "best_bid_ask",
                "asset_id": token_id,
                "market": market_id,
                "timestamp": timestamp,
                "best_bid": self._parse_price(msg.get("best_bid", data.get("best_bid"))),
                "best_ask": self._parse_price(msg.get("best_ask", data.get("best_ask"))),
            }]

        if event_type == "last_trade_price":
            return [{
                "event_type": "last_trade_price",
                "asset_id": token_id,
                "market": market_id,
                "timestamp": timestamp,
                "price": self._parse_price(msg.get("price", data.get("price"))),
                "size": self._parse_price(msg.get("size", data.get("size"))),
                "side": str(msg.get("side", data.get("side", ""))).upper(),
            }]

        if event_type == "tick_size_change":
            return [{
                "event_type": "tick_size_change",
                "asset_id": token_id,
                "market": market_id,
                "timestamp": timestamp,
                "old_tick_size": self._parse_price(msg.get("old_tick_size", data.get("old_tick_size"))),
                "new_tick_size": self._parse_price(msg.get("new_tick_size", data.get("new_tick_size"))),
            }]

        # Legacy nested book payloads
        if channel == "book" and data:
            return [{
                "event_type": "book" if data.get("type") == "snapshot" else "book_delta",
                "asset_id": token_id,
                "market": market_id,
                "timestamp": timestamp,
                "bids": data.get("bids", []),
                "asks": data.get("asks", []),
            }]

        return []

    async def _apply_event(self, event: dict[str, Any]):
        token_id = event.get("asset_id") or ""
        event_type = event.get("event_type") or ""

        if event_type == "book":
            from exchange_client.base import Orderbook

            ob = Orderbook(
                market_id=token_id,
                bids=self._parse_levels(event.get("bids", [])),
                asks=self._parse_levels(event.get("asks", [])),
                timestamp=time.time(),
            )
            self._ob_manager.apply_snapshot(token_id, ob)
            # Store in buffer for disconnect resilience
            self._ob_buffer.store(token_id, ob)
        elif event_type == "book_delta":
            for bid in event.get("bids", []):
                price = self._parse_price(bid.get("price", bid.get("p")))
                size = self._parse_price(bid.get("size", bid.get("s")))
                if price is not None and size is not None:
                    self._ob_manager.apply_delta(token_id, "bid", price, size)
            for ask in event.get("asks", []):
                price = self._parse_price(ask.get("price", ask.get("p")))
                size = self._parse_price(ask.get("size", ask.get("s")))
                if price is not None and size is not None:
                    self._ob_manager.apply_delta(token_id, "ask", price, size)
            # Update buffer with latest state
            ob = self._ob_manager.get_orderbook(token_id)
            if ob:
                self._ob_buffer.store(token_id, ob)
        elif event_type == "price_change":
            price = event.get("price")
            size = event.get("size")
            side = str(event.get("side", "")).upper()
            if token_id and price is not None and size is not None:
                book_side = "bid" if side in {"BUY", "BID"} else "ask"
                self._ob_manager.apply_delta(token_id, book_side, price, size)
        elif event_type == "best_bid_ask":
            best_bid = event.get("best_bid")
            best_ask = event.get("best_ask")
            book = self._ob_manager.get_or_create(token_id)
            if best_bid is not None:
                for price in [price for price in list(book.bids.keys()) if price > best_bid]:
                    book.bids.pop(price, None)
                book.apply_delta("bid", float(best_bid), book.bids.get(float(best_bid), 1.0))
            if best_ask is not None:
                for price in [price for price in list(book.asks.keys()) if price < best_ask]:
                    book.asks.pop(price, None)
                book.apply_delta("ask", float(best_ask), book.asks.get(float(best_ask), 1.0))

        await self._emit_event(event)

    def get_buffered_orderbook(self, token_id: str) -> Any | None:
        """Get orderbook from buffer (fallback during disconnection)."""
        return self._ob_buffer.get(token_id)
