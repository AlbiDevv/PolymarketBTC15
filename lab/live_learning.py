from __future__ import annotations

import json
import pickle
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from config import Settings
from exchange_client.base import Market, Orderbook
from research.trade_costs import net_ev_per_share


FEATURE_COLUMNS = [
    "market_yes",
    "entry_price",
    "price_return_60m",
    "price_range_60m",
    "volatility_60m",
    "volume_24h",
    "liquidity",
    "samples_pre",
    "extreme_yes_share",
    "extreme_no_share",
    "pre_event_window_minutes",
    "time_to_resolution_sec",
    "category_prior",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _prepare_runtime_model(model: Any) -> Any:
    n_jobs = getattr(model, "n_jobs", None)
    if isinstance(n_jobs, int) and n_jobs != 1:
        try:
            model.n_jobs = 1
        except Exception:
            pass
    return model


def _predict_proba_safely(model: Any, features: pd.DataFrame) -> Any:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"sklearn\.utils\.parallel\.delayed should be used with sklearn\.utils\.parallel\.Parallel.*",
            category=UserWarning,
        )
        try:
            return model.predict_proba(features)
        except (AttributeError, TypeError, ValueError):
            return model.predict_proba(features.to_numpy())


@dataclass
class LearnedGateDecision:
    enabled: bool
    accepted_artifact: bool
    predicted_yes_probability: float
    candidate_confidence: float
    should_veto: bool
    expected_net_ev: float = 0.0
    entry_price: float = 0.0
    reason: str = ""


