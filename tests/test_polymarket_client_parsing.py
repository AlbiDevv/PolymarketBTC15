from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Settings
from exchange_client.polymarket import PolymarketClient
from scripts.sync_gamma_closed_events import _dedupe_markets, _normalize_resolution_rows


def test_parse_market_supports_current_gamma_schema():
    payload = {
        "conditionId": "cond_123",
        "question": "Will BTC hit 100k?",
        "category": "Crypto",
        "endDate": "2026-12-31T00:00:00Z",
        "resolutionSource": "oracle",
        "active": True,
        "closed": False,
        "volume24hr": 18234.12,
        "eventId": "evt_1",
        "clobTokenIds": '["tok_yes","tok_no"]',
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.44","0.56"]',
    }

    market = PolymarketClient._parse_market(payload)

    assert market.id == "cond_123"
    assert market.event_id == "evt_1"
    assert market.volume_24h == 18234.12
    assert [t.token_id for t in market.tokens] == ["tok_yes", "tok_no"]
    assert [t.outcome for t in market.tokens] == ["Yes", "No"]
    assert [t.price for t in market.tokens] == [0.44, 0.56]


def test_parse_market_preserves_legacy_tokens_schema():
    payload = {
        "condition_id": "legacy_cond",
        "question": "Legacy market?",
        "active": True,
        "volume_num_24hr": "2500",
        "tokens": [
            {"token_id": "legacy_yes", "outcome": "Yes", "price": "0.33"},
            {"token_id": "legacy_no", "outcome": "No", "price": "0.67"},
        ],
    }

    market = PolymarketClient._parse_market(payload)

    assert market.id == "legacy_cond"
    assert market.volume_24h == 2500.0
    assert len(market.tokens) == 2
    assert market.tokens[0].token_id == "legacy_yes"
    assert market.tokens[1].price == 0.67


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://gamma-api.polymarket.com/markets")
    response = httpx.Response(status_code, request=request, json={"error": "bad order"})
    return httpx.HTTPStatusError("bad order", request=request, response=response)


def test_closed_markets_request_recent_closed_markets_by_default(monkeypatch):
    async def run():
        client = PolymarketClient(Settings())
        calls = []

        async def fake_gamma_get(path, params=None):
            calls.append((path, dict(params or {})))
            return [{"id": "m1", "question": "Q"}]

        client._gamma_get = fake_gamma_get
        try:
            rows = await client.get_closed_markets(limit=10, max_pages=1)
        finally:
            await client.close()
        return rows, calls

    rows, calls = asyncio.run(run())

    assert rows == [{"id": "m1", "question": "Q"}]
    assert calls == [
        (
            "/markets",
            {
                "closed": "true",
                "limit": "10",
                "offset": "0",
                "order": "closedTime",
                "ascending": "false",
            },
        )
    ]


def test_get_market_by_slug_uses_direct_market_endpoint_first(monkeypatch):
    async def run():
        client = PolymarketClient(Settings())
        calls = []

        async def fake_gamma_get(path, params=None):
            calls.append(path)
            return {
                "conditionId": "cond_slug",
                "question": "Bitcoin Up or Down - April 13, 7:15AM-7:30AM ET",
                "active": True,
                "closed": False,
                "endDate": "2026-04-13T11:30:00Z",
                "tokens": [
                    {"token_id": "up", "outcome": "Up", "price": "0.50"},
                    {"token_id": "down", "outcome": "Down", "price": "0.50"},
                ],
            }

        client._gamma_get = fake_gamma_get
        try:
            market = await client.get_market_by_slug("btc-updown-15m-1776081600")
        finally:
            await client.close()
        return market, calls

    market, calls = asyncio.run(run())

    assert market is not None
    assert market.id == "cond_slug"
    assert calls == ["/markets/slug/btc-updown-15m-1776081600"]


