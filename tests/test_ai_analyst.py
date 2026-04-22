from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Settings
from exchange_client.base import Market, Orderbook, OrderbookLevel, Token
from lab.ai_analyst import Crypto15mAiAnalyst
from models.hypothesis import SignalOutput


class _DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _DummyClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return _DummyResponse(self.payload)

    def close(self):
        return None


def _market() -> Market:
    return Market(
        id="btc15m",
        question="Bitcoin Up or Down - April 19, 10:00AM-10:15AM ET",
        category="Crypto",
        end_date=(datetime.now(timezone.utc) + timedelta(minutes=8)).isoformat(),
        resolution_source="",
        active=True,
        volume_24h=1000.0,
        tokens=[Token("yes", "Up", 0.5), Token("no", "Down", 0.5)],
    )


def _book() -> Orderbook:
    return Orderbook(
        market_id="yes",
        bids=[OrderbookLevel(0.48, 200.0)],
        asks=[OrderbookLevel(0.50, 200.0)],
        timestamp=0.0,
    )


def _signal() -> SignalOutput:
    return SignalOutput(
        hypothesis_id="H7",
        market_id="btc15m",
        side="YES",
        model_probability=0.58,
        market_probability=0.49,
        edge=0.012,
        confidence=0.81,
        rationale="crypto15m learned YES",
    )


def test_ai_analyst_veto_is_cached():
    settings = Settings()
    settings.mode = "shadow_maker"
    settings.qwen_api_key = "dummy"
    settings.lab.crypto15m.ai_analyst.enabled = True
    analyst = Crypto15mAiAnalyst(settings)
    analyst._client = _DummyClient({
        "choices": [{"message": {"content": json.dumps({"decision": "VETO", "confidence": 0.87, "reason": "momentum_noise"})}}],
        "usage": {"total_tokens": 42},
    })

    external = {
        "time_to_resolution_sec": 420,
        "crypto_ohlcv_stale": False,
        "crypto15m_model_yes_probability": 0.62,
        "crypto15m_model_no_trade_probability": 0.18,
        "return_zscore_15m": 1.1,
        "trend_consistency_15m": 0.74,
        "ret_5m": 0.006,
    }
    now = datetime.now(timezone.utc)
    first = analyst.review_candidate(
        portfolio_key="Crypto15m_t65_analyst",
        market=_market(),
        signal=_signal(),
        trade_orderbook=_book(),
        no_orderbook=_book(),
        external_data=external,
        quality_score=78.0,
        now=now,
    )
    second = analyst.review_candidate(
        portfolio_key="Crypto15m_t65_analyst",
        market=_market(),
        signal=_signal(),
        trade_orderbook=_book(),
        no_orderbook=_book(),
        external_data=external,
        quality_score=78.0,
        now=now + timedelta(seconds=10),
    )

    assert first.reviewed is True
    assert first.allow is False
    assert first.reason == "momentum_noise"
    assert first.tokens_used == 42
    assert second.cached is True
    assert second.allow is False
    assert analyst._client.calls == 1


def test_ai_analyst_cache_ignores_small_signal_changes_within_time_bucket():
    settings = Settings()
    settings.mode = "shadow_maker"
    settings.qwen_api_key = "dummy"
    settings.lab.crypto15m.ai_analyst.enabled = True
    analyst = Crypto15mAiAnalyst(settings)
    analyst._client = _DummyClient({
        "choices": [{"message": {"content": json.dumps({"decision": "ALLOW", "confidence": 0.7, "reason": "ok"})}}],
        "usage": {"total_tokens": 42},
    })
    external = {"time_to_resolution_sec": 420, "crypto_ohlcv_stale": False}
    now = datetime.now(timezone.utc)
    first = analyst.review_candidate(
        portfolio_key="Crypto15m",
        market=_market(),
        signal=_signal(),
        trade_orderbook=_book(),
        no_orderbook=_book(),
        external_data=external,
        quality_score=78.0,
        now=now,
    )
    changed = _signal()
    changed.confidence = 0.91
    changed.edge = 0.041
    second = analyst.review_candidate(
        portfolio_key="Crypto15m",
        market=_market(),
        signal=changed,
        trade_orderbook=_book(),
        no_orderbook=_book(),
        external_data={"time_to_resolution_sec": 390, "crypto_ohlcv_stale": False},
        quality_score=78.0,
        now=now + timedelta(seconds=20),
    )

    analyst.close()
    assert first.reviewed is True
    assert second.cached is True
    assert analyst._client.calls == 1


def test_ai_analyst_skips_when_disabled_or_missing_key():
    settings = Settings()
    settings.mode = "shadow_maker"
    settings.lab.crypto15m.ai_analyst.enabled = True
    analyst = Crypto15mAiAnalyst(settings)
    verdict = analyst.review_candidate(
        portfolio_key="Crypto15m_t65_analyst",
        market=_market(),
        signal=_signal(),
        trade_orderbook=_book(),
        no_orderbook=_book(),
        external_data={"time_to_resolution_sec": 420, "crypto_ohlcv_stale": False},
        quality_score=70.0,
        now=datetime.now(timezone.utc),
    )
    assert verdict.enabled is False
    assert verdict.reviewed is False
    assert verdict.allow is True
