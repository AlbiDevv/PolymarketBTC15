from __future__ import annotations

import json
import math
import pickle
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from sklearn.linear_model import LogisticRegression  # type: ignore
    from sklearn.metrics import accuracy_score, brier_score_loss  # type: ignore
    from sklearn.pipeline import make_pipeline  # type: ignore
    from sklearn.preprocessing import StandardScaler  # type: ignore
except Exception:  # pragma: no cover - optional dependency fallback
    class LogisticRegression:  # type: ignore
        def __init__(self, max_iter: int = 1000):
            self.max_iter = max_iter
            self._pos_mean = None
            self._neg_mean = None

        def fit(self, x, y):
            x_arr = np.asarray(x, dtype=float)
            y_arr = np.asarray(y, dtype=int)
            pos = x_arr[y_arr == 1]
            neg = x_arr[y_arr == 0]
            self._pos_mean = pos.mean(axis=0) if len(pos) else np.zeros(x_arr.shape[1])
            self._neg_mean = neg.mean(axis=0) if len(neg) else np.zeros(x_arr.shape[1])
            return self

        def predict_proba(self, x):
            x_arr = np.asarray(x, dtype=float)
            pos_dist = np.linalg.norm(x_arr - self._pos_mean, axis=1)
            neg_dist = np.linalg.norm(x_arr - self._neg_mean, axis=1)
            score = 1.0 / (1.0 + np.exp(pos_dist - neg_dist))
            return np.column_stack([1.0 - score, score])

    def accuracy_score(y_true, y_pred):  # type: ignore
        y_true_arr = np.asarray(y_true)
        y_pred_arr = np.asarray(y_pred)
        return float((y_true_arr == y_pred_arr).mean()) if len(y_true_arr) else 0.0

    def brier_score_loss(y_true, y_prob):  # type: ignore
        y_true_arr = np.asarray(y_true, dtype=float)
        y_prob_arr = np.asarray(y_prob, dtype=float)
        return float(np.mean((y_prob_arr - y_true_arr) ** 2)) if len(y_true_arr) else 0.0

    class StandardScaler:  # type: ignore
        def fit(self, x, y=None):
            return self

        def transform(self, x):
            return x

    def make_pipeline(_scaler, model):  # type: ignore
        return model