def test_get_market_by_slug_falls_back_to_event_markets(monkeypatch):
    async def run():
        client = PolymarketClient(Settings())
        calls = []

        async def fake_gamma_get(path, params=None):
            calls.append(path)
            if path.startswith("/markets/slug/"):
                raise _http_status_error(404)
            return {
                "markets": [
                    {
                        "conditionId": "cond_event_slug",
                        "question": "Ethereum Up or Down - April 13, 7:15AM-7:30AM ET",
                        "active": True,
                        "closed": False,
                        "endDate": "2026-04-13T11:30:00Z",
                        "clobTokenIds": '["up","down"]',
                        "outcomes": '["Up","Down"]',
                    }
                ]
            }

        client._gamma_get = fake_gamma_get
        try:
            market = await client.get_market_by_slug("eth-updown-15m-1776081600")
        finally:
            await client.close()
        return market, calls

    market, calls = asyncio.run(run())

    assert market is not None
    assert market.id == "cond_event_slug"
    assert calls == [
        "/markets/slug/eth-updown-15m-1776081600",
        "/events/slug/eth-updown-15m-1776081600",
    ]


def test_closed_markets_retry_without_order_when_gamma_rejects(monkeypatch):
    async def run():
        client = PolymarketClient(Settings())
        calls = []

        async def fake_gamma_get(path, params=None):
            calls.append((path, dict(params or {})))
            if len(calls) == 1:
                raise _http_status_error(500)
            return [{"id": "m1"}]

        client._gamma_get = fake_gamma_get
        try:
            rows = await client.get_closed_markets(limit=10, max_pages=1, order="closedTime")
        finally:
            await client.close()
        return rows, calls

    rows, calls = asyncio.run(run())

    assert rows == [{"id": "m1"}]
    assert calls == [
        (
            "/markets",
            {
                "closed": "true",
                "limit": "10",
                "offset": "0",
                "order": "closedTime",
                "ascending": "false",
            },
        ),
        ("/markets", {"closed": "true", "limit": "10", "offset": "0"}),
    ]


def test_closed_events_retry_without_order_when_gamma_rejects(monkeypatch):
    async def run():
        client = PolymarketClient(Settings())
        calls = []

        async def fake_gamma_get(path, params=None):
            calls.append((path, dict(params or {})))
            if len(calls) == 1:
                raise _http_status_error(422)
            return [{"id": "e1", "title": "Event", "closedTime": "2026-01-01T00:00:00Z"}]

        client._gamma_get = fake_gamma_get
        try:
            rows = await client.get_closed_events(limit=10, max_pages=1, order="closed_time")
        finally:
            await client.close()
        return rows, calls

    rows, calls = asyncio.run(run())

    assert rows[0]["event_id"] == "e1"
    assert calls == [
        (
            "/events",
            {
                "closed": "true",
                "limit": "10",
                "offset": "0",
                "order": "closed_time",
                "ascending": "false",
            },
        ),
        ("/events", {"closed": "true", "limit": "10", "offset": "0"}),
    ]


def test_closed_markets_include_extra_gamma_params(monkeypatch):
    async def run():
        client = PolymarketClient(Settings())
        calls = []

        async def fake_gamma_get(path, params=None):
            calls.append((path, dict(params or {})))
            return [{"id": "m1"}]

        client._gamma_get = fake_gamma_get
        try:
            await client.get_closed_markets(
                limit=10,
                max_pages=1,
                extra_params={"end_date_max": "2025-04-12T00:00:00+00:00"},
            )
        finally:
            await client.close()
        return calls

    calls = asyncio.run(run())

    assert calls == [
        (
            "/markets",
            {
                "closed": "true",
                "limit": "10",
                "offset": "0",
                "order": "closedTime",
                "ascending": "false",
                "end_date_max": "2025-04-12T00:00:00+00:00",
            },
        )
    ]


def test_prices_history_uses_exact_window_before_interval_fallback(monkeypatch):
    async def run():
        client = PolymarketClient(Settings())
        calls = []

        async def fake_clob_get(path, params=None):
            calls.append((path, dict(params or {})))
            return {"history": [{"t": 1775982951, "p": 0.996}]}

        client._clob_get = fake_clob_get
        try:
            rows = await client.get_prices_history(
                "tok",
                start_ts=1775982949,
                end_ts=1775988349,
                interval="all",
                fidelity=60,
            )
        finally:
            await client.close()
        return rows, calls

    rows, calls = asyncio.run(run())

    assert rows == [{"token_id": "tok", "timestamp": 1775982951, "price": 0.996}]
    assert calls == [
        (
            "/prices-history",
            {
                "market": "tok",
                "startTs": "1775982949",
                "endTs": "1775988349",
            },
        ),
    ]


