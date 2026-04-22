"""
Evaluate baseline A (market) vs B (calibrated) + hold-out + walk-forward + bootstrap.

Usage:
  python -m research.evaluate --dataset research/artifacts/dataset_v1.csv \\
    --train-end 2024-06-01 --val-end 2024-09-01 --out-dir research/artifacts
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

from .metrics import brier_score, ece_bins, sharpe_ratio
from .train_calibration import (
    apply_calibrator,
    empirical_bucket_calibrate,
    load_calibrator,
    train_and_save,
)
from .cost_assumptions import (
    COST_ASSUMPTIONS_VERSION,
    DEFAULT_ASSUMPTIONS,
    CostAssumptions,
    ev_proxy_per_row,
)

FALLBACK_SHARE_WARN_THRESHOLD = 0.35


def load_dataset_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["decision_ts"] = pd.to_datetime(df["decision_ts"], utc=True)
    return df


def augment_p_market_source(df: pd.DataFrame) -> pd.DataFrame:
    """If CSV lacks p_market_source, infer from quality_flags_json + side (legacy datasets)."""
    if "p_market_source" in df.columns:
        ser = df["p_market_source"]
        has_val = ser.notna() & ser.astype(str).str.strip().ne("")
        if has_val.any():
            out = df.copy()
            out["p_market_source"] = out["p_market_source"].fillna("legacy_unlabeled")
            return out

    import json

    def infer(row: pd.Series) -> str:
        side = row.get("side", "")
        if side == "YES":
            return "native_yes"
        qf_raw = row.get("quality_flags_json", "")
        if pd.isna(qf_raw):
            return "legacy_unlabeled"
        try:
            qf = json.loads(qf_raw) if isinstance(qf_raw, str) else qf_raw
        except Exception:
            return "legacy_unlabeled"
        if isinstance(qf, list) and "fallback_complement_no_book" in qf:
            return "complement_fallback"
        return "legacy_unlabeled"

    out = df.copy()
    out["p_market_source"] = out.apply(infer, axis=1)
    return out


def segment_metrics_block(df: pd.DataFrame, meta: dict | None, model: object | None) -> dict:
    """Metrics for clean native, complement fallback, and all rows."""
    df = augment_p_market_source(df)
    out: dict = {}
    subsets = {
        "clean_native": df[df["p_market_source"].isin(["native_yes", "native_no"])],
        "complement_fallback": df[df["p_market_source"] == "complement_fallback"],
        "all_rows": df,
    }
    for name, sub in subsets.items():
        out[name] = evaluate_split(f"segment_{name}", sub, meta, model)
    return out


def evaluate_split(
    name: str,
    df: pd.DataFrame,
    meta: dict | None,
    model: object | None,
    fee: float = DEFAULT_ASSUMPTIONS.flat_fee_per_unit,
) -> dict:
    if len(df) == 0:
        return {"split": name, "n": 0}
    y = df["resolved_outcome_for_side"].values.astype(float)
    p_m = df["p_market"].values.astype(float)
    b_a = brier_score(y, p_m)

    if meta is not None:
        p_b = apply_calibrator(p_m, meta, model)
        b_b = brier_score(y, p_b)
    else:
        p_b = p_m
        b_b = b_a

    ece_a, _, _ = ece_bins(y, p_m)
    ece_b, _, _ = ece_bins(y, p_b)
    ev_a = ev_proxy_per_row(y, p_m, fee=fee)
    ev_b = ev_proxy_per_row(y, p_b, fee=fee)

    return {
        "split": name,
        "n": int(len(df)),
        "brier_A_market": float(b_a),
        "brier_B_calibrated": float(b_b),
        "ece_A": float(ece_a),
        "ece_B": float(ece_b),
        "mean_ev_A": float(ev_a.mean()),
        "mean_ev_B": float(ev_b.mean()),
        "sharpe_ev_A": float(sharpe_ratio(ev_a)),
        "sharpe_ev_B": float(sharpe_ratio(ev_b)),
        "cost_assumptions_version": COST_ASSUMPTIONS_VERSION,
    }


def bootstrap_ev(ev: np.ndarray, n_boot: int = 500, seed: int = 42) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    if len(ev) < 2:
        m = float(ev.mean()) if len(ev) else 0.0
        return m, m, m
    means = [rng.choice(ev, size=len(ev), replace=True).mean() for _ in range(n_boot)]
    means = np.array(means)
    return float(means.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def walk_forward_eval(df: pd.DataFrame, n_windows: int = 3) -> list[dict]:
    df = df.sort_values("decision_ts").reset_index(drop=True)
    n = len(df)
    results = []
    if n < 30:
        return results
    chunk = max(n // (n_windows + 2), 8)
    for w in range(n_windows):
        te_start = (w + 2) * chunk
        te_end = te_start + chunk
        train_df = df.iloc[:te_start]
        test_df = df.iloc[te_start:te_end]
        if len(train_df) < 10 or len(test_df) < 5:
            continue
        art = empirical_bucket_calibrate(
            train_df["p_market"].values.astype(float),
            train_df["resolved_outcome_for_side"].values.astype(float),
        )
        meta = {"method": "bucket", "calibration": art, "feature_version": "wf"}
        y = test_df["resolved_outcome_for_side"].values.astype(float)
        p_m = test_df["p_market"].values.astype(float)
        p_b = apply_calibrator(p_m, meta, None)
        fee = DEFAULT_ASSUMPTIONS.flat_fee_per_unit
        results.append({
            "window": w,
            "n_test": len(test_df),
            "brier_A": float(brier_score(y, p_m)),
            "brier_B": float(brier_score(y, p_b)),
            "mean_ev_A": float(ev_proxy_per_row(y, p_m, fee=fee).mean()),
            "mean_ev_B": float(ev_proxy_per_row(y, p_b, fee=fee).mean()),
        })
    return results


def render_frozen_report(sections: dict, out_path: Path) -> None:
    template_path = Path(__file__).resolve().parent / "FROZEN_REPORT_TEMPLATE.md"
    text = template_path.read_text(encoding="utf-8")
    text += "\n\n---\n\n## AUTO-GENERATED\n\n"
    text += "```json\n" + json.dumps(sections, indent=2, default=str) + "\n```\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    logger.info(f"Wrote frozen report to {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Research evaluation: baselines A/B")
    parser.add_argument("--dataset", type=str, default="research/artifacts/dataset_v1.csv")
    parser.add_argument("--train-end", type=str, required=True)
    parser.add_argument("--val-end", type=str, required=True)
    parser.add_argument("--out-dir", type=str, default="research/artifacts")
    parser.add_argument("--method", choices=["bucket", "isotonic"], default="bucket")
    parser.add_argument("--feature-version", type=str, default="fv1")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    out_dir = Path(args.out_dir)
    ds = Path(args.dataset)
    if not ds.exists():
        logger.error(f"Dataset not found: {ds}")
        sys.exit(1)

    df = load_dataset_csv(ds)
    if len(df) < 10:
        logger.error("Dataset too small for evaluation")
        sys.exit(1)

    train_end = datetime.fromisoformat(args.train_end.replace("Z", "+00:00"))
    if train_end.tzinfo is None:
        train_end = train_end.replace(tzinfo=timezone.utc)
    val_end = datetime.fromisoformat(args.val_end.replace("Z", "+00:00"))
    if val_end.tzinfo is None:
        val_end = val_end.replace(tzinfo=timezone.utc)

    ts = df["decision_ts"]
    t_tr = pd.Timestamp(train_end)
    t_val = pd.Timestamp(val_end)
    train_df = df[ts < t_tr]
    val_df = df[(ts >= t_tr) & (ts < t_val)]
    hold_df = df[ts >= t_val]

    if len(train_df) < 5:
        logger.error("Train split too small; adjust --train-end")
        sys.exit(1)

    p_tr = train_df["p_market"].values.astype(float)
    y_tr = train_df["resolved_outcome_for_side"].values.astype(float)

    cal_path = train_and_save(p_tr, y_tr, args.method, out_dir, args.feature_version)
    meta, model = load_calibrator(cal_path)

    res_train = evaluate_split("train", train_df, meta, model)
    res_val = evaluate_split("validation", val_df, meta, model) if len(val_df) else {"split": "validation", "n": 0}
    res_hold = evaluate_split("hold_out", hold_df, meta, model) if len(hold_df) else {"split": "hold_out", "n": 0}

    fee = DEFAULT_ASSUMPTIONS.flat_fee_per_unit
    if len(hold_df) > 0:
        ev_h = ev_proxy_per_row(
            hold_df["resolved_outcome_for_side"].values.astype(float),
            apply_calibrator(hold_df["p_market"].values.astype(float), meta, model),
            fee=fee,
        )
        boot_mean, boot_lo, boot_hi = bootstrap_ev(ev_h)
    else:
        boot_mean = boot_lo = boot_hi = 0.0

    wf = walk_forward_eval(df, n_windows=3)

    df_aug = augment_p_market_source(df)
    fb_share = float(
        (df_aug["p_market_source"] == "complement_fallback").mean(),
    ) if len(df_aug) else 0.0
    assumptions: CostAssumptions = DEFAULT_ASSUMPTIONS

    go = False
    if res_hold.get("n", 0) > 0:
        go = (
            res_hold.get("brier_B_calibrated", 99) < res_hold.get("brier_A_market", 0)
            and res_hold.get("mean_ev_B", -99) > res_hold.get("mean_ev_A", -99)
        )
        if fb_share > FALLBACK_SHARE_WARN_THRESHOLD:
            go = False

    segments_full = segment_metrics_block(df, meta, model)
    segments_hold = segment_metrics_block(hold_df, meta, model) if len(hold_df) else {}

    sections = {
        "dataset": str(ds),
        "train_end": args.train_end,
        "val_end": args.val_end,
        "method": args.method,
        "calibration_artifact": str(cal_path),
        "results_train": res_train,
        "results_validation": res_val,
        "results_hold_out": res_hold,
        "bootstrap_ev_B_mean_ci": [boot_mean, boot_lo, boot_hi],
        "walk_forward": wf,
        "go_no_go": "GO" if go else "NO-GO",
        "execution_cost_assumptions": assumptions.to_dict(),
        "ev_proxy_definition": {
            "version": COST_ASSUMPTIONS_VERSION,
            "flat_fee_per_unit": fee,
            "note": (
                "mean_ev_* is E[y - p - fee] on hold-out labels — a research proxy, "
                "not realized execution EV (no spread crossing, no partial fills)."
            ),
        },
        "segments_full_dataset": segments_full,
        "segments_hold_out": segments_hold,
        "data_quality": {
            "fallback_complement_share": fb_share,
            "warning": (
                "HIGH complement_fallback share — baseline may lean on inferred NO prices; "
                "interpret GO/NO-GO with caution."
                if fb_share > FALLBACK_SHARE_WARN_THRESHOLD
                else None
            ),
        },
    }

    render_frozen_report(sections, out_dir / "FROZEN_REPORT_latest.md")
    print(json.dumps(sections, indent=2, default=str))


if __name__ == "__main__":
    main()
