"""
Formal Hypothesis Specification — v3.0 addition.

Each hypothesis must be described in a standard format so it can be
uniformly tested, compared, enabled/disabled in production.

Template from TZ v3.0 §7.1:
  - ID and name
  - Economic intuition (why the market may be wrong here)
  - Data sources and precise timestamps
  - Signal formula and trigger threshold
  - Entry rule: side, price, size, order TTL
  - Exit rule: re-pricing, stop, timeout, settlement
  - Validation metrics: EV, Sharpe, fill rate, calibration, stability
  - Kill switch: edge degradation, slippage growth, calibration loss
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd

from exchange_client.base import Orderbook
from research.crypto15m import crypto15m_side_gate_reason
from research.trade_costs import net_ev_per_share


def _crypto15m_composite_momentum(data: dict[str, Any]) -> float:
    components = [
        float(data.get("crypto_ret_1m", data.get("ret_1m", 0.0)) or 0.0),
        float(data.get("crypto_ret_3m", data.get("ret_3m", 0.0)) or 0.0),
        float(data.get("crypto_ret_5m", data.get("ret_5m", data.get("price_return_60m", 0.0))) or 0.0),
        float(data.get("crypto_ret_15m", data.get("ret_15m", 0.0)) or 0.0),
    ]
    weights = [0.15, 0.20, 0.40, 0.25]
    weighted = sum(component * weight for component, weight in zip(components, weights, strict=True))
    strongest = max(components, key=lambda value: abs(value), default=0.0)
    if strongest == 0.0:
        return weighted
    strongest_sign = 1.0 if strongest > 0 else -1.0
    aligned_weight = sum(
        weight
        for component, weight in zip(components, weights, strict=True)
        if component != 0.0 and (1.0 if component > 0 else -1.0) == strongest_sign
    )
    if abs(weighted) < abs(strongest) * 0.60 and aligned_weight >= 0.60:
        return strongest * (0.70 + 0.30 * aligned_weight)
    return weighted


def _resolve_no_entry_price(data: dict[str, Any], yes_orderbook: Orderbook) -> float:
    explicit_no_ask = float(data.get("no_best_ask") or 0.0)
    if 0.0 < explicit_no_ask < 1.0:
        return explicit_no_ask
    synthetic_candidates = [
        1.0 - float(getattr(yes_orderbook, "best_bid", 0.0) or 0.0),
        1.0 - float(getattr(yes_orderbook, "mid_price", 0.0) or 0.0),
        1.0 - float(getattr(yes_orderbook, "best_ask", 0.0) or 0.0),
    ]
    for candidate in synthetic_candidates:
        if 0.0 < candidate < 1.0:
            return max(0.001, min(0.999, candidate))
    return 0.0


@dataclass
class SignalOutput:
    """Standardized output from any hypothesis signal function."""
    hypothesis_id: str
    market_id: str
    side: Literal["YES", "NO"] | None  # None = no signal
    model_probability: float
    market_probability: float
    edge: float
    confidence: float  # 0-1
    entry_price: float | None = None
    order_ttl_sec: int = 300  # auto-cancel after 5 min by default
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HypothesisSpec:
    """Formal specification of a hypothesis, stored alongside code."""
    id: str
    name: str
    intuition: str
    data_sources: list[str]
    signal_description: str
    horizon: str  # e.g. "5-30 min", "until settlement"
    exit_rule: str
    kill_criteria: str
    priority: Literal["high", "medium", "low"]
    enabled: bool = True


class HypothesisBase(ABC):
    """
    Base class for all hypothesis implementations.
    Subclass and implement evaluate() for each H1-H5.
    """

    spec: HypothesisSpec

    @abstractmethod
    def evaluate(
        self,
        market_id: str,
        question: str,
        orderbook: Orderbook,
        price_history: pd.DataFrame | None = None,
        external_data: dict | None = None,
    ) -> SignalOutput:
        """Evaluate this hypothesis for a single market right now."""
        ...

    @abstractmethod
    def train(self, historical_data: pd.DataFrame) -> None:
        """(Re-)calibrate model parameters from training data."""
        ...

    def is_degraded(self, recent_metrics: dict) -> bool:
        """Check if hypothesis should be killed based on recent performance."""
        if recent_metrics.get("sharpe", 1) < 0:
            return True
        if recent_metrics.get("edge_mean", 0.1) < 0.01:
            return True
        if recent_metrics.get("calibration_error", 0) > 0.15:
            return True
        return False


class H1_NewsLag(HypothesisBase):
    """
    H1: Price lags after news events.
    Market reacts slowly (5-30 min) to external news.
    """

    spec = HypothesisSpec(
        id="H1",
        name="News Lag",
        intuition="Prediction markets react 5-30 min slower than news wire / Twitter",
        data_sources=["RSS feeds", "Twitter/X", "Polymarket price stream"],
        signal_description="External prior shifted more than market; buy in direction of shift",
        horizon="5-30 min",
        exit_rule="Price normalizes to new level, or TTL (30 min) expires",
        kill_criteria="No positive EV after commissions over 200+ trades",
        priority="high",
    )

    def __init__(self, min_lag_sec: int = 60, max_lag_sec: int = 1800):
        self._min_lag = min_lag_sec
        self._max_lag = max_lag_sec
        self._calibration_offset: float = 0.0

    def evaluate(
        self,
        market_id: str,
        question: str,
        orderbook: Orderbook,
        price_history: pd.DataFrame | None = None,
        external_data: dict | None = None,
    ) -> SignalOutput:
        mid = orderbook.mid_price
        external_prob = (external_data or {}).get("probability")

        if external_prob is None:
            return SignalOutput(
                hypothesis_id="H1", market_id=market_id, side=None,
                model_probability=mid, market_probability=mid, edge=0, confidence=0,
            )

        adjusted = external_prob + self._calibration_offset
        diff = adjusted - mid

        if abs(diff) < 0.05:
            return SignalOutput(
                hypothesis_id="H1", market_id=market_id, side=None,
                model_probability=adjusted, market_probability=mid,
                edge=abs(diff), confidence=abs(diff),
            )

        side: Literal["YES", "NO"] = "YES" if diff > 0 else "NO"
        entry = orderbook.best_ask if side == "YES" else (1 - orderbook.best_bid)

        return SignalOutput(
            hypothesis_id="H1",
            market_id=market_id,
            side=side,
            model_probability=adjusted,
            market_probability=mid,
            edge=abs(diff),
            confidence=min(abs(diff) * 5, 1.0),
            entry_price=entry,
            order_ttl_sec=1800,
            rationale=f"External prob {adjusted:.2f} vs market {mid:.2f}",
        )

    def train(self, historical_data: pd.DataFrame) -> None:
        if "external_prob" in historical_data.columns and "outcome_prob" in historical_data.columns:
            self._calibration_offset = float(
                (historical_data["outcome_prob"] - historical_data["external_prob"]).mean()
            )


class H2_RoundNumberBias(HypothesisBase):
    """
    H2: Systematic bias at round numbers.
    Crowd overvalues ~50% and ~90% probabilities.
    """

    spec = HypothesisSpec(
        id="H2",
        name="Round Number Bias",
        intuition="Prices near 0.50 and 0.90 are systematically pushed by anchoring",
        data_sources=["Polymarket price history", "Settlement outcomes"],
        signal_description="Price near round number with historical miscalibration",
        horizon="Until re-pricing or settlement",
        exit_rule="Price moves to fair range or settlement",
        kill_criteria="Bias is not statistically significant (p > 0.05)",
        priority="high",
    )

    BIAS_ZONES = [
        (0.08, 0.12, +0.03),   # near 10%: market underestimates
        (0.45, 0.55, -0.02),   # near 50%: market overestimates YES
        (0.88, 0.92, -0.03),   # near 90%: market overestimates YES
    ]

    def __init__(self):
        self._zones = list(self.BIAS_ZONES)

    def evaluate(
        self,
        market_id: str,
        question: str,
        orderbook: Orderbook,
        price_history: pd.DataFrame | None = None,
        external_data: dict | None = None,
    ) -> SignalOutput:
        mid = orderbook.mid_price

        for low, high, bias in self._zones:
            if low <= mid <= high:
                adjusted = mid + bias
                adjusted = max(0.01, min(0.99, adjusted))

                if bias > 0:
                    side: Literal["YES", "NO"] = "YES"
                    entry = orderbook.best_ask
                else:
                    side = "NO"
                    entry = 1 - orderbook.best_bid

                return SignalOutput(
                    hypothesis_id="H2",
                    market_id=market_id,
                    side=side,
                    model_probability=adjusted,
                    market_probability=mid,
                    edge=abs(bias),
                    confidence=0.4,
                    entry_price=entry,
                    rationale=f"Round number zone [{low:.2f}-{high:.2f}], bias={bias:+.2f}",
                )

        return SignalOutput(
            hypothesis_id="H2", market_id=market_id, side=None,
            model_probability=mid, market_probability=mid, edge=0, confidence=0,
        )

    def train(self, historical_data: pd.DataFrame) -> None:
        """Recalibrate bias zones from historical settlement data."""
        if "mid_price" not in historical_data.columns or "outcome" not in historical_data.columns:
            return

        new_zones = []
        for low, high, default_bias in self.BIAS_ZONES:
            mask = (historical_data["mid_price"] >= low) & (historical_data["mid_price"] <= high)
            subset = historical_data[mask]
            if len(subset) < 30:
                new_zones.append((low, high, default_bias))
                continue

            actual_rate = subset["outcome"].mean()
            expected_rate = subset["mid_price"].mean()
            observed_bias = actual_rate - expected_rate
            new_zones.append((low, high, round(observed_bias, 3)))

        self._zones = new_zones


class H4_UnderpricedTails(HypothesisBase):
    """
    H4: Underpriced tail events.
    Low-probability outcomes are systematically cheaper than fair value.
    """

    spec = HypothesisSpec(
        id="H4",
        name="Underpriced Tails",
        intuition="Longshot contracts (< 10%) win more often than their price implies",
        data_sources=["Polymarket prices < 0.10", "Settlement outcomes"],
        signal_description="Longshot calibration shows systematic underpricing",
        horizon="Until settlement",
        exit_rule="Settlement",
        kill_criteria="Actual win rate / implied < 1.1 over 200+ events",
        priority="medium",
    )

    def __init__(self, tail_threshold: float = 0.10, calibration_boost: float = 0.03):
        self._threshold = tail_threshold
        self._boost = calibration_boost

    def evaluate(
        self,
        market_id: str,
        question: str,
        orderbook: Orderbook,
        price_history: pd.DataFrame | None = None,
        external_data: dict | None = None,
    ) -> SignalOutput:
        mid = orderbook.mid_price

        if mid > self._threshold:
            return SignalOutput(
                hypothesis_id="H4", market_id=market_id, side=None,
                model_probability=mid, market_probability=mid, edge=0, confidence=0,
            )

        adjusted = mid + self._boost
        entry = orderbook.best_ask

        return SignalOutput(
            hypothesis_id="H4",
            market_id=market_id,
            side="YES",
            model_probability=adjusted,
            market_probability=mid,
            edge=self._boost,
            confidence=0.3,
            entry_price=entry,
            rationale=f"Tail event: market={mid:.3f}, model={adjusted:.3f}",
        )

    def train(self, historical_data: pd.DataFrame) -> None:
        if "mid_price" not in historical_data.columns or "outcome" not in historical_data.columns:
            return
        tails = historical_data[historical_data["mid_price"] <= self._threshold]
        if len(tails) < 50:
            return
        actual = tails["outcome"].mean()
        implied = tails["mid_price"].mean()
        if implied > 0:
            self._boost = max(0, actual - implied)


class H6_LateStagePressure(HypothesisBase):
    """
    H6: Late-stage microstructure pressure near resolution.
    Uses only live orderbook state, recent price persistence and short-window
    trade direction agreement. Experimental, not a "safe money" strategy.
    """

    spec = HypothesisSpec(
        id="H6",
        name="Late Stage Pressure",
        intuition="Near expiry, persistent extreme pricing plus one-sided book pressure can reveal short-lived mispricing or urgency.",
        data_sources=["Polymarket best bid/ask", "last trade price stream", "time-to-resolution metadata"],
        signal_description="Enter only when extreme price persists, book imbalance stays one-sided and edge remains positive after fee/slippage.",
        horizon="Minutes to <= 30m",
        exit_rule="Small TP/SL, max hold 30m, immediate flatten on resolution",
        kill_criteria="Negative net expectancy after fee/slippage or excessive forced exits",
        priority="medium",
    )

    def evaluate(
        self,
        market_id: str,
        question: str,
        orderbook: Orderbook,
        price_history: pd.DataFrame | None = None,
        external_data: dict | None = None,
    ) -> SignalOutput:
        data = external_data or {}
        yes_mid = float(data.get("yes_mid", orderbook.mid_price))
        extreme_yes_min = float(data.get("extreme_yes_min", 0.92))
        extreme_yes_max = float(data.get("extreme_yes_max", 0.08))
        persistence_required = int(data.get("persistence_required_sec", 120))
        imbalance_min = float(data.get("imbalance_ratio_min", 2.5))
        fee_plus_slippage = float(data.get("fee_plus_slippage", 0.0))

        if yes_mid >= extreme_yes_min:
            persistence = float(data.get("yes_extreme_persistence_sec", 0.0))
            imbalance = float(data.get("yes_imbalance_ratio", 0.0))
            direction_ok = bool(data.get("yes_direction_agrees", False))
            modeled_yes = min(0.995, yes_mid + min(0.03, max(0.0, imbalance - 1.0) * 0.01))
            edge = modeled_yes - yes_mid
            if persistence >= persistence_required and imbalance >= imbalance_min and direction_ok and edge > fee_plus_slippage:
                return SignalOutput(
                    hypothesis_id="H6",
                    market_id=market_id,
                    side="YES",
                    model_probability=modeled_yes,
                    market_probability=yes_mid,
                    edge=edge,
                    confidence=min(0.95, 0.35 + (imbalance - imbalance_min) * 0.08),
                    entry_price=orderbook.best_ask,
                    order_ttl_sec=60,
                    rationale=(
                        f"late-stage YES pressure | mid={yes_mid:.3f} "
                        f"persist={persistence:.0f}s imbalance={imbalance:.2f}"
                    ),
                )

        if yes_mid <= extreme_yes_max:
            persistence = float(data.get("no_extreme_persistence_sec", 0.0))
            imbalance = float(data.get("no_imbalance_ratio", 0.0))
            direction_ok = bool(data.get("no_direction_agrees", False))
            modeled_yes = max(0.001, yes_mid - min(0.03, max(0.0, imbalance - 1.0) * 0.01))
            edge = yes_mid - modeled_yes
            if persistence >= persistence_required and imbalance >= imbalance_min and direction_ok and edge > fee_plus_slippage:
                return SignalOutput(
                    hypothesis_id="H6",
                    market_id=market_id,
                    side="NO",
                    model_probability=modeled_yes,
                    market_probability=yes_mid,
                    edge=edge,
                    confidence=min(0.95, 0.35 + (imbalance - imbalance_min) * 0.08),
                    entry_price=1 - orderbook.best_bid,
                    order_ttl_sec=60,
                    rationale=(
                        f"late-stage NO pressure | mid={yes_mid:.3f} "
                        f"persist={persistence:.0f}s imbalance={imbalance:.2f}"
                    ),
                )

        return SignalOutput(
            hypothesis_id="H6",
            market_id=market_id,
            side=None,
            model_probability=yes_mid,
            market_probability=yes_mid,
            edge=0.0,
            confidence=0.0,
        )

    def train(self, historical_data: pd.DataFrame) -> None:
        return


class H7_Crypto15mDirection(HypothesisBase):
    """
    H7: Active BTC/ETH 15m direction track.
    Uses Polymarket crypto market state plus external crypto momentum/model
    features. It may return YES, NO, or no-trade.
    """

    spec = HypothesisSpec(
        id="H7",
        name="Crypto 15m Direction",
        intuition="Short-horizon BTC/ETH direction markets can be active enough for repeated A/B shadow testing when entry price still leaves positive net EV.",
        data_sources=["Polymarket orderbook", "BTC/USDT and ETH/USDT OHLCV"],
        signal_description="Enter only when crypto momentum/model confidence and fee-adjusted EV clear strict gates.",
        horizon="<= 2h, usually 15m markets",
        exit_rule="Small TP/SL, max hold 15m, settlement flatten",
        kill_criteria="Negative net EV, poor fill rate, or drawdown beyond limits",
        priority="medium",
    )

    def evaluate(
        self,
        market_id: str,
        question: str,
        orderbook: Orderbook,
        price_history: pd.DataFrame | None = None,
        external_data: dict | None = None,
    ) -> SignalOutput:
        data = external_data or {}
        if not bool(data.get("crypto15m_is_market", False)):
            return SignalOutput(
                "H7",
                market_id,
                None,
                orderbook.mid_price,
                orderbook.mid_price,
                0.0,
                0.0,
                rationale=str(data.get("crypto15m_reason") or "not_crypto15m_market"),
            )

        min_confidence = float(data.get("crypto15m_min_confidence", 0.55))
        min_net_ev = float(data.get("crypto15m_min_net_ev", 0.003))
        max_spread = float(data.get("crypto15m_max_spread", 0.04))
        candidate_window_minutes = float(data.get("crypto15m_candidate_window_minutes", 15.0))
        candidate_min_time_to_resolution_sec = float(data.get("crypto15m_candidate_min_time_to_resolution_sec", 0.0))
        candidate_target_time_to_resolution_sec = float(data.get("crypto15m_candidate_target_time_to_resolution_sec", 0.0))
        candidate_target_tolerance_sec = float(data.get("crypto15m_candidate_target_tolerance_sec", 0.0))
        min_entry_price = float(data.get("crypto15m_min_entry_price", 0.0))
        max_entry_price = float(data.get("crypto15m_max_entry_price", 1.0))
        min_abs_return_zscore_15m = float(data.get("crypto15m_min_abs_return_zscore_15m", 0.0))
        min_trend_consistency_15m = float(data.get("crypto15m_min_trend_consistency_15m", 0.0))
        allow_no_trade_fallback = bool(data.get("crypto15m_allow_no_trade_fallback", True))
        no_trade_fallback_max_probability = float(data.get("crypto15m_no_trade_fallback_max_probability", 0.82))
        relax_momentum_gate = bool(data.get("crypto15m_relax_momentum_gate", False))
        relax_regime_gates = bool(data.get("crypto15m_relax_regime_gates", False))
        time_to_resolution_sec = float(data.get("time_to_resolution_sec", 0.0) or 0.0)
        if time_to_resolution_sec > candidate_window_minutes * 60.0:
            return SignalOutput("H7", market_id, None, orderbook.mid_price, orderbook.mid_price, 0.0, 0.0, rationale="outside_entry_window")
        if time_to_resolution_sec < candidate_min_time_to_resolution_sec:
            return SignalOutput("H7", market_id, None, orderbook.mid_price, orderbook.mid_price, 0.0, 0.0, rationale="too_close_to_resolution")
        if candidate_target_tolerance_sec > 0 and candidate_target_time_to_resolution_sec > 0:
            if abs(time_to_resolution_sec - candidate_target_time_to_resolution_sec) > candidate_target_tolerance_sec:
                return SignalOutput("H7", market_id, None, orderbook.mid_price, orderbook.mid_price, 0.0, 0.0, rationale="outside_entry_window")
        if orderbook.spread > max_spread:
            return SignalOutput("H7", market_id, None, orderbook.mid_price, orderbook.mid_price, 0.0, 0.0, rationale="spread_too_wide")

        learned_side = str(data.get("crypto15m_model_side") or "").upper()
        learned_label = str(data.get("crypto15m_model_label") or "").upper()
        learned_confidence = float(data.get("crypto15m_model_confidence", 0.0))
        no_trade_probability = float(data.get("crypto15m_model_no_trade_probability", 0.0))
        fallback_from_no_trade = False
        composite_momentum = _crypto15m_composite_momentum(data)
        fallback_metadata = {
            "model_label": learned_label,
            "model_confidence": learned_confidence,
            "model_no_trade_probability": no_trade_probability,
            "momentum_1m": float(data.get("crypto_ret_1m", data.get("ret_1m", 0.0))),
            "momentum_3m": float(data.get("crypto_ret_3m", data.get("ret_3m", 0.0))),
            "momentum_5m": float(data.get("crypto_ret_5m", data.get("ret_5m", data.get("price_return_60m", 0.0)))),
            "momentum_15m": float(data.get("crypto_ret_15m", data.get("ret_15m", 0.0))),
            "composite_momentum": composite_momentum,
            "return_zscore_15m": float(data.get("return_zscore_15m", 0.0)),
            "trend_consistency_15m": float(data.get("trend_consistency_15m", 0.5)),
        }
        if bool(data.get("crypto15m_use_learned_gate")) and learned_label and learned_label not in {"YES", "NO"}:
            if (
                learned_label == "NO_TRADE"
                and allow_no_trade_fallback
                and no_trade_probability <= no_trade_fallback_max_probability
                and not bool(data.get("crypto_ohlcv_stale", False))
            ):
                fallback_from_no_trade = True
            else:
                return SignalOutput(
                    "H7",
                    market_id,
                    None,
                    orderbook.mid_price,
                    orderbook.mid_price,
                    0.0,
                    max(learned_confidence, no_trade_probability),
                    rationale=f"model_{learned_label.lower()}",
                    metadata={
                        **fallback_metadata,
                        "no_trade_fallback_allowed": allow_no_trade_fallback,
                        "no_trade_fallback_max_probability": no_trade_fallback_max_probability,
                    },
                )
        if learned_side in {"YES", "NO"}:
            if bool(data.get("crypto_ohlcv_stale", False)):
                return SignalOutput(
                    "H7",
                    market_id,
                    None,
                    orderbook.mid_price,
                    orderbook.mid_price,
                    0.0,
                    learned_confidence,
                    rationale="crypto_ohlcv_stale",
                )
            if learned_confidence < min_confidence:
                return SignalOutput(
                    "H7",
                    market_id,
                    None,
                    orderbook.mid_price,
                    orderbook.mid_price,
                    0.0,
                    learned_confidence,
                    rationale="model_uncalibrated",
                )
            yes_probability_raw = data.get("crypto15m_model_yes_probability")
            if yes_probability_raw is None:
                fallback_ev = float(data.get("crypto15m_model_net_ev", 0.0))
                yes_probability = (
                    min(0.99, orderbook.mid_price + fallback_ev)
                    if learned_side == "YES"
                    else max(0.01, orderbook.mid_price - fallback_ev)
                )
            else:
                yes_probability = max(0.01, min(0.99, float(yes_probability_raw)))

            entry_price = orderbook.best_ask if learned_side == "YES" else _resolve_no_entry_price(data, orderbook)
            if entry_price <= 0 or entry_price >= 1:
                return SignalOutput(
                    "H7",
                    market_id,
                    None,
                    orderbook.mid_price,
                    orderbook.mid_price,
                    0.0,
                    learned_confidence,
                    rationale="missing_entry_price",
                )
            if entry_price < min_entry_price:
                return SignalOutput(
                    "H7",
                    market_id,
                    None,
                    orderbook.mid_price,
                    orderbook.mid_price,
                    0.0,
                    learned_confidence,
                    rationale="entry_price_too_low",
                    metadata={
                        **fallback_metadata,
                        "entry_price": entry_price,
                        "min_entry_price": min_entry_price,
                    },
                )
            gate_reason = crypto15m_side_gate_reason(
                {
                    "yes_entry_price": orderbook.best_ask,
                    "no_entry_price": _resolve_no_entry_price(data, orderbook),
                    "return_zscore_15m": float(data.get("return_zscore_15m", 0.0)),
                    "trend_consistency_15m": float(data.get("trend_consistency_15m", 0.5)),
                },
                learned_side,
                max_entry_price=max_entry_price,
                min_abs_return_zscore_15m=min_abs_return_zscore_15m,
                min_trend_consistency_15m=min_trend_consistency_15m,
            )
            if gate_reason is not None:
                if relax_regime_gates and gate_reason in {"btc_zscore_too_small", "btc_trend_inconsistent"}:
                    gate_reason = None
            if gate_reason is not None:
                return SignalOutput(
                    "H7",
                    market_id,
                    None,
                    orderbook.mid_price,
                    orderbook.mid_price,
                    0.0,
                    learned_confidence,
                    rationale=gate_reason,
                )
            side_win_probability = yes_probability if learned_side == "YES" else (1.0 - yes_probability)
            learned_net_ev = net_ev_per_share(
                win_probability=side_win_probability,
                entry_price=entry_price,
                fee_rate=float(data.get("fee_rate", 0.02)),
                slippage=float(data.get("estimated_slippage", 0.0)),
            )
            if learned_net_ev < min_net_ev:
                return SignalOutput(
                    "H7",
                    market_id,
                    None,
                    orderbook.mid_price,
                    orderbook.mid_price,
                    max(0.0, learned_net_ev),
                    learned_confidence,
                    rationale="fee_adjusted_ev_negative",
                )
            return SignalOutput(
                hypothesis_id="H7",
                market_id=market_id,
                side=learned_side,  # type: ignore[arg-type]
                model_probability=yes_probability,
                market_probability=orderbook.mid_price,
                edge=learned_net_ev,
                confidence=learned_confidence,
                entry_price=entry_price,
                order_ttl_sec=30,
                rationale=f"crypto15m learned {learned_side} ev={learned_net_ev:.4f}",
                metadata={
                    "threshold": min_confidence,
                    "model_yes_probability": yes_probability,
                    "model_no_probability": 1.0 - yes_probability,
                    "model_yes_class_probability": float(data.get("crypto15m_model_yes_class_probability", 0.0)),
                    "model_no_class_probability": float(data.get("crypto15m_model_no_class_probability", 0.0)),
                    "model_no_trade_probability": float(data.get("crypto15m_model_no_trade_probability", 0.0)),
                    "expected_net_ev": learned_net_ev,
                    "entry_price": entry_price,
                },
            )

        momentum = composite_momentum
        threshold = float(data.get("crypto15m_momentum_threshold", 0.003))
        if relax_momentum_gate:
            threshold *= 0.5
        if abs(momentum) < threshold:
            return SignalOutput(
                "H7",
                market_id,
                None,
                orderbook.mid_price,
                orderbook.mid_price,
                abs(momentum),
                0.0,
                rationale="momentum_too_small",
                metadata=fallback_metadata if fallback_from_no_trade else {},
            )

        side: Literal["YES", "NO"] = "YES" if momentum > 0 else "NO"
        gate_reason = crypto15m_side_gate_reason(
            {
                "yes_entry_price": orderbook.best_ask,
                "no_entry_price": _resolve_no_entry_price(data, orderbook),
                "return_zscore_15m": float(data.get("return_zscore_15m", 0.0)),
                "trend_consistency_15m": float(data.get("trend_consistency_15m", 0.5)),
            },
            side,
            max_entry_price=max_entry_price,
            min_abs_return_zscore_15m=min_abs_return_zscore_15m,
            min_trend_consistency_15m=min_trend_consistency_15m,
        )
        if gate_reason is not None:
            if relax_regime_gates and gate_reason in {"btc_zscore_too_small", "btc_trend_inconsistent"}:
                gate_reason = None
        if gate_reason is not None:
            return SignalOutput(
                "H7",
                market_id,
                None,
                orderbook.mid_price,
                orderbook.mid_price,
                0.0,
                max(learned_confidence, no_trade_probability) if fallback_from_no_trade else 0.0,
                rationale=gate_reason,
                metadata=fallback_metadata if fallback_from_no_trade else {},
            )
        entry_price = orderbook.best_ask if side == "YES" else _resolve_no_entry_price(data, orderbook)
        if entry_price <= 0 or entry_price >= 1:
            return SignalOutput(
                "H7",
                market_id,
                None,
                orderbook.mid_price,
                orderbook.mid_price,
                0.0,
                0.0,
                rationale="missing_entry_price",
                metadata=fallback_metadata if fallback_from_no_trade else {},
            )
        if entry_price < min_entry_price:
            return SignalOutput(
                "H7",
                market_id,
                None,
                orderbook.mid_price,
                orderbook.mid_price,
                0.0,
                confidence if "confidence" in locals() else 0.0,
                rationale="entry_price_too_low",
                metadata={
                    **(fallback_metadata if fallback_from_no_trade else {}),
                    "entry_price": entry_price,
                    "min_entry_price": min_entry_price,
                },
            )

        zscore = abs(float(data.get("return_zscore_15m", 0.0)))
        trend_consistency = float(data.get("trend_consistency_15m", 0.5))
        confidence = min(
            0.95,
            0.46
            + abs(momentum) * 22.0
            + min(zscore, 3.0) * 0.08
            + max(0.0, trend_consistency - 0.5) * 0.35,
        )
        if fallback_from_no_trade:
            confidence = min(
                0.92,
                confidence + 0.03,
            )
        if confidence < min_confidence:
            return SignalOutput(
                "H7",
                market_id,
                None,
                orderbook.mid_price,
                orderbook.mid_price,
                abs(momentum),
                confidence,
                rationale="low_confidence",
                metadata=fallback_metadata if fallback_from_no_trade else {},
            )
        directional_edge_prob = min(
            0.10,
            abs(momentum) * 4.0
            + max(0.0, zscore - min_abs_return_zscore_15m) * 0.015
            + max(0.0, trend_consistency - min_trend_consistency_15m) * 0.08,
        )
        model_probability = (
            min(0.99, orderbook.mid_price + directional_edge_prob)
            if side == "YES"
            else max(0.01, orderbook.mid_price - directional_edge_prob)
        )
        side_win_probability = model_probability if side == "YES" else (1.0 - model_probability)
        edge = net_ev_per_share(
            win_probability=side_win_probability,
            entry_price=entry_price,
            fee_rate=float(data.get("fee_rate", 0.02)),
            slippage=float(data.get("estimated_slippage", 0.0)),
        )
        if edge < min_net_ev:
            return SignalOutput(
                "H7",
                market_id,
                None,
                orderbook.mid_price,
                orderbook.mid_price,
                max(0.0, edge),
                confidence,
                rationale="fee_adjusted_ev_negative",
                metadata=fallback_metadata if fallback_from_no_trade else {},
            )

        return SignalOutput(
            hypothesis_id="H7",
            market_id=market_id,
            side=side,
            model_probability=model_probability,
            market_probability=orderbook.mid_price,
            edge=edge,
            confidence=confidence,
            entry_price=entry_price,
            order_ttl_sec=30,
            rationale=(
                f"crypto15m {'fallback ' if fallback_from_no_trade else ''}momentum "
                f"{side} ret5m={momentum:.4f}"
            ),
            metadata={
                **(fallback_metadata if fallback_from_no_trade else {}),
                "threshold": min_confidence,
                "expected_net_ev": edge,
                "entry_price": entry_price,
                "fallback_from_model_no_trade": fallback_from_no_trade,
                "return_zscore_15m": zscore,
                "trend_consistency_15m": trend_consistency,
            },
        )

    def train(self, historical_data: pd.DataFrame) -> None:
        return
