"""
p_model v1 — market as prior + calibration + H2/H4 + micro (bounded).

Parameters for bias terms are fit on TRAIN only; frozen for validation/hold-out.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PModelV1Params:
    """Frozen after training — bump model_version when changed."""

    model_version: str
    calibration_params_json: str
    h2_zones: tuple[tuple[float, float, float], ...]
    h4_tail_low: float
    h4_tail_high: float
    h4_bias_low: float
    h4_bias_high: float
    micro_max_abs: float


def p_model_v1_formula(
    p_base: float,
    bias_round: float,
    bias_tail: float,
    bias_micro: float,
    micro_max_abs: float,
) -> float:
    """
    p_model = clip(p_base + bias_round + bias_tail + clamp(bias_micro), 0.01, 0.99)

    bias_micro should be clipped to [-micro_max_abs, micro_max_abs] before summation.
    """
    bm = max(-micro_max_abs, min(micro_max_abs, bias_micro))
    x = p_base + bias_round + bias_tail + bm
    return max(0.01, min(0.99, x))
