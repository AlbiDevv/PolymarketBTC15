from __future__ import annotations

import json
import math
import pickle
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from research.trade_costs import net_ev_per_share


CRYPTO15M_FEATURE_COLUMNS = [
    "ret_1m",
    "ret_3m",
    "ret_5m",
    "ret_15m",
    "ret_60m",
    "volatility_15m",
    "volatility_regime_60m",
    "volume_spike_15m",
    "volume_spike_5m",
    "candle_body_15m",
    "upper_wick_15m",
    "lower_wick_15m",
    "distance_to_15m_open",
    "distance_to_vwap_15m",
    "return_zscore_15m",
    "trend_consistency_15m",
    "time_to_resolution_sec",
    "poly_mid",
    "poly_spread",
    "poly_depth_bid",
    "poly_depth_ask",
    "poly_imbalance",
    "poly_return_5m",
]


@dataclass(frozen=True)
class CryptoMarketInfo:
    is_crypto15m: bool
    symbol: str = ""
    asset: str = ""
    timeframe_minutes: int = 0
    reason: str = ""


def _lower_words(*parts: Any) -> str:
    return " ".join(str(part or "").lower() for part in parts)


def classify_crypto_market(question: str, *, category: str = "", tags: Iterable[str] | None = None) -> CryptoMarketInfo:
    text = _lower_words(question, category, " ".join(tags or []))
    asset = ""
    if re.search(r"\b(bitcoin|btc)\b", text):
        asset = "BTC"
    elif re.search(r"\b(ethereum|ether|eth)\b", text):
        asset = "ETH"
    if not asset:
        return CryptoMarketInfo(False, reason="not_btc_eth")

    has_direction = bool(re.search(r"\b(up\s+or\s+down|up/down|higher|lower|above|below)\b", text))
    has_15m = bool(re.search(r"\b(15\s*minutes?|15m|15-minute|15 min)\b", text))
    has_1h = bool(re.search(r"\b(1\s*hour|1h|hourly|60\s*minutes?)\b", text))
    has_short_time_range = bool(re.search(r"\b\d{1,2}:\d{2}\s*(am|pm)?\s*[-–]\s*\d{1,2}:\d{2}\s*(am|pm)?\b", text))
    has_hour_time = bool(re.search(r"\b\d{1,2}\s*(am|pm)\s*(et|utc|gmt)\b", text))
    if not has_direction:
        return CryptoMarketInfo(False, asset=asset, symbol=f"{asset}/USDT", reason="not_direction_market")
    if has_15m or has_short_time_range:
        return CryptoMarketInfo(True, asset=asset, symbol=f"{asset}/USDT", timeframe_minutes=15)
    if has_1h or has_hour_time:
        return CryptoMarketInfo(True, asset=asset, symbol=f"{asset}/USDT", timeframe_minutes=60)
    return CryptoMarketInfo(False, asset=asset, symbol=f"{asset}/USDT", reason="unsupported_timeframe")


def classify_crypto15m_updown_market(question: str, *, category: str = "", tags: Iterable[str] | None = None) -> CryptoMarketInfo:
    info = classify_crypto_market(question, category=category, tags=tags)
    if not info.is_crypto15m:
        return info
    text = _lower_words(question, category, " ".join(tags or []))
    if not re.search(r"\b(up\s+or\s+down|up/down)\b", text):
        return CryptoMarketInfo(False, asset=info.asset, symbol=info.symbol, reason="not_updown_market")
    if info.timeframe_minutes != 15:
        return CryptoMarketInfo(False, asset=info.asset, symbol=info.symbol, reason="not_15m_updown_market")
    return info