from historical import append_manifest, read_dataset
from research.chroma_store import MotifStore
from research.trade_costs import coerce_bool, coerce_fee_rate, net_ev_per_share, polymarket_taker_fee_per_share


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    rendered = str(value).strip()
    if not rendered:
        return None
    if rendered.endswith("Z"):
        rendered = rendered[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(rendered).astimezone(timezone.utc)
    except ValueError:
        return None


def _to_price_dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        numeric = int(float(value))
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except (TypeError, ValueError):
        return _to_dt(value)


@dataclass
class PublishedArtifact:
    artifact_key: str
    manifest_path: Path
    model_path: Path
    accepted: bool
    metrics: dict[str, Any]
    holdouts: list[dict[str, Any]]
    motifs: list[dict[str, Any]]
    verdict: dict[str, Any]


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, "", "nan"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _ev_for_direction(
    predicted_yes: np.ndarray,
    market_yes: np.ndarray,
    outcome_yes: np.ndarray,
    fee_rate: float | np.ndarray,
    *,
    fees_enabled: np.ndarray | None = None,
    slippage: float = 0.0,
) -> np.ndarray:
    direction_yes = predicted_yes >= 0.5
    buy_price = np.where(direction_yes, market_yes, 1.0 - market_yes)
    realized = np.where(direction_yes, outcome_yes, 1.0 - outcome_yes)
    if fees_enabled is None:
        fees_enabled = np.ones(len(buy_price), dtype=bool)
    fee_rates = np.asarray(fee_rate, dtype=float)
    if fee_rates.ndim == 0:
        fee_rates = np.full(len(buy_price), float(fee_rates))
    fee = np.array([
        polymarket_taker_fee_per_share(price, fee_rate=rate, fees_enabled=enabled)
        for price, rate, enabled in zip(buy_price, fee_rates, fees_enabled)
    ])
    return realized - buy_price - fee - max(0.0, float(slippage))


def _candidate_points(pre_rows: pd.DataFrame, *, stride_sec: int) -> pd.DataFrame:
    if pre_rows.empty:
        return pre_rows
    selected = []
    next_ts: datetime | None = None
    for _, row in pre_rows.sort_values("timestamp").iterrows():
        ts = row["timestamp"]
        if next_ts is None or ts >= next_ts:
            selected.append(row)
            next_ts = ts + timedelta(seconds=stride_sec)
    if selected and selected[-1]["timestamp"] != pre_rows.iloc[-1]["timestamp"]:
        selected.append(pre_rows.iloc[-1])
    return pd.DataFrame(selected)


def build_learning_frame(lake_dir: str | Path, *, candidate_stride_sec: int = 300) -> pd.DataFrame:
    resolutions = read_dataset(lake_dir, "resolutions")
    windows = read_dataset(lake_dir, "price_windows")
    if resolutions.empty or windows.empty:
        return pd.DataFrame()

    if "market_id" in resolutions.columns:
        if "standard_binary_pair" in resolutions.columns:
            resolutions["_standard_known"] = resolutions["standard_binary_pair"].notna()
            resolutions = resolutions.sort_values("_standard_known")
        resolutions = resolutions.drop_duplicates(subset=["market_id"], keep="last")
    if {"market_id", "side", "timestamp", "price"}.issubset(set(windows.columns)):
        windows = windows.drop_duplicates(subset=["market_id", "side", "timestamp", "price"], keep="last")

    if "timestamp" in windows.columns:
        windows["timestamp"] = windows["timestamp"].map(_to_price_dt)
    if "settled_at" in resolutions.columns:
        resolutions["settled_at"] = resolutions["settled_at"].map(_to_dt)
    if "end_date" in resolutions.columns:
        resolutions["end_date"] = resolutions["end_date"].map(_to_dt)

    rows: list[dict[str, Any]] = []
    for market_id, market_rows in windows.groupby("market_id"):
        resolution_rows = resolutions[resolutions["market_id"] == market_id]
        if resolution_rows.empty:
            continue
        resolution = resolution_rows.iloc[0]
        if "standard_binary_pair" in resolution.index and not coerce_bool(resolution.get("standard_binary_pair"), default=True):
            continue
        settled_at = resolution.get("settled_at") or resolution.get("end_date")
        if settled_at is None:
            continue

        yes_rows = market_rows[market_rows["side"] == "YES"].sort_values("timestamp")
        if yes_rows.empty:
            continue
        pre_rows = yes_rows[yes_rows["timestamp"] <= settled_at]
        if pre_rows.empty:
            continue
        settled_outcome = str(resolution.get("outcome") or "").upper()
        label_yes = 1 if settled_outcome == "YES" else 0
        category = str(resolution.get("category") or "unknown")
        fees_enabled = coerce_bool(resolution.get("fees_enabled"), default=True)
        fee_rate = coerce_fee_rate(resolution.get("fee_rate"), default=0.02)

        for _, candidate in _candidate_points(pre_rows, stride_sec=candidate_stride_sec).iterrows():
            candidate_ts = candidate["timestamp"]
            trailing = pre_rows[
                (pre_rows["timestamp"] <= candidate_ts)
                & (pre_rows["timestamp"] >= candidate_ts - timedelta(minutes=60))
            ]
            if trailing.empty:
                continue
            prices = trailing["price"].astype(float)
            returns = prices.diff().dropna()
            market_yes = float(candidate["price"])
            direction_yes = market_yes >= 0.5
            oracle_entry_price = market_yes if direction_yes else 1.0 - market_yes
            oracle_win_probability = label_yes if direction_yes else 1.0 - label_yes
            oracle_net_pnl = net_ev_per_share(
                win_probability=oracle_win_probability,
                entry_price=oracle_entry_price,
                fee_rate=fee_rate,
                fees_enabled=fees_enabled,
            )

            pre_window_minutes = max(1.0, (candidate_ts - trailing.iloc[0]["timestamp"]).total_seconds() / 60.0)
            time_to_resolution_sec = max(0.0, (settled_at - candidate_ts).total_seconds())
            extreme_yes = (prices >= 0.60).mean()
            extreme_no = (prices <= 0.40).mean()

            rows.append({
                "market_id": market_id,
                "event_id": resolution.get("event_id"),
                "question": resolution.get("question"),
                "category": category,
                "tags": resolution.get("tags") or [],
                "settled_at": settled_at,
                "candidate_ts": candidate_ts,
                "market_yes": market_yes,
                "entry_price": oracle_entry_price,
                "candidate_side": "YES" if direction_yes else "NO",
                "candidate_fee": polymarket_taker_fee_per_share(
                    oracle_entry_price,
                    fee_rate=fee_rate,
                    fees_enabled=fees_enabled,
                ),
                "candidate_net_pnl": float(oracle_net_pnl),
                "candidate_net_ev": float(oracle_net_pnl),
                "would_fill": bool(oracle_entry_price < 1.0),
                "hold_minutes": float(time_to_resolution_sec / 60.0),
                "exit_reason": "settlement",
                "fee_rate": fee_rate,
                "fees_enabled": fees_enabled,
                "price_return_60m": float(prices.iloc[-1] - prices.iloc[0]),
                "price_range_60m": float(prices.max() - prices.min()),
                "volatility_60m": float(returns.std()) if not returns.empty else 0.0,
                "volume_24h": _safe_float(resolution.get("volume_24h")),
                "liquidity": _safe_float(resolution.get("liquidity")),
                "samples_pre": int(len(trailing)),
                "extreme_yes_share": float(extreme_yes),
                "extreme_no_share": float(extreme_no),
                "pre_event_window_minutes": pre_window_minutes,
                "time_to_resolution_sec": time_to_resolution_sec,
                "outcome_yes": label_yes,
            })
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    category_rates = frame.groupby("category")["outcome_yes"].mean().to_dict()
    frame["category_prior"] = frame["category"].map(lambda value: category_rates.get(value, 0.5))
    frame["late_stage_high_conf_flag"] = (
        ((frame["market_yes"] >= 0.60) | (frame["market_yes"] <= 0.40))
        & ((frame["extreme_yes_share"] >= 0.5) | (frame["extreme_no_share"] >= 0.5))
        & (frame["entry_price"] <= 0.995)
    )
    return frame.sort_values("settled_at").reset_index(drop=True)


def frame_readiness(
    frame: pd.DataFrame,
    *,
    min_markets_required: int,
    min_coverage_days: int,
) -> dict[str, Any]:
    if frame.empty or "settled_at" not in frame.columns:
        return {
            "ready": False,
            "reason": "historical_lake_empty",
            "markets_used": 0,
            "coverage_days": 0,
        }
    settled = frame["settled_at"].dropna()
    if settled.empty:
        return {
            "ready": False,
            "reason": "missing_settlement_timestamps",
            "markets_used": int(frame["market_id"].nunique()) if "market_id" in frame.columns else int(len(frame)),
            "coverage_days": 0,
        }
    coverage_days = max(0, int((settled.max() - settled.min()).days))
    markets_used = int(frame["market_id"].nunique()) if "market_id" in frame.columns else int(len(frame))
    failures: list[str] = []
    if markets_used < min_markets_required:
        failures.append("min_markets_required")
    if coverage_days < min_coverage_days:
        failures.append("min_coverage_days")
    return {
        "ready": not failures,
        "reason": "ready" if not failures else ",".join(failures),
        "markets_used": markets_used,
        "coverage_days": coverage_days,
    }


def split_holdouts(frame: pd.DataFrame, *, window_days: int, windows_count: int) -> tuple[pd.DataFrame, list[pd.DataFrame]]:
    if frame.empty:
        return frame, []
    latest = frame["settled_at"].max()
    holdouts: list[pd.DataFrame] = []
    cursor_end = latest
    for _ in range(windows_count):
        cursor_start = cursor_end - timedelta(days=window_days)
        window = frame[(frame["settled_at"] > cursor_start) & (frame["settled_at"] <= cursor_end)]
        if not window.empty:
            holdouts.append(window.copy())
        cursor_end = cursor_start
    train = frame[frame["settled_at"] <= cursor_end].copy()
    return train, list(reversed(holdouts))


def _feature_columns() -> list[str]:
    return [
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


def train_model(train: pd.DataFrame) -> tuple[LogisticRegression, dict[str, float]]:
    if train.empty:
        raise ValueError("training frame is empty")
    features = train[_feature_columns()].fillna(0.0)
    labels = train["outcome_yes"].astype(int)
    model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000))
    model.fit(features, labels)
    priors = train.groupby("category")["outcome_yes"].mean().to_dict()
    return model, {str(key): float(value) for key, value in priors.items()}


