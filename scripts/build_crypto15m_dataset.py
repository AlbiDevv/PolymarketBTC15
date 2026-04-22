from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_settings
from historical import append_manifest, read_dataset, replace_dataset, write_partitioned_parquet
from research.crypto15m import (
    CRYPTO15M_FEATURE_COLUMNS,
    add_polymarket_features,
    classify_crypto15m_updown_market,
    choose_training_side,
    label_candidate,
    timestamp_ns,
)


def _read_crypto_candles(out_dir: str | Path) -> pd.DataFrame:
    frames = [pd.read_parquet(path) for path in Path(out_dir).glob("symbol=*/*/candles.parquet")]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _pct(current: pd.Series, previous: pd.Series) -> pd.Series:
    previous = previous.replace(0, np.nan)
    return ((current - previous) / previous).replace([np.inf, -np.inf], np.nan).fillna(0.0)


def _build_crypto_feature_table(candles: pd.DataFrame) -> pd.DataFrame:
    if candles.empty:
        return pd.DataFrame(columns=["symbol", "timestamp", *CRYPTO15M_FEATURE_COLUMNS])
    frames: list[pd.DataFrame] = []
    for symbol, group in candles.groupby("symbol"):
        group = group.sort_values("timestamp").reset_index(drop=True).copy()
        close = group["close"].astype(float)
        open_ = group["open"].astype(float)
        high = group["high"].astype(float)
        low = group["low"].astype(float)
        volume = group["volume"].astype(float)
        one_min_returns = close.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0)
        rolling_volume_15 = volume.rolling(15, min_periods=1).sum()
        rolling_volume_5 = volume.rolling(5, min_periods=1).sum()
        rolling_volume_60 = volume.shift(15).rolling(60, min_periods=1).mean() * 15
        rolling_volume_5_baseline = volume.shift(5).rolling(60, min_periods=1).mean() * 5
        candle_open_15 = open_.shift(14).fillna(open_)
        rolling_high_15 = high.rolling(15, min_periods=1).max()
        rolling_low_15 = low.rolling(15, min_periods=1).min()
        full_range = (rolling_high_15 - rolling_low_15).replace(0, np.nan)
        volatility_15m = one_min_returns.rolling(15, min_periods=2).std(ddof=0).fillna(0.0)
        volatility_60m = one_min_returns.rolling(60, min_periods=5).std(ddof=0).replace(0, np.nan)
        ret_15m = _pct(close, close.shift(15))
        return_sign = np.sign(ret_15m.replace(0.0, np.nan).fillna(0.0))
        one_min_sign = np.sign(one_min_returns.fillna(0.0))
        direction_alignment = (one_min_sign == return_sign).astype(float)
        trend_consistency = direction_alignment.rolling(15, min_periods=1).mean().fillna(0.5)
        vwap_15 = (
            (close * volume).rolling(15, min_periods=1).sum()
            / volume.rolling(15, min_periods=1).sum().replace(0, np.nan)
        ).fillna(close)
        frame = pd.DataFrame({
            "symbol": symbol,
            "timestamp": group["timestamp"],
            "ret_1m": _pct(close, close.shift(1)),
            "ret_3m": _pct(close, close.shift(3)),
            "ret_5m": _pct(close, close.shift(5)),
            "ret_15m": ret_15m,
            "ret_60m": _pct(close, close.shift(60)),
            "volatility_15m": volatility_15m,
            "volatility_regime_60m": (volatility_15m / volatility_60m).replace([np.inf, -np.inf], np.nan).fillna(0.0),
            "volume_spike_15m": (rolling_volume_15 / rolling_volume_60.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(1.0),
            "volume_spike_5m": (rolling_volume_5 / rolling_volume_5_baseline.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(1.0),
            "candle_body_15m": _pct(close, candle_open_15),
            "upper_wick_15m": ((rolling_high_15 - pd.concat([close, candle_open_15], axis=1).max(axis=1)) / full_range).clip(lower=0.0).fillna(0.0),
            "lower_wick_15m": ((pd.concat([close, candle_open_15], axis=1).min(axis=1) - rolling_low_15) / full_range).clip(lower=0.0).fillna(0.0),
            "distance_to_15m_open": _pct(close, candle_open_15),
            "distance_to_vwap_15m": _pct(close, vwap_15),
            "return_zscore_15m": (ret_15m / volatility_15m.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(0.0),
            "trend_consistency_15m": trend_consistency,
        })
        frames.append(frame)
    return pd.concat(frames, ignore_index=True).sort_values(["symbol", "timestamp"]).reset_index(drop=True)


def build_dataset(settings) -> pd.DataFrame:
    windows = read_dataset(settings.historical.price_window.out_dir, "crypto15m_price_windows")
    if windows.empty:
        windows = read_dataset(settings.historical.price_window.out_dir, "price_windows")
    candles = _read_crypto_candles(settings.crypto_data.out_dir)
    if windows.empty or candles.empty:
        return pd.DataFrame()

    rows = windows.copy()
    rows["timestamp_dt"] = pd.to_datetime(rows["timestamp"], unit="s", utc=True)
    rows["settled_at_dt"] = pd.to_datetime(rows["settled_at"], utc=True, format="mixed")
    rows = rows[rows["question"].map(lambda q: classify_crypto15m_updown_market(str(q)).is_crypto15m)]
    if rows.empty:
        return pd.DataFrame()
    rows = (
        rows.sort_values(["market_id", "timestamp_dt", "side", "settled_at_dt"])
        .drop_duplicates(["market_id", "timestamp_dt", "side"], keep="first")
    )
    candles["timestamp"] = pd.to_datetime(candles["timestamp"], utc=True)
    if "timeframe" in candles.columns:
        candles = candles[candles["timeframe"].eq("1m")]
    crypto_feature_table = _build_crypto_feature_table(candles)

    out_rows: list[dict] = []
    total_markets = int(rows["market_id"].nunique())
    neutral_depth_usd = max(float(settings.lab.crypto15m.min_depth_usd), 250.0)
    for idx, (market_id, group) in enumerate(rows.groupby("market_id"), start=1):
        if idx % 1000 == 0:
            print({"stage": "build_candidates", "markets_done": idx, "markets_total": total_markets, "rows": len(out_rows)}, flush=True)
        question = str(group["question"].iloc[0])
        info = classify_crypto15m_updown_market(question)
        if not info.is_crypto15m:
            continue
        pivot = group.pivot_table(index="timestamp_dt", columns="side", values="price", aggfunc="last").sort_index()
        if "YES" not in pivot.columns:
            continue
        yes_prices = pivot["YES"].dropna()
        if len(yes_prices) < 3:
            continue
        settled_at = pd.Timestamp(group["settled_at_dt"].iloc[0])
        outcome = str(group["outcome"].iloc[0]).upper()
        if outcome == "UP":
            outcome = "YES"
        elif outcome == "DOWN":
            outcome = "NO"
        if outcome not in {"YES", "NO"}:
            outcome = "YES" if float(yes_prices.iloc[-1]) >= 0.5 else "NO"
        yes_wins = outcome == "YES"
        candidate_prices = yes_prices.iloc[:-1]
        history_ns = yes_prices.index.astype("int64").to_numpy()
        history_values = yes_prices.astype(float).to_numpy()
        candidate_window_start = settled_at - pd.Timedelta(minutes=settings.lab.crypto15m.candidate_window_minutes)
        max_poly_return_abs = float(settings.lab.crypto15m.max_poly_return_abs)
        target_time_left = int(settings.lab.crypto15m.candidate_target_time_to_resolution_sec)
        target_tolerance = int(settings.lab.crypto15m.candidate_target_tolerance_sec)
        candidate_points: list[tuple[pd.Timestamp, float, float]] = []
        for ts, yes_price in candidate_prices.items():
            if ts < candidate_window_start:
                continue
            time_left = max(0.0, (settled_at - ts).total_seconds())
            if time_left <= 0 or time_left > settings.lab.crypto15m.max_horizon_hours * 3600:
                continue
            if time_left < int(settings.lab.crypto15m.candidate_min_time_to_resolution_sec):
                continue
            candidate_points.append((ts, float(yes_price), time_left))
        if not candidate_points:
            continue
        chosen_ts, chosen_yes_price, chosen_time_left = min(
            candidate_points,
            key=lambda item: (abs(item[2] - target_time_left), -item[2]),
        )
        if abs(chosen_time_left - target_time_left) > target_tolerance:
            continue
        for ts, yes_price, time_left in [(chosen_ts, chosen_yes_price, chosen_time_left)]:
            no_price = float(pivot.loc[ts, "NO"]) if "NO" in pivot.columns and pd.notna(pivot.loc[ts, "NO"]) else max(0.001, 1.0 - float(yes_price))
            poly_return_5m = 0.0
            current_idx = int(np.searchsorted(history_ns, ts.value, side="right")) - 1
            if current_idx > 0:
                cutoff = ts - pd.Timedelta(minutes=5)
                prior_idx = int(np.searchsorted(history_ns, cutoff.value, side="right")) - 1
                if prior_idx >= 0 and float(history_values[prior_idx]) > 0:
                    prior_price = float(history_values[prior_idx])
                    if prior_price >= 0.01:
                        raw_return = (float(yes_price) - prior_price) / prior_price
                        poly_return_5m = max(-max_poly_return_abs, min(max_poly_return_abs, raw_return))
            spread = abs(float(yes_price) - (1.0 - no_price))
            feature_row = add_polymarket_features(
                {},
                mid=float(yes_price),
                spread=spread,
                depth_bid=neutral_depth_usd,
                depth_ask=neutral_depth_usd,
                time_to_resolution_sec=time_left,
                poly_return_5m=poly_return_5m,
            )
            fill_probability = max(0.05, min(0.85, 1.0 - spread * 10.0))
            yes_label = label_candidate(
                yes_wins=yes_wins,
                side="YES",
                entry_price=float(yes_price),
                fee_rate=settings.strategy.fee_rate,
                slippage=spread / 2.0,
                fill_probability=fill_probability,
            )
            no_label = label_candidate(
                yes_wins=yes_wins,
                side="NO",
                entry_price=no_price,
                fee_rate=settings.strategy.fee_rate,
                slippage=spread / 2.0,
                fill_probability=fill_probability,
            )
            out_rows.append({
                "date": ts.date().isoformat(),
                "market_id": str(market_id),
                "question": question,
                "symbol": info.symbol,
                "timestamp": ts.isoformat(),
                "settled_at": settled_at.isoformat(),
                "outcome": outcome,
                "yes_entry_price": float(yes_price),
                "no_entry_price": no_price,
                "yes_net_pnl": yes_label["net_pnl"],
                "yes_net_ev": yes_label["net_ev"],
                "no_net_pnl": no_label["net_pnl"],
                "no_net_ev": no_label["net_ev"],
                "would_fill": bool(yes_label["would_fill"] or no_label["would_fill"]),
                "label_side": "YES" if yes_label["net_ev"] >= settings.lab.crypto15m.min_net_ev and yes_label["net_ev"] >= no_label["net_ev"] else ("NO" if no_label["net_ev"] >= settings.lab.crypto15m.min_net_ev else "NO_TRADE"),
                **{column: float(feature_row.get(column, 0.0)) for column in CRYPTO15M_FEATURE_COLUMNS},
            })
    dataset = pd.DataFrame(out_rows)
    if dataset.empty or crypto_feature_table.empty:
        return dataset
    dataset["timestamp_ns"] = timestamp_ns(dataset["timestamp"])
    merged_frames: list[pd.DataFrame] = []
    crypto_columns = [
        column for column in CRYPTO15M_FEATURE_COLUMNS
        if column.startswith(("ret_", "volatility", "volume", "candle", "upper", "lower", "distance", "return_zscore", "trend_consistency"))
    ]
    for symbol, group in dataset.groupby("symbol"):
        feature_group = crypto_feature_table[crypto_feature_table["symbol"].eq(symbol)]
        if feature_group.empty:
            merged_frames.append(group)
            continue
        left = group.drop(columns=crypto_columns, errors="ignore").sort_values("timestamp_ns")
        right = feature_group[["timestamp", *crypto_columns]].copy()
        right["timestamp_ns"] = timestamp_ns(right["timestamp"])
        right = right.drop(columns=["timestamp"]).sort_values("timestamp_ns")
        merged = pd.merge_asof(
            left,
            right,
            on="timestamp_ns",
            direction="backward",
            suffixes=("", "_crypto"),
        )
        merged_frames.append(merged)
    dataset = pd.concat(merged_frames, ignore_index=True)
    dataset = dataset.drop(columns=["timestamp_ns"], errors="ignore")
    for column in CRYPTO15M_FEATURE_COLUMNS:
        if column not in dataset.columns:
            dataset[column] = 0.0
        dataset[column] = dataset[column].fillna(0.0).astype(float)
    dataset["label_side"] = dataset.apply(
        lambda row: choose_training_side(
            row,
            min_net_ev=settings.lab.crypto15m.min_net_ev,
            max_entry_price=settings.lab.crypto15m.max_entry_price,
            min_abs_return_zscore_15m=settings.lab.crypto15m.min_abs_return_zscore_15m,
            min_trend_consistency_15m=settings.lab.crypto15m.min_trend_consistency_15m,
        ),
        axis=1,
    )
    return dataset


def main():
    parser = argparse.ArgumentParser(description="Build Crypto15m trade-candidate dataset")
    parser.add_argument("--config", default=None)
    parser.add_argument("--append", action="store_true", help="Append a new candidate batch instead of replacing the generated dataset")
    args = parser.parse_args()
    settings = load_settings(args.config)
    dataset = build_dataset(settings)
    if not args.append:
        replace_dataset(settings.crypto_data.out_dir, "crypto15m_candidates")
    written = write_partitioned_parquet(
        settings.crypto_data.out_dir,
        "crypto15m_candidates",
        dataset.to_dict("records"),
        partition_key="date",
        filename_prefix="crypto15m_candidates",
    )
    append_manifest(settings.crypto_data.out_dir, "crypto15m_candidates", {
        "rows": int(len(dataset)),
        "markets": int(dataset["market_id"].nunique()) if not dataset.empty else 0,
        "written_files": [str(path) for path in written],
    })
    print({"rows": int(len(dataset)), "written": [str(path) for path in written]})


if __name__ == "__main__":
    main()