def normalize_ohlcv_rows(
    rows: Iterable[Iterable[Any]],
    *,
    exchange_id: str,
    symbol: str,
    timeframe: str,
) -> pd.DataFrame:
    normalized = []
    for row in rows:
        values = list(row)
        if len(values) < 6:
            continue
        ts_ms = int(values[0])
        normalized.append({
            "timestamp": pd.to_datetime(ts_ms, unit="ms", utc=True),
            "timestamp_ms": ts_ms,
            "open": float(values[1]),
            "high": float(values[2]),
            "low": float(values[3]),
            "close": float(values[4]),
            "volume": float(values[5]),
            "exchange": exchange_id,
            "symbol": symbol,
            "timeframe": timeframe,
        })
    frame = pd.DataFrame(normalized)
    if frame.empty:
        return frame
    return frame.sort_values("timestamp").drop_duplicates(["symbol", "timeframe", "timestamp"]).reset_index(drop=True)


def _as_utc_ts(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def timestamp_ns(values: Any) -> pd.Series:
    dt = pd.to_datetime(values, utc=True, format="mixed")
    if isinstance(dt, pd.DatetimeIndex):
        dt = pd.Series(dt)
    return dt.dt.tz_convert("UTC").dt.tz_localize(None).astype("datetime64[ns]").astype("int64")


def _pct(current: float, previous: float) -> float:
    if previous <= 0:
        return 0.0
    return (current - previous) / previous


def _safe_ratio(numerator: float, denominator: float, default: float = 0.0) -> float:
    if abs(denominator) <= 1e-12:
        return default
    return numerator / denominator


def build_crypto_features(candles_1m: pd.DataFrame, *, at: Any, symbol: str) -> dict[str, float]:
    zero_features = {
        column: 0.0
        for column in CRYPTO15M_FEATURE_COLUMNS
        if column not in {"time_to_resolution_sec", "poly_mid", "poly_spread", "poly_depth_bid", "poly_depth_ask", "poly_imbalance", "poly_return_5m"}
    }
    if candles_1m.empty:
        return dict(zero_features)
    at_ts = _as_utc_ts(at)
    if (
        candles_1m.attrs.get("symbol") == symbol
        or ("symbol" in candles_1m.columns and not candles_1m.empty and candles_1m["symbol"].iloc[0] == symbol)
    ):
        rows = candles_1m
    else:
        rows = candles_1m[candles_1m["symbol"].eq(symbol)]
    if rows.empty:
        rows = candles_1m
    if "timeframe" in rows.columns:
        one_minute = rows[rows["timeframe"].eq("1m")]
        if not one_minute.empty:
            rows = one_minute
    if not pd.api.types.is_datetime64_any_dtype(rows["timestamp"]):
        rows = rows.copy()
        rows["timestamp"] = pd.to_datetime(rows["timestamp"], utc=True)
    if not rows["timestamp"].is_monotonic_increasing:
        rows = rows.sort_values("timestamp")
    ts_ns = rows["timestamp_ns"].to_numpy() if "timestamp_ns" in rows.columns else timestamp_ns(rows["timestamp"]).to_numpy()
    idx = int(np.searchsorted(ts_ns, at_ts.value, side="right"))
    rows = rows.iloc[max(0, idx - 90):idx]
    if rows.empty:
        return dict(zero_features)

    latest = rows.iloc[-1]
    latest_close = float(latest["close"])

    def close_n_minutes_ago(minutes: int) -> float:
        cutoff = at_ts - pd.Timedelta(minutes=minutes)
        prior = rows[rows["timestamp"] <= cutoff]
        if prior.empty:
            return float(rows.iloc[0]["open"])
        return float(prior.iloc[-1]["close"])

    last15 = rows.tail(15)
    last5 = rows.tail(5)
    returns = rows["close"].pct_change().tail(15).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    returns60 = rows["close"].pct_change().tail(60).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    candle_open = float(last15.iloc[0]["open"])
    high = float(last15["high"].max())
    low = float(last15["low"].min())
    volume_now = float(last15["volume"].sum())
    prev_volume = float(rows.iloc[:-15].tail(60)["volume"].mean() * 15) if len(rows) > 15 else volume_now
    volume_now_5 = float(last5["volume"].sum())
    prev_volume_5 = float(rows.iloc[:-5].tail(60)["volume"].mean() * 5) if len(rows) > 5 else volume_now_5
    body = _pct(latest_close, candle_open)
    ret_15m = _pct(latest_close, close_n_minutes_ago(15))
    full_range = max(high - low, 1e-9)
    direction = np.sign(ret_15m if abs(ret_15m) > 1e-9 else body)
    if direction == 0:
        trend_consistency = 0.5
    else:
        return_signs = np.sign(returns.to_numpy(dtype=float))
        trend_consistency = float(np.mean(return_signs == direction)) if len(return_signs) else 0.5
    volume_sum_15 = float(last15["volume"].sum())
    vwap_15 = (
        float((last15["close"].astype(float) * last15["volume"].astype(float)).sum() / volume_sum_15)
        if volume_sum_15 > 0
        else latest_close
    )
    volatility_15m = float(returns.std(ddof=0) if len(returns) else 0.0)
    volatility_60m = float(returns60.std(ddof=0) if len(returns60) else 0.0)
    return {
        "ret_1m": _pct(latest_close, close_n_minutes_ago(1)),
        "ret_3m": _pct(latest_close, close_n_minutes_ago(3)),
        "ret_5m": _pct(latest_close, close_n_minutes_ago(5)),
        "ret_15m": ret_15m,
        "ret_60m": _pct(latest_close, close_n_minutes_ago(60)),
        "volatility_15m": volatility_15m,
        "volatility_regime_60m": _safe_ratio(volatility_15m, volatility_60m),
        "volume_spike_15m": (volume_now / prev_volume) if prev_volume > 0 else 1.0,
        "volume_spike_5m": (volume_now_5 / prev_volume_5) if prev_volume_5 > 0 else 1.0,
        "candle_body_15m": body,
        "upper_wick_15m": max(0.0, (high - max(latest_close, candle_open)) / full_range),
        "lower_wick_15m": max(0.0, (min(latest_close, candle_open) - low) / full_range),
        "distance_to_15m_open": body,
        "distance_to_vwap_15m": _pct(latest_close, vwap_15),
        "return_zscore_15m": _safe_ratio(ret_15m, volatility_15m),
        "trend_consistency_15m": trend_consistency,
    }


def add_polymarket_features(features: dict[str, float], *, mid: float, spread: float, depth_bid: float, depth_ask: float, time_to_resolution_sec: float, poly_return_5m: float = 0.0) -> dict[str, float]:
    bid = max(0.0, depth_bid)
    ask = max(0.0, depth_ask)
    out = dict(features)
    out.update({
        "time_to_resolution_sec": float(time_to_resolution_sec),
        "poly_mid": float(mid),
        "poly_spread": float(spread),
        "poly_depth_bid": bid,
        "poly_depth_ask": ask,
        "poly_imbalance": bid / ask if ask > 0 else 0.0,
        "poly_return_5m": float(poly_return_5m),
    })
    return out


def label_candidate(
    *,
    yes_wins: bool,
    side: str,
    entry_price: float,
    fee_rate: float,
    slippage: float,
    fill_probability: float,
) -> dict[str, Any]:
    side_upper = side.upper()
    gross = (1.0 - entry_price) if ((side_upper == "YES") == yes_wins) else -entry_price
    fee = fee_rate * entry_price * (1.0 - entry_price)
    net_pnl = gross - fee - slippage
    expected = net_pnl * max(0.0, min(1.0, fill_probability))
    return {
        "net_pnl": float(net_pnl),
        "net_ev": float(expected),
        "would_fill": fill_probability >= 0.25,
        "exit_reason": "settlement_win" if gross > 0 else "settlement_loss",
    }


def _side_entry_price(row: pd.Series | dict[str, Any], side: str) -> float:
    side_upper = side.upper()
    if side_upper == "YES":
        return float(row.get("yes_entry_price", row.get("entry_price", 0.0)) or 0.0)
    return float(row.get("no_entry_price", row.get("entry_price", 0.0)) or 0.0)


def crypto15m_side_gate_reason(
    row: pd.Series | dict[str, Any],
    side: str,
    *,
    max_entry_price: float = 1.0,
    min_abs_return_zscore_15m: float = 0.0,
    min_trend_consistency_15m: float = 0.0,
) -> str | None:
    entry_price = _side_entry_price(row, side)
    if entry_price <= 0 or entry_price >= 1:
        return "missing_entry_price"
    if entry_price > max_entry_price:
        return "price_too_late"
    if abs(float(row.get("return_zscore_15m", 0.0) or 0.0)) < min_abs_return_zscore_15m:
        return "btc_zscore_too_small"
    if float(row.get("trend_consistency_15m", 0.5) or 0.5) < min_trend_consistency_15m:
        return "btc_trend_inconsistent"
    return None


def choose_training_side(
    row: pd.Series,
    *,
    min_net_ev: float,
    max_entry_price: float = 1.0,
    min_abs_return_zscore_15m: float = 0.0,
    min_trend_consistency_15m: float = 0.0,
) -> str:
    yes_gate = crypto15m_side_gate_reason(
        row,
        "YES",
        max_entry_price=max_entry_price,
        min_abs_return_zscore_15m=min_abs_return_zscore_15m,
        min_trend_consistency_15m=min_trend_consistency_15m,
    )
    no_gate = crypto15m_side_gate_reason(
        row,
        "NO",
        max_entry_price=max_entry_price,
        min_abs_return_zscore_15m=min_abs_return_zscore_15m,
        min_trend_consistency_15m=min_trend_consistency_15m,
    )
    yes_ev = float(row.get("yes_net_ev", -math.inf)) if yes_gate is None else -math.inf
    no_ev = float(row.get("no_net_ev", -math.inf)) if no_gate is None else -math.inf
    if yes_ev < min_net_ev and no_ev < min_net_ev:
        return "NO_TRADE"
    return "YES" if yes_ev >= no_ev else "NO"


def _selected_trade_metrics(
    frame: pd.DataFrame,
    *,
    min_confidence: float,
    max_entry_price: float = 1.0,
    min_abs_return_zscore_15m: float = 0.0,
    min_trend_consistency_15m: float = 0.0,
) -> dict[str, Any]:
    if frame.empty:
        return {
            "rows": 0,
            "selected_count": 0,
            "selected_unique_markets": 0,
            "selected_accuracy": 0.0,
            "selected_unique_accuracy": 0.0,
            "mean_net_ev": 0.0,
            "unique_mean_net_ev": 0.0,
            "win_rate": 0.0,
            "unique_win_rate": 0.0,
        }
    selected = frame[
        frame["predicted_side"].isin(["YES", "NO"])
        & (frame["prediction_confidence"] >= min_confidence)
    ].copy()
    if not selected.empty:
        selected["gate_reason"] = selected.apply(
            lambda row: crypto15m_side_gate_reason(
                row,
                str(row["predicted_side"]),
                max_entry_price=max_entry_price,
                min_abs_return_zscore_15m=min_abs_return_zscore_15m,
                min_trend_consistency_15m=min_trend_consistency_15m,
            ),
            axis=1,
        )
        selected = selected[selected["gate_reason"].isna()].copy()
    selected = selected.sort_values("timestamp_dt") if "timestamp_dt" in selected.columns else selected
    selected_unique = (
        selected.drop_duplicates("market_id", keep="first")
        if "market_id" in selected.columns
        else selected
    )

    def _trade_net_ev(row: pd.Series) -> float:
        return float(row["yes_net_ev"] if row["predicted_side"] == "YES" else row["no_net_ev"])

    selected_accuracy = 0.0
    if not selected.empty:
        selected_accuracy = float(np.mean(selected["label_side"].fillna("NO_TRADE") == selected["predicted_side"]))
    selected_unique_accuracy = 0.0
    if not selected_unique.empty:
        selected_unique_accuracy = float(np.mean(selected_unique["label_side"].fillna("NO_TRADE") == selected_unique["predicted_side"]))
    selected_pnl = [_trade_net_ev(row) for _, row in selected.iterrows()]
    selected_unique_pnl = [_trade_net_ev(row) for _, row in selected_unique.iterrows()]
    return {
        "rows": int(len(frame)),
        "selected_count": int(len(selected)),
        "selected_unique_markets": int(len(selected_unique)),
        "selected_accuracy": selected_accuracy,
        "selected_unique_accuracy": selected_unique_accuracy,
        "mean_net_ev": float(np.mean(selected_pnl)) if selected_pnl else 0.0,
        "unique_mean_net_ev": float(np.mean(selected_unique_pnl)) if selected_unique_pnl else 0.0,
        "win_rate": float(np.mean([value > 0 for value in selected_pnl])) if selected_pnl else 0.0,
        "unique_win_rate": float(np.mean([value > 0 for value in selected_unique_pnl])) if selected_unique_pnl else 0.0,
    }


def _build_chronological_holdouts(
    frame: pd.DataFrame,
    *,
    window_days: int,
    windows_count: int,
    min_train_days: int = 30,
) -> list[dict[str, Any]]:
    if frame.empty or "timestamp_dt" not in frame.columns:
        return []
    ordered = frame.sort_values("timestamp_dt").reset_index(drop=True)
    current_end = ordered["timestamp_dt"].max()
    holdouts: list[dict[str, Any]] = []
    for index in range(int(max(1, windows_count))):
        window_start = current_end - pd.Timedelta(days=window_days)
        holdout = ordered[(ordered["timestamp_dt"] > window_start) & (ordered["timestamp_dt"] <= current_end)].copy()
        train = ordered[ordered["timestamp_dt"] <= window_start].copy()
        if holdout.empty or train.empty:
            break
        train_coverage = int(max(0, (train["timestamp_dt"].max() - train["timestamp_dt"].min()).days)) if len(train) else 0
        if train_coverage < min_train_days:
            break
        holdouts.append({
            "label": f"holdout_{windows_count - index}",
            "start": window_start.isoformat(),
            "end": current_end.isoformat(),
            "train": train,
            "holdout": holdout,
            "train_coverage_days": train_coverage,
        })
        current_end = window_start
    return list(reversed(holdouts))


def train_crypto15m_model(
    dataset: pd.DataFrame,
    *,
    artifact_dir: str | Path,
    min_net_ev: float = 0.003,
    min_confidence: float = 0.55,
    max_entry_price: float = 1.0,
    min_abs_return_zscore_15m: float = 0.0,
    min_trend_consistency_15m: float = 0.0,
    min_selected_accuracy: float = 0.90,
    min_rows: int = 3000,
    min_markets: int = 300,
    min_coverage_days: int = 7,
    holdout_window_days: int = 60,
    holdout_windows_count: int = 2,
    min_rows_per_holdout: int = 100,
    min_high_conf_count_per_holdout: int = 25,
) -> dict[str, Any]:
    artifact_root = Path(artifact_dir)
    artifact_root.mkdir(parents=True, exist_ok=True)
    def write_rejection(verdict: dict[str, Any]) -> dict[str, Any]:
        manifest = {
            "artifact_key": "crypto15m_rejected",
            "accepted": False,
            "reason": verdict.get("reason"),
            "feature_columns": CRYPTO15M_FEATURE_COLUMNS,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (artifact_root / "latest_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (artifact_root / "latest_verdict.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
        return verdict

    if dataset.empty or len(dataset) < min_rows:
        verdict = {
            "accepted": False,
            "reason": "not_enough_rows",
            "rows": int(len(dataset)),
            "min_rows": int(min_rows),
        }
        return write_rejection(verdict)

    from sklearn.ensemble import RandomForestClassifier

    frame = dataset.copy()
    dedupe_cols = [column for column in ("market_id", "timestamp") if column in frame.columns]
    if dedupe_cols:
        frame = frame.drop_duplicates(dedupe_cols, keep="last")
    if len(frame) < min_rows:
        verdict = {
            "accepted": False,
            "reason": "not_enough_rows_after_dedupe",
            "rows": int(len(frame)),
            "min_rows": int(min_rows),
        }
        return write_rejection(verdict)
    markets_used = int(frame["market_id"].nunique()) if "market_id" in frame.columns else 0
    coverage_days = 0
    if "timestamp" in frame.columns:
        frame["timestamp_dt"] = pd.to_datetime(frame["timestamp"], utc=True, format="mixed")
        frame = frame.sort_values("timestamp_dt").reset_index(drop=True)
        ts = frame["timestamp_dt"]
        coverage_days = int(max(0, (ts.max() - ts.min()).days)) if not ts.empty else 0
    if markets_used < min_markets or coverage_days < min_coverage_days:
        verdict = {
            "accepted": False,
            "reason": "not_enough_market_coverage",
            "rows": int(len(frame)),
            "markets_used": markets_used,
            "coverage_days": coverage_days,
            "min_markets": int(min_markets),
            "min_coverage_days": int(min_coverage_days),
        }
        return write_rejection(verdict)
    if "label_side" not in frame.columns:
        frame["label_side"] = frame.apply(
            lambda row: choose_training_side(
                row,
                min_net_ev=min_net_ev,
                max_entry_price=max_entry_price,
                min_abs_return_zscore_15m=min_abs_return_zscore_15m,
                min_trend_consistency_15m=min_trend_consistency_15m,
            ),
            axis=1,
        )
    holdout_specs = _build_chronological_holdouts(
        frame,
        window_days=holdout_window_days,
        windows_count=holdout_windows_count,
        min_train_days=max(30, holdout_window_days // 2),
    )
    if not holdout_specs:
        verdict = {
            "accepted": False,
            "reason": "not_enough_holdout_coverage",
            "rows": int(len(frame)),
            "markets_used": markets_used,
            "coverage_days": coverage_days,
            "holdout_window_days": int(holdout_window_days),
            "holdout_windows_requested": int(holdout_windows_count),
        }
        return write_rejection(verdict)

    holdout_reports: list[dict[str, Any]] = []
    for spec in holdout_specs:
        x_train = spec["train"][CRYPTO15M_FEATURE_COLUMNS].fillna(0.0)
        y_train = spec["train"]["label_side"].fillna("NO_TRADE")
        x_holdout = spec["holdout"][CRYPTO15M_FEATURE_COLUMNS].fillna(0.0)
        model = RandomForestClassifier(
            n_estimators=240,
            max_depth=10,
            min_samples_leaf=4,
            random_state=42,
            class_weight="balanced_subsample",
            n_jobs=-1,
        )
        model.fit(x_train, y_train)
        holdout = spec["holdout"].copy()
        holdout["predicted_side"] = model.predict(x_holdout)
        confidence = np.ones(len(holdout))
        if hasattr(model, "predict_proba"):
            confidence = np.max(model.predict_proba(x_holdout), axis=1)
        holdout["prediction_confidence"] = confidence
        metrics = _selected_trade_metrics(
            holdout,
            min_confidence=min_confidence,
            max_entry_price=max_entry_price,
            min_abs_return_zscore_15m=min_abs_return_zscore_15m,
            min_trend_consistency_15m=min_trend_consistency_15m,
        )
        holdout_reports.append({
            "label": spec["label"],
            "start": spec["start"],
            "end": spec["end"],
            "train_coverage_days": spec["train_coverage_days"],
            **metrics,
        })

    mean_net_ev = float(np.mean([report["mean_net_ev"] for report in holdout_reports])) if holdout_reports else 0.0
    unique_mean_net_ev = float(np.mean([report["unique_mean_net_ev"] for report in holdout_reports])) if holdout_reports else 0.0
    selected_accuracy = float(np.mean([report["selected_accuracy"] for report in holdout_reports])) if holdout_reports else 0.0
    selected_unique_accuracy = float(np.mean([report["selected_unique_accuracy"] for report in holdout_reports])) if holdout_reports else 0.0
    accepted = bool(
        holdout_reports
        and all(report["rows"] >= min_rows_per_holdout for report in holdout_reports)
        and all(report["selected_unique_markets"] >= min_high_conf_count_per_holdout for report in holdout_reports)
        and all(report["mean_net_ev"] > 0 for report in holdout_reports)
        and all(report["unique_mean_net_ev"] > 0 for report in holdout_reports)
        and all(report["selected_accuracy"] >= min_selected_accuracy for report in holdout_reports)
        and all(report["selected_unique_accuracy"] >= min_selected_accuracy for report in holdout_reports)
    )

    x = frame[CRYPTO15M_FEATURE_COLUMNS].fillna(0.0)
    y = frame["label_side"].fillna("NO_TRADE")
    model = RandomForestClassifier(
        n_estimators=240,
        max_depth=10,
        min_samples_leaf=4,
        random_state=42,
        class_weight="balanced_subsample",
        n_jobs=-1,
    )
    model.fit(x, y)
    artifact_key = "crypto15m_latest"
    model_path = artifact_root / f"{artifact_key}.pkl"
    with open(model_path, "wb") as fh:
        pickle.dump({"model": model, "feature_columns": CRYPTO15M_FEATURE_COLUMNS}, fh)
    manifest = {
        "artifact_key": artifact_key,
        "accepted": accepted,
        "model_path": str(model_path),
        "feature_columns": CRYPTO15M_FEATURE_COLUMNS,
        "accuracy": selected_accuracy,
        "mean_net_ev": mean_net_ev,
        "selected_count": int(sum(report["selected_count"] for report in holdout_reports)),
        "selected_unique_markets": int(sum(report["selected_unique_markets"] for report in holdout_reports)),
        "selected_accuracy": selected_accuracy,
        "selected_unique_accuracy": selected_unique_accuracy,
        "rows": int(len(frame)),
        "markets_used": markets_used,
        "coverage_days": coverage_days,
        "min_confidence": min_confidence,
        "max_entry_price": max_entry_price,
        "min_abs_return_zscore_15m": min_abs_return_zscore_15m,
        "min_trend_consistency_15m": min_trend_consistency_15m,
        "unique_mean_net_ev": unique_mean_net_ev,
        "holdouts": holdout_reports,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (artifact_root / "latest_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    verdict = {
        "accepted": accepted,
        "reason": "accepted" if accepted else "holdout_net_ev_or_support_failed",
        "accuracy": selected_accuracy,
        "mean_net_ev": mean_net_ev,
        "unique_mean_net_ev": unique_mean_net_ev,
        "selected_count": int(sum(report["selected_count"] for report in holdout_reports)),
        "selected_unique_markets": int(sum(report["selected_unique_markets"] for report in holdout_reports)),
        "selected_accuracy": selected_accuracy,
        "selected_unique_accuracy": selected_unique_accuracy,
        "rows": int(len(frame)),
        "markets_used": markets_used,
        "coverage_days": coverage_days,
        "max_entry_price": max_entry_price,
        "min_abs_return_zscore_15m": min_abs_return_zscore_15m,
        "min_trend_consistency_15m": min_trend_consistency_15m,
        "holdouts": holdout_reports,
    }
    (artifact_root / "latest_verdict.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    return verdict