def evaluate_holdout(
    model: LogisticRegression,
    holdout: pd.DataFrame,
    *,
    high_conf_threshold: float,
    fee_rate: float,
    min_candidate_net_ev: float = 0.0,
    max_candidate_entry_price: float = 1.0,
    estimated_slippage: float = 0.0,
) -> dict[str, Any]:
    features = holdout[_feature_columns()].fillna(0.0)
    probs = model.predict_proba(features)[:, 1]
    labels = holdout["outcome_yes"].astype(int).to_numpy()
    preds = (probs >= 0.5).astype(int)
    model_conf_mask = (probs >= high_conf_threshold) | (probs <= (1.0 - high_conf_threshold))
    if "late_stage_high_conf_flag" in holdout.columns:
        late_stage_mask = holdout["late_stage_high_conf_flag"].astype(bool).to_numpy()
    else:
        late_stage_mask = np.ones(len(holdout), dtype=bool)
    market_yes = holdout["market_yes"].to_numpy(dtype=float)
    buy_prices = np.where(preds == 1, market_yes, 1.0 - market_yes)
    if "fee_rate" in holdout.columns:
        fee_rates = holdout["fee_rate"].map(lambda value: coerce_fee_rate(value, default=fee_rate)).to_numpy(dtype=float)
    else:
        fee_rates = np.full(len(holdout), float(fee_rate))
    if "fees_enabled" in holdout.columns:
        fees_enabled = holdout["fees_enabled"].map(lambda value: coerce_bool(value, default=True)).to_numpy(dtype=bool)
    else:
        fees_enabled = np.ones(len(holdout), dtype=bool)
    expected_win_probability = np.where(preds == 1, probs, 1.0 - probs)
    expected_net_ev = np.array([
        net_ev_per_share(
            win_probability=prob,
            entry_price=price,
            fee_rate=rate,
            fees_enabled=enabled,
            slippage=estimated_slippage,
        )
        for prob, price, rate, enabled in zip(expected_win_probability, buy_prices, fee_rates, fees_enabled)
    ])
    tradable_mask = (expected_net_ev >= min_candidate_net_ev) & (buy_prices <= max_candidate_entry_price)
    high_conf_mask = model_conf_mask & late_stage_mask & tradable_mask
    if high_conf_mask.any():
        high_conf_accuracy = accuracy_score(labels[high_conf_mask], preds[high_conf_mask])
        high_conf_ev = float(_ev_for_direction(
            probs[high_conf_mask],
            market_yes[high_conf_mask],
            labels[high_conf_mask],
            fee_rates[high_conf_mask],
            fees_enabled=fees_enabled[high_conf_mask],
            slippage=estimated_slippage,
        ).mean())
    else:
        high_conf_accuracy = 0.0
        high_conf_ev = 0.0
    calibration_error = float(brier_score_loss(labels, probs))
    return {
        "rows": int(len(holdout)),
        "accuracy": float(accuracy_score(labels, preds)),
        "high_conf_accuracy": float(high_conf_accuracy),
        "high_conf_count": int(high_conf_mask.sum()),
        "high_conf_ratio": float(high_conf_mask.mean()),
        "late_stage_count": int(late_stage_mask.sum()),
        "model_conf_count": int(model_conf_mask.sum()),
        "tradable_count": int(tradable_mask.sum()),
        "expected_net_ev_mean": float(expected_net_ev[high_conf_mask].mean()) if high_conf_mask.any() else 0.0,
        "avg_entry_price": float(buy_prices[high_conf_mask].mean()) if high_conf_mask.any() else 0.0,
        "high_conf_net_ev": high_conf_ev,
        "calibration_error": calibration_error,
        "from": holdout["settled_at"].min().isoformat(),
        "to": holdout["settled_at"].max().isoformat(),
    }


