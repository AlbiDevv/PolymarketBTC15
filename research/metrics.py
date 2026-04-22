"""
Metrics for research — expected vs realized EV, calibration, risk.

Implementations are thin wrappers; full evaluation in notebooks/scripts or backtest engine.
"""

from __future__ import annotations

import numpy as np


def brier_score(y_true: np.ndarray, p_pred: np.ndarray) -> float:
    y = y_true.astype(float)
    return float(np.mean((p_pred - y) ** 2))


def expected_value_binary(y_true: np.ndarray, p_pred: np.ndarray, cost_per_trade: np.ndarray) -> float:
    """
    Simplified EV proxy: mean of (y_true - p_pred) adjusted by cost — extend for real fee model.
    For rigorous EV use project-specific EV calculator with fees/spread.
    """
    y = y_true.astype(float)
    return float(np.mean(y - p_pred - cost_per_trade))


def ece_bins(y_true: np.ndarray, p_pred: np.ndarray, n_bins: int = 10) -> tuple[float, np.ndarray, np.ndarray]:
    """Expected calibration error — returns (ece, bin_pred_means, bin_acc)."""
    bins = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    n = len(y_true)
    preds = []
    accs = []
    for i in range(n_bins):
        m = (p_pred >= bins[i]) & (p_pred < bins[i + 1])
        if i == n_bins - 1:
            m = (p_pred >= bins[i]) & (p_pred <= bins[i + 1])
        cnt = m.sum()
        if cnt == 0:
            preds.append(0.0)
            accs.append(0.0)
            continue
        acc = y_true[m].mean()
        conf = p_pred[m].mean()
        ece += (cnt / n) * abs(acc - conf)
        preds.append(conf)
        accs.append(acc)
    return float(ece), np.array(preds), np.array(accs)


def sharpe_ratio(returns: np.ndarray, eps: float = 1e-12) -> float:
    if len(returns) < 2:
        return 0.0
    mu = returns.mean()
    sd = returns.std()
    if sd < eps:
        return 0.0
    return float(mu / sd * np.sqrt(len(returns)))