class LearnedModelGate:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._loaded_at: datetime | None = None
        self._manifest: dict[str, Any] | None = None
        self._bundle: dict[str, Any] | None = None

    def _manifest_path(self) -> Path:
        return Path(self._settings.strategy.learned_model.artifact_path)

    def _reload_needed(self) -> bool:
        if self._loaded_at is None:
            return True
        age = (_utcnow() - self._loaded_at).total_seconds()
        return age >= self._settings.strategy.learned_model.reload_interval_sec

    def _load(self):
        manifest_path = self._manifest_path()
        if not manifest_path.exists():
            self._manifest = None
            self._bundle = None
            self._loaded_at = _utcnow()
            return
        with open(manifest_path, "r", encoding="utf-8") as fh:
            self._manifest = json.load(fh)
        model_path = Path(self._manifest.get("model_path") or "")
        if model_path.exists():
            with open(model_path, "rb") as fh:
                self._bundle = pickle.load(fh)
            if isinstance(self._bundle, dict) and "model" in self._bundle:
                self._bundle["model"] = _prepare_runtime_model(self._bundle["model"])
        else:
            self._bundle = None
        self._loaded_at = _utcnow()

    def _ensure_loaded(self):
        if self._reload_needed():
            self._load()

    def feature_vector(
        self,
        market: Market,
        yes_orderbook: Orderbook,
        no_orderbook: Orderbook,
        external_data: dict[str, Any] | None,
    ) -> dict[str, float]:
        external = external_data or {}
        category_priors = (self._bundle or {}).get("category_priors", {})
        category = market.category or "unknown"
        yes_mid = float(external.get("yes_mid", yes_orderbook.mid_price))
        entry_price = float(external.get("entry_price", yes_mid))
        return {
            "market_yes": yes_mid,
            "entry_price": entry_price,
            "price_return_60m": float(external.get("price_return_60m", 0.0)),
            "price_range_60m": float(external.get("price_range_60m", 0.0)),
            "volatility_60m": float(external.get("volatility_60m", 0.0)),
            "volume_24h": float(market.volume_24h or 0.0),
            "liquidity": float(external.get("liquidity", market.volume_24h or 0.0)),
            "samples_pre": float(external.get("samples_pre", 0.0)),
            "extreme_yes_share": float(external.get("extreme_yes_share", 1.0 if yes_mid >= 0.92 else 0.0)),
            "extreme_no_share": float(external.get("extreme_no_share", 1.0 if yes_mid <= 0.08 else 0.0)),
            "pre_event_window_minutes": float(external.get("pre_event_window_minutes", 60.0)),
            "time_to_resolution_sec": float(external.get("time_to_resolution_sec", 0.0)),
            "category_prior": float(category_priors.get(category, 0.5)),
        }

    def score_candidate(
        self,
        market: Market,
        yes_orderbook: Orderbook,
        no_orderbook: Orderbook,
        *,
        side: str,
        market_probability: float,
        external_data: dict[str, Any] | None = None,
    ) -> LearnedGateDecision:
        if not self._settings.strategy.learned_model.enabled:
            return LearnedGateDecision(False, False, market_probability, 0.0, False)

        self._ensure_loaded()
        if not self._manifest or not self._bundle:
            return LearnedGateDecision(False, False, market_probability, 0.0, False, reason="artifact_missing")

        accepted = bool(self._manifest.get("accepted"))
        if self._settings.strategy.learned_model.require_accepted_artifact and not accepted:
            return LearnedGateDecision(True, False, market_probability, 0.0, True, reason="artifact_not_accepted")
        fresh_until = self._manifest.get("training_fresh_until")
        if fresh_until:
            try:
                normalized = fresh_until[:-1] + "+00:00" if str(fresh_until).endswith("Z") else str(fresh_until)
                if datetime.fromisoformat(normalized).astimezone(timezone.utc) < _utcnow():
                    return LearnedGateDecision(True, accepted, market_probability, 0.0, True, reason="artifact_stale")
            except ValueError:
                pass

        vector = self.feature_vector(market, yes_orderbook, no_orderbook, external_data)
        columns = self._bundle.get("feature_columns") or FEATURE_COLUMNS
        features = pd.DataFrame(
            [[float(vector.get(column, 0.0)) for column in columns]],
            columns=columns,
        )
        predicted_yes = float(_predict_proba_safely(self._bundle["model"], features)[0][1])
        candidate_confidence = max(predicted_yes, 1.0 - predicted_yes)
        if side == "YES":
            entry_price = yes_orderbook.best_ask if yes_orderbook.best_ask > 0 else market_probability
            win_probability = predicted_yes
        else:
            entry_price = no_orderbook.best_ask if no_orderbook.best_ask > 0 else 1.0 - market_probability
            win_probability = 1.0 - predicted_yes
        entry_price = max(0.0, min(1.0, float(entry_price)))
        expected_net_ev = net_ev_per_share(
            win_probability=win_probability,
            entry_price=entry_price,
            fee_rate=float(self._settings.strategy.fee_rate),
            fees_enabled=True,
            slippage=float(self._settings.strategy.learned_model.estimated_slippage),
        )

        threshold = float(self._manifest.get("high_conf_threshold") or self._settings.strategy.learned_model.min_candidate_confidence)
        if candidate_confidence < threshold:
            return LearnedGateDecision(True, accepted, predicted_yes, candidate_confidence, True, expected_net_ev, entry_price, reason="low_model_confidence")
        max_entry_price = float(self._manifest.get("max_candidate_entry_price") or self._settings.strategy.learned_model.max_candidate_entry_price)
        if entry_price > max_entry_price:
            return LearnedGateDecision(True, accepted, predicted_yes, candidate_confidence, True, expected_net_ev, entry_price, reason="price_too_late")
        min_net_ev = float(self._manifest.get("min_candidate_net_ev") or self._settings.strategy.learned_model.min_candidate_net_ev)
        if expected_net_ev < min_net_ev:
            return LearnedGateDecision(True, accepted, predicted_yes, candidate_confidence, True, expected_net_ev, entry_price, reason="fee_adjusted_ev_negative")

        veto_margin = self._settings.strategy.learned_model.veto_margin
        if side == "YES" and predicted_yes < (market_probability + veto_margin):
            return LearnedGateDecision(True, accepted, predicted_yes, candidate_confidence, True, expected_net_ev, entry_price, reason="model_below_market_yes")
        if side == "NO" and (1.0 - predicted_yes) < ((1.0 - market_probability) + veto_margin):
            return LearnedGateDecision(True, accepted, predicted_yes, candidate_confidence, True, expected_net_ev, entry_price, reason="model_below_market_no")
        return LearnedGateDecision(True, accepted, predicted_yes, candidate_confidence, False, expected_net_ev, entry_price)
