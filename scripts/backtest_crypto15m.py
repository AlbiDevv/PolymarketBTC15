from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_settings
from historical import read_dataset
from research.crypto15m import CRYPTO15M_FEATURE_COLUMNS, crypto15m_side_gate_reason


def main():
    parser = argparse.ArgumentParser(description="Backtest latest Crypto15m model artifact")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    settings = load_settings(args.config)
    dataset = read_dataset(settings.crypto_data.out_dir, "crypto15m_candidates")
    manifest_path = Path(settings.strategy.crypto15m_model.artifact_path)
    if dataset.empty:
        raise SystemExit("no crypto15m dataset")
    if not manifest_path.exists():
        raise SystemExit("no crypto15m artifact")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("accepted"):
        raise SystemExit(f"crypto15m artifact is not accepted: {manifest.get('reason')}")
    with open(manifest["model_path"], "rb") as fh:
        bundle = pickle.load(fh)
    frame = dataset.copy().sort_values("timestamp")
    dedupe_cols = [column for column in ("market_id", "timestamp") if column in frame.columns]
    if dedupe_cols:
        frame = frame.drop_duplicates(dedupe_cols, keep="last")
    frame["timestamp_dt"] = pd.to_datetime(frame["timestamp"], utc=True, format="mixed")
    holdout_windows = manifest.get("holdouts") or []
    if holdout_windows:
        latest_holdout = holdout_windows[-1]
        holdout_start = pd.to_datetime(latest_holdout["start"], utc=True, format="mixed")
        holdout_end = pd.to_datetime(latest_holdout["end"], utc=True, format="mixed")
        holdout = frame[
            (frame["timestamp_dt"] > holdout_start)
            & (frame["timestamp_dt"] <= holdout_end)
        ].copy()
    elif "market_id" in frame.columns:
        market_order = frame.groupby("market_id")["timestamp_dt"].min().sort_values()
        split_at = max(1, int(len(market_order) * 0.75))
        holdout_markets = set(market_order.iloc[split_at:].index.astype(str))
        holdout = frame[frame["market_id"].astype(str).isin(holdout_markets)].copy()
    else:
        holdout = frame.iloc[int(len(frame) * 0.75):].copy()
    preds = bundle["model"].predict(holdout[CRYPTO15M_FEATURE_COLUMNS].fillna(0.0))
    holdout["predicted_side"] = preds
    confidence = np.ones(len(holdout))
    if hasattr(bundle["model"], "predict_proba"):
        confidence = np.max(bundle["model"].predict_proba(holdout[CRYPTO15M_FEATURE_COLUMNS].fillna(0.0)), axis=1)
    holdout["prediction_confidence"] = confidence
    min_confidence = float(manifest.get("min_confidence", settings.strategy.crypto15m_model.min_confidence))
    selected = holdout[
        holdout["predicted_side"].isin(["YES", "NO"])
        & (holdout["prediction_confidence"] >= min_confidence)
    ].copy()
    if not selected.empty:
        selected["gate_reason"] = selected.apply(
            lambda row: crypto15m_side_gate_reason(
                row,
                str(row["predicted_side"]),
                max_entry_price=float(manifest.get("max_entry_price", settings.lab.crypto15m.max_entry_price)),
                min_abs_return_zscore_15m=float(
                    manifest.get("min_abs_return_zscore_15m", settings.lab.crypto15m.min_abs_return_zscore_15m)
                ),
                min_trend_consistency_15m=float(
                    manifest.get("min_trend_consistency_15m", settings.lab.crypto15m.min_trend_consistency_15m)
                ),
            ),
            axis=1,
        )
        selected = selected[selected["gate_reason"].isna()].copy()
    selected = selected.sort_values("timestamp_dt")
    selected_unique = selected.drop_duplicates("market_id", keep="first") if "market_id" in selected.columns else selected
    pnl = [
        row["yes_net_pnl"] if row["predicted_side"] == "YES" else row["no_net_pnl"]
        for _, row in selected.iterrows()
    ]
    unique_pnl = [
        row["yes_net_pnl"] if row["predicted_side"] == "YES" else row["no_net_pnl"]
        for _, row in selected_unique.iterrows()
    ]
    equity = np.cumsum(pnl) if pnl else np.array([])
    max_drawdown = 0.0
    if len(equity):
        peaks = np.maximum.accumulate(equity)
        max_drawdown = float(np.min(equity - peaks))
    unique_equity = np.cumsum(unique_pnl) if unique_pnl else np.array([])
    unique_max_drawdown = 0.0
    if len(unique_equity):
        unique_peaks = np.maximum.accumulate(unique_equity)
        unique_max_drawdown = float(np.min(unique_equity - unique_peaks))
    result = {
        "rows": int(len(frame)),
        "holdout_rows": int(len(holdout)),
        "trades": int(len(selected)),
        "unique_market_trades": int(len(selected_unique)),
        "realized_pnl_units": float(np.sum(pnl)) if pnl else 0.0,
        "unique_realized_pnl_units": float(np.sum(unique_pnl)) if unique_pnl else 0.0,
        "avg_pnl_per_trade": float(np.mean(pnl)) if pnl else 0.0,
        "unique_avg_pnl_per_trade": float(np.mean(unique_pnl)) if unique_pnl else 0.0,
        "win_rate": float(np.mean([x > 0 for x in pnl])) if pnl else 0.0,
        "unique_win_rate": float(np.mean([x > 0 for x in unique_pnl])) if unique_pnl else 0.0,
        "max_drawdown_units": max_drawdown,
        "unique_max_drawdown_units": unique_max_drawdown,
        "min_confidence": min_confidence,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
