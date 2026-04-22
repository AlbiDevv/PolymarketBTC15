from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger
import pandas as pd

from config import Settings
from research.crypto15m import build_crypto_features, normalize_ohlcv_rows


@dataclass(frozen=True)
class CryptoOHLCVSnapshot:
    symbol: str
    fresh: bool
    age_sec: float | None = None
    exchange_id: str = ""
    features: dict[str, float] = field(default_factory=dict)


class CryptoOHLCVLiveFeed:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._enabled = bool(settings.crypto_data.enabled and settings.lab.crypto15m.live_ohlcv_enabled)
        self._symbols = list(settings.crypto_data.symbols)
        self._exchanges = [settings.crypto_data.exchange_primary, *settings.crypto_data.exchange_fallbacks]
        self._lookback_minutes = int(settings.lab.crypto15m.live_ohlcv_lookback_minutes)
        self._poll_sec = int(settings.lab.crypto15m.live_ohlcv_poll_sec)
        self._stale_sec = int(settings.lab.crypto15m.live_ohlcv_stale_sec)
        self._frames: dict[str, pd.DataFrame] = {}
        self._exchange_by_symbol: dict[str, str] = {}
        self._last_error_by_symbol: dict[str, str] = {}
        self._feature_cache: dict[str, tuple[datetime, CryptoOHLCVSnapshot]] = {}
        self._exchange_cache: dict[str, Any] = {}
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        if not self._enabled or self._running:
            return
        self._running = True
        await self.refresh()
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self._close_exchanges_sync)

    async def _loop(self):
        while self._running:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"Crypto OHLCV live refresh failed: {exc}")
            await asyncio.sleep(self._poll_sec)

    async def refresh(self):
        if not self._enabled:
            return
        await asyncio.to_thread(self._refresh_sync)

    def feature_snapshot(self, symbol: str, *, at: datetime, now: datetime | None = None) -> CryptoOHLCVSnapshot:
        if not self._enabled:
            return CryptoOHLCVSnapshot(symbol=symbol, fresh=False)
        frame = self._frames.get(symbol)
        if frame is None or frame.empty:
            return CryptoOHLCVSnapshot(symbol=symbol, fresh=False, exchange_id=self._exchange_by_symbol.get(symbol, ""))
        current = now or datetime.now(timezone.utc)
        cached = self._feature_cache.get(symbol)
        if cached is not None:
            cached_at, snapshot = cached
            if (current - cached_at).total_seconds() <= 5.0:
                return snapshot
        latest_ts = pd.Timestamp(frame["timestamp"].max()).to_pydatetime()
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.replace(tzinfo=timezone.utc)
        age_sec = max(0.0, (current - latest_ts.astimezone(timezone.utc)).total_seconds())
        fresh = age_sec <= self._stale_sec
        features = build_crypto_features(frame, at=at, symbol=symbol) if fresh else {}
        snapshot = CryptoOHLCVSnapshot(
            symbol=symbol,
            fresh=fresh,
            age_sec=age_sec,
            exchange_id=self._exchange_by_symbol.get(symbol, ""),
            features=features,
        )
        self._feature_cache[symbol] = (current, snapshot)
        return snapshot

    def _refresh_sync(self):
        since_ms = int((datetime.now(timezone.utc).timestamp() - self._lookback_minutes * 60) * 1000)
        limit = min(1000, max(80, self._lookback_minutes + 5))
        for symbol in self._symbols:
            last_error: Exception | None = None
            for exchange_id in self._exchanges:
                try:
                    exchange = self._get_exchange(exchange_id)
                    rows = exchange.fetch_ohlcv(symbol, "1m", since_ms, limit)
                    frame = normalize_ohlcv_rows(rows, exchange_id=exchange_id, symbol=symbol, timeframe="1m")
                    if frame.empty:
                        raise RuntimeError(f"empty OHLCV for {symbol}")
                    self._frames[symbol] = frame.tail(self._lookback_minutes + 5).reset_index(drop=True)
                    self._exchange_by_symbol[symbol] = exchange_id
                    self._feature_cache.pop(symbol, None)
                    self._last_error_by_symbol.pop(symbol, None)
                    break
                except Exception as exc:  # pragma: no cover - network fallback
                    last_error = exc
                    self._last_error_by_symbol[symbol] = f"{type(exc).__name__}: {str(exc)[:180]}"
            else:
                logger.warning(f"Crypto OHLCV fetch failed for {symbol}: {last_error}")

    def _get_exchange(self, exchange_id: str):
        import ccxt

        cached = self._exchange_cache.get(exchange_id)
        if cached is not None:
            return cached
        cls = getattr(ccxt, exchange_id)
        exchange = cls({
            "enableRateLimit": True,
            "timeout": 10000,
        })
        exchange.load_markets()
        if not bool((exchange.has or {}).get("fetchOHLCV")):
            raise RuntimeError(f"{exchange_id} does not support fetchOHLCV")
        self._exchange_cache[exchange_id] = exchange
        return exchange

    def _close_exchanges_sync(self):
        for exchange in list(self._exchange_cache.values()):
            try:
                close = getattr(exchange, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
        self._exchange_cache.clear()

    def diagnostics(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "symbols": self._symbols,
            "exchanges": self._exchanges,
            "frames": {symbol: len(frame) for symbol, frame in self._frames.items()},
            "last_errors": dict(self._last_error_by_symbol),
        }
