from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from config import Settings
from exchange_client.base import Market, Orderbook
from models.hypothesis import SignalOutput


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


@dataclass
class AnalystReview:
    enabled: bool
    reviewed: bool
    allow: bool
    reason: str = ""
    confidence: float = 0.0
    cached: bool = False
    tokens_used: int = 0
    model: str = ""
    raw_decision: str = ""
    latency_ms: float = 0.0


class Crypto15mAiAnalyst:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._calls: deque[datetime] = deque()
        self._cache: dict[str, tuple[datetime, AnalystReview]] = {}
        self._last_call_at: dict[str, datetime] = {}
        timeout = float(settings.lab.crypto15m.ai_analyst.timeout_sec)
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _cfg(self):
        return self._settings.lab.crypto15m.ai_analyst

    def _enabled(self) -> bool:
        cfg = self._cfg()
        if not cfg.enabled:
            return False
        if cfg.shadow_only and self._settings.mode != "shadow_maker":
            return False
        if cfg.provider.strip().lower() != "qwen":
            return False
        return bool(self._settings.qwen_api_key.strip())

    def _market_key(
        self,
        *,
        portfolio_key: str,
        market_id: str,
        side: str,
        time_to_resolution_sec: float,
        signal_confidence: float,
        signal_edge: float,
    ) -> str:
        # Keep the analyst budget for distinct market situations, not every tiny edge/confidence tick.
        rounded_ttr = int(max(0.0, time_to_resolution_sec) // 120)
        return "|".join(
            [
                portfolio_key,
                market_id,
                side,
                str(rounded_ttr),
            ]
        )

    def _within_budget(self, now: datetime) -> bool:
        one_hour_ago = now - timedelta(hours=1)
        while self._calls and self._calls[0] < one_hour_ago:
            self._calls.popleft()
        return len(self._calls) < int(self._cfg().max_calls_per_hour)

    def _should_review(
        self,
        *,
        signal: SignalOutput,
        external_data: dict[str, Any],
        now: datetime,
        cache_key: str,
    ) -> AnalystReview | None:
        cfg = self._cfg()
        if not self._enabled():
            return AnalystReview(False, False, True, reason="disabled")
        if signal.side not in {"YES", "NO"}:
            return AnalystReview(True, False, True, reason="no_trade_signal")
        if bool(external_data.get("crypto_ohlcv_stale")):
            return AnalystReview(True, False, True, reason="crypto_ohlcv_stale")
        if signal.confidence < float(cfg.min_signal_confidence):
            return AnalystReview(True, False, True, reason="below_ai_confidence")
        if signal.edge < float(cfg.min_signal_edge):
            return AnalystReview(True, False, True, reason="below_ai_edge")
        time_to_resolution_sec = _coerce_float(external_data.get("time_to_resolution_sec"), 0.0)
        if time_to_resolution_sec < float(cfg.min_time_to_resolution_sec):
            return AnalystReview(True, True, False, reason="too_close_to_resolution", raw_decision="VETO")
        if time_to_resolution_sec > float(cfg.max_time_to_resolution_sec):
            return AnalystReview(True, False, True, reason="too_early_for_analyst")
        cached = self._cache.get(cache_key)
        if cached is not None and cached[0] >= now:
            verdict = cached[1]
            return AnalystReview(
                verdict.enabled,
                verdict.reviewed,
                verdict.allow,
                reason=verdict.reason,
                confidence=verdict.confidence,
                cached=True,
                tokens_used=verdict.tokens_used,
                model=verdict.model,
                raw_decision=verdict.raw_decision,
                latency_ms=verdict.latency_ms,
            )
        market_cooldown_sec = int(cfg.market_cooldown_sec)
        last_call = self._last_call_at.get(cache_key)
        if last_call is not None and (now - last_call).total_seconds() < market_cooldown_sec:
            return AnalystReview(True, False, True, reason="cooldown")
        if not self._within_budget(now):
            return AnalystReview(True, True, False, reason="hourly_budget_reached", raw_decision="VETO")
        return None

    def _messages(
        self,
        *,
        market: Market,
        signal: SignalOutput,
        trade_orderbook: Orderbook,
        no_orderbook: Orderbook,
        external_data: dict[str, Any],
        quality_score: float,
    ) -> list[dict[str, str]]:
        payload = {
            "question": market.question,
            "side": signal.side,
            "signal_confidence": round(float(signal.confidence), 4),
            "expected_net_ev": round(float(signal.edge), 6),
            "market_mid": round(float(trade_orderbook.mid_price), 4),
            "best_bid": round(float(trade_orderbook.best_bid), 4),
            "best_ask": round(float(trade_orderbook.best_ask), 4),
            "spread": round(float(trade_orderbook.spread), 4),
            "yes_best_ask": round(_coerce_float(external_data.get("yes_best_ask"), 0.0), 4),
            "no_best_ask": round(_coerce_float(external_data.get("no_best_ask"), 0.0), 4),
            "yes_probability": round(_coerce_float(external_data.get("crypto15m_model_yes_probability"), 0.0), 4),
            "no_trade_probability": round(_coerce_float(external_data.get("crypto15m_model_no_trade_probability"), 0.0), 4),
            "ret_1m": round(_coerce_float(external_data.get("ret_1m"), 0.0), 6),
            "ret_5m": round(
                _coerce_float(
                    external_data.get("ret_5m", external_data.get("crypto_ret_5m", 0.0)),
                    0.0,
                ),
                6,
            ),
            "ret_15m": round(_coerce_float(external_data.get("ret_15m"), 0.0), 6),
            "return_zscore_15m": round(_coerce_float(external_data.get("return_zscore_15m"), 0.0), 4),
            "trend_consistency_15m": round(_coerce_float(external_data.get("trend_consistency_15m"), 0.0), 4),
            "volatility_15m": round(_coerce_float(external_data.get("volatility_15m"), 0.0), 6),
            "volume_spike_15m": round(_coerce_float(external_data.get("volume_spike_15m"), 0.0), 4),
            "distance_to_15m_open": round(_coerce_float(external_data.get("distance_to_15m_open"), 0.0), 6),
            "time_to_resolution_sec": int(_coerce_float(external_data.get("time_to_resolution_sec"), 0.0)),
            "quality_score": round(float(quality_score), 2),
            "ohlcv_age_sec": round(_coerce_float(external_data.get("crypto_ohlcv_age_sec"), 0.0), 2),
            "ohlcv_exchange": str(external_data.get("crypto_ohlcv_exchange") or ""),
        }
        return [
            {
                "role": "system",
                "content": (
                    "You are a conservative BTC 15-minute Polymarket shadow trading analyst. "
                    "You do not create trades. You only decide whether to ALLOW or VETO an already proposed trade. "
                    "Prefer VETO when momentum is noisy, EV is thin, spread/depth look weak, or the setup looks late. "
                    'Reply with compact JSON only: {"decision":"ALLOW|VETO","confidence":0..1,"reason":"short_snake_case_reason"}.'
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            },
        ]

    def review_candidate(
        self,
        *,
        portfolio_key: str,
        market: Market,
        signal: SignalOutput,
        trade_orderbook: Orderbook,
        no_orderbook: Orderbook,
        external_data: dict[str, Any],
        quality_score: float,
        now: datetime | None = None,
    ) -> AnalystReview:
        current = now or _utcnow()
        cache_key = self._market_key(
            portfolio_key=portfolio_key,
            market_id=market.id,
            side=str(signal.side or ""),
            time_to_resolution_sec=_coerce_float(external_data.get("time_to_resolution_sec"), 0.0),
            signal_confidence=float(signal.confidence),
            signal_edge=float(signal.edge),
        )
        precheck = self._should_review(
            signal=signal,
            external_data=external_data,
            now=current,
            cache_key=cache_key,
        )
        if precheck is not None:
            return precheck

        cfg = self._cfg()
        started_at = _utcnow()
        try:
            response = self._client.post(
                cfg.endpoint,
                headers={
                    "Authorization": f"Bearer {self._settings.qwen_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg.model,
                    "messages": self._messages(
                        market=market,
                        signal=signal,
                        trade_orderbook=trade_orderbook,
                        no_orderbook=no_orderbook,
                        external_data=external_data,
                        quality_score=quality_score,
                    ),
                    "temperature": float(cfg.temperature),
                    "top_p": float(cfg.top_p),
                    "max_tokens": int(cfg.max_tokens),
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return AnalystReview(True, False, True, reason="api_error", model=cfg.model)

        message = (
            ((payload.get("choices") or [{}])[0] or {})
            .get("message", {})
            .get("content", "")
        )
        parsed = _extract_json_object(str(message))
        if not parsed:
            return AnalystReview(True, False, True, reason="api_unparseable", model=cfg.model)

        raw_decision = str(parsed.get("decision") or parsed.get("action") or "").strip().upper()
        confidence = max(0.0, min(1.0, _coerce_float(parsed.get("confidence"), 0.0)))
        reason = str(parsed.get("reason") or "ai_allow").strip()[:80] or "ai_allow"
        allow = raw_decision not in {"VETO", "REJECT", "BLOCK", "NO"}
        usage = payload.get("usage") or {}
        latency_ms = max(0.0, (_utcnow() - started_at).total_seconds() * 1000.0)
        verdict = AnalystReview(
            enabled=True,
            reviewed=True,
            allow=allow,
            reason=reason,
            confidence=confidence,
            cached=False,
            tokens_used=int(_coerce_float(usage.get("total_tokens"), 0.0)),
            model=cfg.model,
            raw_decision=raw_decision or ("ALLOW" if allow else "VETO"),
            latency_ms=latency_ms,
        )
        ttl = int(cfg.cache_ttl_sec)
        self._cache[cache_key] = (current + timedelta(seconds=ttl), verdict)
        self._last_call_at[cache_key] = current
        self._calls.append(current)
        return verdict