def extract_motifs(frame: pd.DataFrame, holdouts: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    if frame.empty:
        return []

    def _motif_key(row) -> str:
        if row["market_yes"] >= 0.92:
            zone = "yes_extreme"
        elif row["market_yes"] <= 0.08:
            zone = "no_extreme"
        else:
            zone = "mid"
        trend = "up" if row["price_return_60m"] >= 0 else "down"
        vol = "calm" if row["volatility_60m"] < 0.01 else "active"
        return f"{zone}:{trend}:{vol}:{row['category']}"

    working = frame.copy()
    working["motif_key"] = working.apply(_motif_key, axis=1)
    motifs: list[dict[str, Any]] = []
    for motif_key, subset in working.groupby("motif_key"):
        if len(subset) < 5:
            continue
        if "candidate_net_pnl" in subset.columns:
            expected_value = float(subset["candidate_net_pnl"].mean())
        else:
            expected_value = float(_ev_for_direction(subset["market_yes"].to_numpy(), subset["market_yes"].to_numpy(), subset["outcome_yes"].to_numpy(), 0.02).mean())
        motifs.append({
            "motif_key": motif_key,
            "feature_signature": {
                "category": str(subset.iloc[0]["category"]),
                "extreme_yes_share_avg": float(subset["extreme_yes_share"].mean()),
                "extreme_no_share_avg": float(subset["extreme_no_share"].mean()),
            },
            "pre_event_window": {
                "minutes_avg": float(subset["pre_event_window_minutes"].mean()),
            },
            "time_lag_sec": 0,
            "sample_size": int(len(subset)),
            "hit_rate": float(subset["outcome_yes"].mean()),
            "expected_value": expected_value,
            "confidence_score": float(min(0.99, 0.4 + (len(subset) / 50.0))),
            "holdout_metrics": {
                "windows": holdouts,
            },
        })
    motifs.sort(key=lambda item: (item["confidence_score"], item["expected_value"], item["sample_size"]), reverse=True)
    return motifs[:limit]


def publish_artifact(
    *,
    out_dir: str | Path,
    model: LogisticRegression,
    category_priors: dict[str, float],
    holdouts: list[dict[str, Any]],
    motifs: list[dict[str, Any]],
    high_conf_threshold: float,
    fee_rate: float,
    min_high_conf_accuracy: float,
    max_calibration_error: float,
    min_rows_per_holdout: int,
    min_high_conf_count_per_holdout: int,
    readiness: dict[str, Any] | None = None,
    min_candidate_net_ev: float = 0.0,
    max_candidate_entry_price: float = 1.0,
    estimated_slippage: float = 0.0,
) -> PublishedArtifact:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    artifact_key = _utcnow().strftime("pmotif_%Y%m%dT%H%M%SZ")
    model_path = root / f"{artifact_key}.pkl"
    manifest_path = root / f"{artifact_key}.json"

    with open(model_path, "wb") as fh:
        pickle.dump({
            "model": model,
            "feature_columns": _feature_columns(),
            "category_priors": category_priors,
        }, fh)

    high_conf_scores = [item["high_conf_accuracy"] for item in holdouts]
    ev_scores = [item["high_conf_net_ev"] for item in holdouts]
    calibration_scores = [item["calibration_error"] for item in holdouts]
    support_failures: list[str] = []
    for index, report in enumerate(holdouts, start=1):
        if int(report.get("rows") or 0) < min_rows_per_holdout:
            support_failures.append(f"holdout_{index}_rows")
        if int(report.get("high_conf_count") or 0) < min_high_conf_count_per_holdout:
            support_failures.append(f"holdout_{index}_high_conf_count")
        if float(report.get("high_conf_accuracy") or 0.0) < min_high_conf_accuracy:
            support_failures.append(f"holdout_{index}_high_conf_accuracy")
        if float(report.get("high_conf_net_ev") or 0.0) <= 0.0:
            support_failures.append(f"holdout_{index}_high_conf_net_ev")
        if float(report.get("calibration_error") or 0.0) > max_calibration_error:
            support_failures.append(f"holdout_{index}_calibration_error")
    readiness = readiness or {
        "ready": True,
        "reason": "ready",
        "markets_used": 0,
        "coverage_days": 0,
    }
    accepted = bool(holdouts and readiness.get("ready") and not support_failures)
    verdict = {
        "accepted": accepted,
        "reason": "accepted" if accepted else (readiness.get("reason") if not readiness.get("ready") else ",".join(support_failures) or "holdout_requirements_failed"),
        "mean_high_conf_net_ev": float(np.mean(ev_scores)) if ev_scores else 0.0,
        "mean_high_conf_accuracy": float(np.mean(high_conf_scores)) if high_conf_scores else 0.0,
        "coverage_days": int(readiness.get("coverage_days") or 0),
        "markets_used": int(readiness.get("markets_used") or 0),
        "failures": support_failures,
    }

    metrics = {
        "high_conf_threshold": high_conf_threshold,
        "fee_rate": fee_rate,
        "holdout_windows": len(holdouts),
        "min_high_conf_accuracy": min_high_conf_accuracy,
        "max_calibration_error": max_calibration_error,
        "min_rows_per_holdout": min_rows_per_holdout,
        "min_high_conf_count_per_holdout": min_high_conf_count_per_holdout,
        "min_candidate_net_ev": min_candidate_net_ev,
        "max_candidate_entry_price": max_candidate_entry_price,
        "estimated_slippage": estimated_slippage,
        "accepted": accepted,
        "mean_high_conf_accuracy": float(np.mean(high_conf_scores)) if high_conf_scores else 0.0,
        "mean_high_conf_net_ev": float(np.mean(ev_scores)) if ev_scores else 0.0,
        "mean_calibration_error": float(np.mean(calibration_scores)) if calibration_scores else 0.0,
        "verdict": verdict,
    }
    manifest = {
        "artifact_key": artifact_key,
        "created_at": _utcnow().isoformat(),
        "model_path": str(model_path),
        "feature_columns": _feature_columns(),
        "category_priors": category_priors,
        "high_conf_threshold": high_conf_threshold,
        "min_candidate_net_ev": min_candidate_net_ev,
        "max_candidate_entry_price": max_candidate_entry_price,
        "estimated_slippage": estimated_slippage,
        "metrics": metrics,
        "holdouts": holdouts,
        "motif_count": len(motifs),
        "accepted": accepted,
        "enabled": accepted,
        "training_fresh_until": (_utcnow() + timedelta(days=7)).isoformat(),
        "verdict": verdict,
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    latest = root / "latest_manifest.json"
    latest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    verdict_path = root / f"{artifact_key}.verdict.json"
    verdict_payload = {
        "artifact_key": artifact_key,
        **verdict,
    }
    verdict_path.write_text(json.dumps(verdict_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "latest_verdict.json").write_text(json.dumps(verdict_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    store = MotifStore(root / "chroma")
    store.upsert("causal_relationships", [
        {"id": f"{artifact_key}:{item['motif_key']}", **item}
        for item in motifs
    ])
    store.upsert("polymarket_market_events", [
        {"id": f"{artifact_key}:summary", "artifact_key": artifact_key, "holdouts": holdouts}
    ])

    return PublishedArtifact(
        artifact_key=artifact_key,
        manifest_path=manifest_path,
        model_path=model_path,
        accepted=accepted,
        metrics=metrics,
        holdouts=holdouts,
        motifs=motifs,
        verdict=verdict,
    )