def test_prices_history_retries_with_interval_when_exact_window_empty(monkeypatch):
    async def run():
        client = PolymarketClient(Settings())
        calls = []

        async def fake_clob_get(path, params=None):
            calls.append((path, dict(params or {})))
            if len(calls) == 1:
                return {"history": []}
            return {"history": [{"t": 1775982951, "p": 0.996}]}

        client._clob_get = fake_clob_get
        try:
            rows = await client.get_prices_history(
                "tok",
                start_ts=1775982949,
                end_ts=1775988349,
                interval="all",
                fidelity=60,
            )
        finally:
            await client.close()
        return rows, calls

    rows, calls = asyncio.run(run())

    assert rows == [{"token_id": "tok", "timestamp": 1775982951, "price": 0.996}]
    assert calls == [
        (
            "/prices-history",
            {
                "market": "tok",
                "startTs": "1775982949",
                "endTs": "1775988349",
            },
        ),
        (
            "/prices-history",
            {
                "market": "tok",
                "startTs": "1775982949",
                "endTs": "1775988349",
                "interval": "all",
                "fidelity": "60",
            },
        ),
    ]


def test_resolution_sync_extracts_yes_no_from_current_gamma_schema():
    rows = _normalize_resolution_rows([
        {
            "id": "1954179",
            "conditionId": "cond_1",
            "question": "Ethereum above 2,290 on April 12, 2AM ET?",
            "category": "Crypto",
            "closedTime": "2026-04-12 09:03:43+00",
            "endDate": "2026-04-12T09:00:00Z",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0", "1"]',
            "clobTokenIds": '["yes_token", "no_token"]',
        }
    ])

    assert rows[0]["market_id"] == "cond_1"
    assert rows[0]["yes_token_id"] == "yes_token"
    assert rows[0]["no_token_id"] == "no_token"
    assert rows[0]["outcome"] == "NO"


def test_resolution_sync_normalizes_binary_non_yes_no_markets():
    rows = _normalize_resolution_rows([
        {
            "conditionId": "cond_2",
            "question": "Bitcoin Up or Down?",
            "closedTime": "2026-04-12 09:35:19+00",
            "outcomes": '["Up", "Down"]',
            "outcomePrices": '["1", "0"]',
            "clobTokenIds": '["up_token", "down_token"]',
        }
    ])

    assert rows[0]["yes_token_id"] == "up_token"
    assert rows[0]["no_token_id"] == "down_token"
    assert rows[0]["yes_outcome"] == "Up"
    assert rows[0]["no_outcome"] == "Down"
    assert rows[0]["raw_winning_outcome"] == "Up"
    assert rows[0]["outcome"] == "YES"
    assert rows[0]["standard_binary_pair"] is True


def test_resolution_sync_marks_name_vs_name_pairs_nonstandard():
    rows = _normalize_resolution_rows([
        {
            "conditionId": "cond_3",
            "question": "Week 2 UNC QB: Gio Lopez or Max Johnson",
            "closedTime": "2025-09-24 04:33:03+00",
            "outcomes": '["Gio Lopez", "Max Johnson"]',
            "outcomePrices": '["1", "0"]',
            "clobTokenIds": '["gio_token", "max_token"]',
        }
    ])

    assert rows[0]["standard_binary_pair"] is False


def test_resolution_sync_dedupes_markets_by_condition_id():
    rows = _dedupe_markets([
        {"id": "1", "conditionId": "cond_1"},
        {"id": "2", "conditionId": "cond_1"},
        {"id": "3", "conditionId": "cond_2"},
    ])

    assert [row["id"] for row in rows] == ["1", "3"]
