"""
Baseline models A–E — same inputs/outputs, comparable under identical execution assumptions.

Training/calibration logic lives in train_calibration.py (future); this module defines names and interfaces.
"""

from __future__ import annotations

from enum import Enum
from typing import Callable

import numpy as np


class BaselineId(str, Enum):
    A_MARKET = "baseline_market"
    B_CALIBRATED = "baseline_calibrated"
    C_H2_ONLY = "baseline_h2_only"
    D_H4_ONLY = "baseline_h4_only"
    E_MODEL_V1 = "model_v1"


def predict_baseline_market(p_market: np.ndarray) -> np.ndarray:
    """A: p_model = p_market"""
    return np.clip(p_market, 0.01, 0.99)


def predict_baseline_calibrated(
    p_market: np.ndarray,
    calibrator: Callable[[np.ndarray], np.ndarray],
) -> np.ndarray:
    """B: calibrated(p_market) only — no H2/H4."""
    return np.clip(calibrator(p_market), 0.01, 0.99)


def predict_baseline_h2_only(
    p_base: np.ndarray,
    bias_round: np.ndarray,
) -> np.ndarray:
    """C: p_base (typically calibrated) + H2 round-number bias."""
    return np.clip(p_base + bias_round, 0.01, 0.99)


def predict_baseline_h4_only(
    p_base: np.ndarray,
    bias_tail: np.ndarray,
) -> np.ndarray:
    """D: p_base + H4 tail bias."""
    return np.clip(p_base + bias_tail, 0.01, 0.99)


def predict_model_v1(
    p_base: np.ndarray,
    bias_round: np.ndarray,
    bias_tail: np.ndarray,
    bias_micro: np.ndarray,
) -> np.ndarray:
    """E: full v1 stack."""
    return np.clip(p_base + bias_round + bias_tail + bias_micro, 0.01, 0.99)
