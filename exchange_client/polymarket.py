from __future__ import annotations

import time
import asyncio
import json
from typing import Literal

import httpx
from loguru import logger

from config import Settings
from .base import (
    ExchangeClientBase,
    Market,
    Token,
    Orderbook,
    OrderbookLevel,
    Trade,
    Position,
    OrderResult,
)


class PolymarketClient(ExchangeClientBase):
    """
    Polymarket CLOB API + Gamma API client.

    Live order signing uses py-clob-client (wraps EIP-712 internally).
    The _clob_sdk field is initialized lazily on first live order.
    Read-only endpoints use raw httpx for speed.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._clob_url = settings.exchange.clob_url.rstrip("/")
        self._gamma_url = settings.exchange.gamma_url.rstrip("/")
        self._chain_id = settings.exchange.chain_id

        headers = {}
        if settings.polymarket_api_key:
            headers["POLY_API_KEY"] = settings.polymarket_api_key
            headers["POLY_API_SECRET"] = settings.polymarket_api_secret
            headers["POLY_PASSPHRASE"] = settings.polymarket_api_passphrase

        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers=headers,
        )
        self._rate_limit_remaining = 100
        self._rate_limit_reset: float = 0

        # Lazy-initialized py-clob-client for signed orders
        self._clob_sdk = None
        self._private_key = settings.polygon_private_key

    def _get_clob_sdk(self):
        """
        Initialize py-clob-client SDK for EIP-712 signed orders.
        Only needed for live mode. Raises clear error if deps missing.
        """
        if self._clob_sdk is not None:
            return self._clob_sdk

        if not self._private_key:
            raise RuntimeError(
                "POLYGON_PRIVATE_KEY required for live trading. "
                "Set it in .env file."
            )

        try:
            from py_clob_client.client import ClobClient
        except ImportError:
            raise RuntimeError(
                "py-clob-client not installed. Run: pip install py-clob-client"
            )

        self._clob_sdk = ClobClient(
            host=self._clob_url,
            key=self._private_key,
            chain_id=self._chain_id,
            creds={
                "apiKey": self._settings.polymarket_api_key,
                "secret": self._settings.polymarket_api_secret,
                "passphrase": self._settings.polymarket_api_passphrase,
            } if self._settings.polymarket_api_key else None,
        )

        # Generate API creds if not provided
        if not self._settings.polymarket_api_key:
            logger.info("No API key found — generating via wallet signature...")
            try:
                creds = self._clob_sdk.create_or_derive_api_creds()
                self._clob_sdk.set_api_creds(creds)
                logger.info(f"API creds generated for {self._clob_sdk.get_address()}")
            except Exception as e:
                raise RuntimeError(f"Failed to generate API credentials: {e}")

        return self._clob_sdk

    async def close(self):
        await self._http.aclose()

    # ──────────────── HTTP helpers ────────────────

    async def _rate_limit_wait(self):
        if self._rate_limit_remaining <= 5 and time.time() < self._rate_limit_reset:
            wait = self._rate_limit_reset - time.time() + 0.5
            logger.warning(f"Rate limit near, waiting {wait:.1f}s")
            await asyncio.sleep(wait)

    def _update_rate_limits(self, resp: httpx.Response):
        rl = resp.headers.get("x-ratelimit-remaining")
        if rl:
            self._rate_limit_remaining = int(rl)
        reset = resp.headers.get("x-ratelimit-reset")
        if reset:
            self._rate_limit_reset = float(reset)

    async def _clob_get(self, path: str, params: dict | None = None) -> dict | list:
        await self._rate_limit_wait()
        resp = await self._http.get(f"{self._clob_url}{path}", params=params)
        self._update_rate_limits(resp)
        resp.raise_for_status()
        return resp.json()

    async def _clob_post(self, path: str, json_body: dict | None = None) -> dict:
        await self._rate_limit_wait()
        resp = await self._http.post(f"{self._clob_url}{path}", json=json_body)
        self._update_rate_limits(resp)
        resp.raise_for_status()
        return resp.json()

    async def _clob_delete(self, path: str, json_body: dict | None = None) -> dict:
        await self._rate_limit_wait()
        resp = await self._http.request("DELETE", f"{self._clob_url}{path}", json=json_body)
        self._update_rate_limits(resp)
        resp.raise_for_status()
        return resp.json()

    async def _gamma_get(self, path: str, params: dict | None = None) -> dict | list:
        resp = await self._http.get(f"{self._gamma_url}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def _gamma_get_with_order_fallback(self, path: str, params: dict | None = None) -> dict | list:
        try:
            return await self._gamma_get(path, params=params)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            if params and "order" in params and status_code in {400, 422, 500}:
                fallback_params = dict(params)
                bad_order = fallback_params.pop("order", None)
                fallback_params.pop("ascending", None)
                logger.warning(
                    "Gamma {} rejected order={!r} with HTTP {}; retrying without order".format(
                        path,
                        bad_order,
                        status_code,
                    )
                )
                return await self._gamma_get(path, params=fallback_params)
            raise

    @staticmethod
    def _coerce_float(*values, default: float = 0.0) -> float:
        for value in values:
            if value in (None, "", "null"):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return default

    @staticmethod
    def _coerce_bool(*values, default: bool = False) -> bool:
        for value in values:
            if isinstance(value, bool):
                return value
            if value is None:
                continue
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"true", "1", "yes"}:
                    return True
                if lowered in {"false", "0", "no"}:
                    return False
        return default

    @staticmethod
    def _parse_list_field(value) -> list:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return parsed
            if "," in stripped:
                return [
                    part.strip().strip("\"").strip("'")
                    for part in stripped.split(",")
                    if part.strip()
                ]
            return [stripped.strip("\"").strip("'")]
        return [value]

    @classmethod
    def _parse_tokens(cls, market_payload: dict) -> list[Token]:
        raw_tokens = market_payload.get("tokens", [])
        if isinstance(raw_tokens, list) and raw_tokens:
            tokens = []
            for token in raw_tokens:
                tokens.append(Token(
                    token_id=str(
                        token.get("token_id")
                        or token.get("tokenId")
                        or token.get("clobTokenId")
                        or ""
                    ),
                    outcome=str(token.get("outcome", "")),
                    price=cls._coerce_float(
                        token.get("price"),
                        token.get("outcomePrice"),
                    ),
                    winner=token.get("winner"),
                ))
            return [t for t in tokens if t.token_id]

        token_ids = cls._parse_list_field(
            market_payload.get("clobTokenIds") or market_payload.get("clob_token_ids")
        )
        outcomes = cls._parse_list_field(market_payload.get("outcomes"))
        prices = cls._parse_list_field(
            market_payload.get("outcomePrices") or market_payload.get("outcome_prices")
        )
        winners = cls._parse_list_field(market_payload.get("winners"))

        tokens = []
        for idx, token_id in enumerate(token_ids):
            if token_id in (None, ""):
                continue
            outcome = str(outcomes[idx]) if idx < len(outcomes) else ""
            price = cls._coerce_float(prices[idx] if idx < len(prices) else None)
            winner = winners[idx] if idx < len(winners) else None
            tokens.append(Token(
                token_id=str(token_id),
                outcome=outcome,
                price=price,
                winner=winner,
            ))
        return tokens

    @classmethod
    def _parse_market(cls, payload: dict) -> Market:
        closed = cls._coerce_bool(payload.get("closed"), default=False)
        accepting_orders = cls._coerce_bool(
            payload.get("accepting_orders"),
            payload.get("acceptingOrders"),
            default=not closed,
        )
        active = cls._coerce_bool(
            payload.get("active"),
            payload.get("enable_order_book"),
            payload.get("enableOrderBook"),
            payload.get("is_active"),
            default=accepting_orders and not closed,
        )
        volume_24h = cls._coerce_float(
            payload.get("volume_24h"),
            payload.get("volume24hr"),
            payload.get("volume24Hr"),
            payload.get("volume_num_24hr"),
            payload.get("volumeNum24hr"),
            payload.get("volume"),
            default=0.0,
        )

        return Market(
            id=str(
                payload.get("condition_id")
                or payload.get("conditionId")
                or payload.get("id")
                or ""
            ),
            question=str(payload.get("question", "")),
            category=str(payload.get("category", "")),
            end_date=payload.get("end_date_iso") or payload.get("endDate") or payload.get("end_date"),
            resolution_source=str(
                payload.get("resolution_source")
                or payload.get("resolutionSource")
                or ""
            ),
            active=active and not closed,
            volume_24h=volume_24h,
            tokens=cls._parse_tokens(payload),
            event_id=payload.get("event_id") or payload.get("eventId"),
            tags=[
                str(tag).strip()
                for tag in cls._parse_list_field(payload.get("tags"))
                if str(tag).strip()
            ],
        )

    # ──────────────── Markets (paginated) ────────────────

    async def get_markets(
        self,
        active_only: bool = True,
        max_pages: int = 5,
        *,
        order_by: str = "volume24hr",
        ascending: bool = False,
    ) -> list[Market]:
        """
        Fetch active markets with bounded pagination.
        Gamma's response shape has changed over time; we support both cursor and
        offset-style pagination but keep a strict page cap so a single cycle does
        not spend minutes traversing the full market universe.
        """
        all_markets: list[Market] = []
        seen_market_ids: set[str] = set()
        cursor: str | None = None
        offset = 0
        limit = 100

        for page in range(max_pages):
            params: dict[str, str] = {"limit": str(limit)}
            if active_only:
                params["closed"] = "false"
            if order_by:
                params["order"] = order_by
            params["ascending"] = "true" if ascending else "false"
            if cursor:
                params["next_cursor"] = cursor
            else:
                params["offset"] = str(offset)

            data = await self._gamma_get("/markets", params=params)

            # Gamma API may return list or dict with data+next_cursor
            items: list[dict] = []
            next_cursor: str | None = None

            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("data", data.get("markets", []))
                next_cursor = data.get("next_cursor")
            else:
                break

            if not items:
                break

            page_new = 0
            for m in items:
                market = self._parse_market(m)
                if market.id and market.id in seen_market_ids:
                    continue
                if market.id:
                    seen_market_ids.add(market.id)
                all_markets.append(market)
                page_new += 1

            if next_cursor:
                cursor = next_cursor
                continue

            offset += len(items)
            if len(items) < limit or page_new == 0:
                break

        return all_markets

    async def get_market_by_slug(self, slug: str) -> Market | None:
        """
        Fetch a single Gamma market by slug.

        Crypto Up/Down markets are short-lived and often fall outside the
        volume-sorted active scan, so runtime discovery needs direct slug
        lookups for the current and next 15m windows.
        """
        normalized = str(slug or "").strip()
        if not normalized:
            return None

        try:
            payload = await self._gamma_get(f"/markets/slug/{normalized}")
            if isinstance(payload, dict):
                market = self._parse_market(payload)
                if market.id and market.tokens:
                    return market
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status not in {404, 422}:
                logger.debug(f"Gamma market slug lookup failed for {normalized}: HTTP {status}")
        except Exception as exc:
            logger.debug(f"Gamma market slug lookup failed for {normalized}: {exc}")

        try:
            payload = await self._gamma_get(f"/events/slug/{normalized}")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status not in {404, 422}:
                logger.debug(f"Gamma event slug lookup failed for {normalized}: HTTP {status}")
            return None
        except Exception as exc:
            logger.debug(f"Gamma event slug lookup failed for {normalized}: {exc}")
            return None

        payloads: list[dict] = []
        if isinstance(payload, dict):
            markets = payload.get("markets")
            if isinstance(markets, list):
                payloads.extend(item for item in markets if isinstance(item, dict))
            else:
                payloads.append(payload)
        elif isinstance(payload, list):
            payloads.extend(item for item in payload if isinstance(item, dict))

        for item in payloads:
            market = self._parse_market(item)
            if market.id and market.tokens:
                return market
        return None

    async def get_closed_events(
        self,
        *,
        limit: int = 100,
        max_pages: int = 20,
        order: str | None = "closedTime",
        ascending: bool | None = False,
    ) -> list[dict]:
        events: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            params = {
                "closed": "true",
                "limit": str(limit),
                "offset": str(offset),
            }
            if order:
                params["order"] = order
            if ascending is not None:
                params["ascending"] = "true" if ascending else "false"
            data = await self._gamma_get_with_order_fallback("/events", params=params)
            items = data if isinstance(data, list) else data.get("data", data.get("events", []))
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                events.append({
                    "event_id": str(item.get("id") or item.get("event_id") or ""),
                    "slug": str(item.get("slug") or ""),
                    "title": str(item.get("title") or item.get("question") or ""),
                    "category": str(item.get("category") or ""),
                    "subcategory": str(item.get("subcategory") or ""),
                    "closed": self._coerce_bool(item.get("closed"), default=False),
                    "active": self._coerce_bool(item.get("active"), default=False),
                    "closed_time": item.get("closedTime") or item.get("closed_time") or item.get("endDate"),
                    "start_date": item.get("startDate") or item.get("start_date"),
                    "end_date": item.get("endDate") or item.get("end_date"),
                    "volume": self._coerce_float(item.get("volume"), item.get("volume24hr"), default=0.0),
                    "liquidity": self._coerce_float(item.get("liquidity"), item.get("liquidityClob"), default=0.0),
                    "resolution_source": str(item.get("resolutionSource") or item.get("resolution_source") or ""),
                    "raw": item,
                })
            if len(items) < limit:
                break
            offset += len(items)
        return events

    async def get_closed_markets(
        self,
        *,
        limit: int = 100,
        max_pages: int = 20,
        order: str | None = "closedTime",
        ascending: bool | None = False,
        extra_params: dict | None = None,
    ) -> list[dict]:
        markets: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            params = {
                "closed": "true",
                "limit": str(limit),
                "offset": str(offset),
            }
            if order:
                params["order"] = order
            if ascending is not None:
                params["ascending"] = "true" if ascending else "false"
            if extra_params:
                params.update({str(key): str(value) for key, value in extra_params.items()})
            data = await self._gamma_get_with_order_fallback("/markets", params=params)
            items = data if isinstance(data, list) else data.get("data", data.get("markets", []))
            if not items:
                break
            markets.extend([item for item in items if isinstance(item, dict)])
            if len(items) < limit:
                break
            offset += len(items)
        return markets

    async def get_prices_history(
        self,
        token_id: str,
        *,
        start_ts: int,
        end_ts: int,
        interval: str = "all",
        fidelity: int = 1,
    ) -> list[dict]:
        base_params = {
            "market": token_id,
            "startTs": str(int(start_ts)),
            "endTs": str(int(end_ts)),
        }
        data = await self._clob_get("/prices-history", params=base_params)
        history = data.get("history", []) if isinstance(data, dict) else []
        if not history:
            fallback_data = await self._clob_get("/prices-history", params={
                **base_params,
                "interval": interval,
                "fidelity": str(int(fidelity)),
            })
            fallback_history = fallback_data.get("history", []) if isinstance(fallback_data, dict) else []
            if fallback_history:
                history = fallback_history
        out: list[dict] = []
        for item in history:
            if not isinstance(item, dict):
                continue
            ts = self._coerce_float(item.get("t"), item.get("timestamp"), default=0.0)
            price = self._coerce_float(item.get("p"), item.get("price"), default=0.0)
            out.append({
                "token_id": token_id,
                "timestamp": int(ts),
                "price": price,
            })
        return out

    async def get_batch_prices_history(
        self,
        token_ids: list[str],
        *,
        start_ts: int,
        end_ts: int,
        interval: str = "all",
        fidelity: int = 1,
    ) -> dict[str, list[dict]]:
        payload = {
            "markets": token_ids[:20],
            "start_ts": int(start_ts),
            "end_ts": int(end_ts),
            "interval": interval,
            "fidelity": int(fidelity),
        }
        data = await self._clob_post("/batch-prices-history", json_body=payload)
        raw_history = data.get("history", {}) if isinstance(data, dict) else {}
        out: dict[str, list[dict]] = {}
        for token_id, history in raw_history.items():
            rows: list[dict] = []
            for item in history or []:
                if not isinstance(item, dict):
                    continue
                ts = self._coerce_float(item.get("t"), item.get("timestamp"), default=0.0)
                price = self._coerce_float(item.get("p"), item.get("price"), default=0.0)
                rows.append({
                    "token_id": str(token_id),
                    "timestamp": int(ts),
                    "price": price,
                })
            out[str(token_id)] = rows
        return out

    # ──────────────── Orderbook ────────────────

    async def get_orderbook(self, token_id: str) -> Orderbook:
        data = await self._clob_get("/book", params={"token_id": token_id})
        now = time.time()

        bids = [
            OrderbookLevel(price=float(b["price"]), size=float(b["size"]))
            for b in data.get("bids", [])
        ]
        asks = [
            OrderbookLevel(price=float(a["price"]), size=float(a["size"]))
            for a in data.get("asks", [])
        ]

        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        return Orderbook(market_id=token_id, bids=bids, asks=asks, timestamp=now)

    # ──────────────── Trade history ────────────────

    async def get_fee_rate_bps(self, token_id: str) -> float | None:
        try:
            data = await self._clob_get(f"/fee-rate/{token_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise
        if not isinstance(data, dict):
            return None
        try:
            return float(data.get("base_fee"))
        except (TypeError, ValueError):
            return None

    async def get_trade_history(self, market_id: str, limit: int = 100) -> list[Trade]:
        data = await self._gamma_get(
            "/trades", params={"market": market_id, "limit": str(limit)}
        )
        return [
            Trade(
                market_id=market_id,
                side=t.get("side", "YES").upper(),
                price=float(t.get("price", 0)),
                size=float(t.get("size", 0)),
                timestamp=float(t.get("timestamp", 0)),
            )
            for t in data
        ]

    # ──────────────── Orders (EIP-712 signed for live) ────────────────

    async def place_order(
        self,
        token_id: str,
        side: Literal["BUY", "SELL"],
        price: float,
        size: float,
    ) -> OrderResult:
        """
        Place a real order on Polymarket CLOB via py-clob-client.
        Mode checks are handled by the execution broker layer — if this method
        is called, it means a real order should be sent.
        """
        sdk = self._get_clob_sdk()

        loop = asyncio.get_running_loop()
        try:
            from py_clob_client.order_builder.constants import BUY as SDK_BUY, SELL as SDK_SELL

            sdk_side = SDK_BUY if side == "BUY" else SDK_SELL

            resp = await loop.run_in_executor(
                None,
                lambda: sdk.create_and_post_order({
                    "token_id": token_id,
                    "side": sdk_side,
                    "price": price,
                    "size": size,
                    "type": "GTC",
                }),
            )
        except ImportError:
            resp = await loop.run_in_executor(
                None,
                lambda: sdk.create_and_post_order({
                    "token_id": token_id,
                    "side": side,
                    "price": price,
                    "size": size,
                }),
            )

        if isinstance(resp, dict):
            return OrderResult(
                order_id=resp.get("orderID", resp.get("id", "")),
                status=resp.get("status", "UNKNOWN"),
                filled_size=float(resp.get("filledSize", 0)),
                avg_fill_price=float(resp.get("avgFillPrice", 0)),
            )

        return OrderResult(
            order_id=getattr(resp, "order_id", getattr(resp, "orderID", str(resp))),
            status=getattr(resp, "status", "SUBMITTED"),
            filled_size=0,
            avg_fill_price=0,
        )

    async def cancel_order(self, order_id: str) -> bool:
        if self._settings.mode in ("dry_run", "paper"):
            logger.info(f"[{self._settings.mode.upper()}] Would cancel order {order_id}")
            return True

        try:
            await self._clob_delete("/order", json_body={"orderID": order_id})
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def cancel_all_orders(self) -> int:
        if self._settings.mode in ("dry_run", "paper"):
            logger.info(f"[{self._settings.mode.upper()}] Would cancel all orders")
            return 0

        data = await self._clob_delete("/cancel-all")
        return data.get("canceled", 0)

    # ──────────────── Positions ────────────────

    async def get_positions(self) -> list[Position]:
        data = await self._clob_get("/positions")

        items = data if isinstance(data, list) else data.get("positions", [])
        return [
            Position(
                market_id=p.get("market", p.get("condition_id", "")),
                token_id=p.get("token_id", ""),
                side=p.get("side", "YES"),
                size=float(p.get("size", 0)),
                avg_price=float(p.get("avgPrice", 0)),
            )
            for p in items
        ]

    # ──────────────── Resolution status (NEW) ────────────────

    async def get_market_resolution(self, condition_id: str) -> dict | None:
        """
        Check if a market has resolved via Gamma API.
        Returns dict with resolved, outcome, status fields, or None.
        """
        try:
            data = await self._gamma_get(f"/markets/{condition_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

        if not isinstance(data, dict):
            return None

        resolved = data.get("closed", False) or data.get("resolved", False)
        if not resolved:
            return None

        # Determine outcome from tokens' winner field
        outcome = None
        for t in data.get("tokens", []):
            if t.get("winner") is True:
                outcome = t.get("outcome", "").upper()
                break

        # Also check explicit resolution fields
        if not outcome:
            outcome = data.get("resolution", data.get("outcome", ""))

        status = "resolved"
        if data.get("disputed"):
            status = "disputed"
        elif data.get("cancelled"):
            status = "cancelled"

        return {
            "resolved": True,
            "outcome": outcome or "",
            "status": status,
            "resolution_source": data.get("resolution_source", ""),
        }
