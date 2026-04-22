"""
Train calibration on temporal train split only. Saves versioned artifact.

Methods:
  - bucket: JSON with bin edges + means (portable)
  - isotonic: pickled sklearn IsotonicRegression (research/artifacts/*.pkl)
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any, Literal

import numpy as np
from loguru import logger

Method = Literal["bucket", "isotonic"]


def empirical_bucket_calibrate(
    p_market: np.ndarray,
    y: np.ndarray,
    n_bins: int = 10,
) -> dict[str, Any]:
    bins = np.linspace(0, 1, n_bins + 1)
    bin_means = np.full(n_bins, 0.5)
    for i in range(n_bins):
        lo, hi = bins[i], bins[i + 1]
        m = (p_market >= lo) & (p_market < hi) if i < n_bins - 1 else (p_market >= lo) & (p_market <= hi)
        if m.sum() > 0:
            bin_means[i] = float(np.clip(y[m].mean(), 0.01, 0.99))
    return {"type": "bucket", "n_bins": n_bins, "bin_edges": bins.tolist(), "bin_means": bin_means.tolist()}


def apply_bucket_calibrator(p: np.ndarray, art: dict[str, Any]) -> np.ndarray:
    edges = np.array(art["bin_edges"])
    means = np.array(art["bin_means"])
    out = np.full_like(p, 0.5, dtype=float)
    n_bins = art["n_bins"]
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        m = (p >= lo) & (p < hi) if i < n_bins - 1 else (p >= lo) & (p <= hi)
        out[m] = means[i]
    return np.clip(out, 0.01, 0.99)


def train_and_save(
    p_train: np.ndarray,
    y_train: np.ndarray,
    method: Method,
    out_dir: Path,
    feature_version: str,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"feature_version": feature_version, "method": method, "n_train": int(len(p_train))}

    if method == "bucket":
        art = empirical_bucket_calibrate(p_train, y_train)
        meta["calibration"] = art
        path = out_dir / f"calibration_{feature_version}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        logger.info(f"Saved bucket calibration to {path}")
        return path

    from sklearn.isotonic import IsotonicRegression

    ir = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99)
    ir.fit(p_train, y_train)
    path = out_dir / f"calibration_isotonic_{feature_version}.pkl"
    with open(path, "wb") as f:
        pickle.dump({"meta": meta, "model": ir}, f)
    logger.info(f"Saved isotonic calibration to {path}")
    return path


def load_calibrator(path: Path) -> tuple[dict[str, Any], Any]:
    if path.suffix == ".json":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data, None
    with open(path, "rb") as f:
        data = pickle.load(f)
    return data["meta"], data["model"]


def apply_calibrator(p: np.ndarray, meta: dict[str, Any], model: Any) -> np.ndarray:
    if meta["method"] == "bucket":
        return apply_bucket_calibrator(p, meta["calibration"])
    pred = model.predict(p)
    return np.clip(pred, 0.01, 0.99)
