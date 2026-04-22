"""
Research package: dataset v1 spec, p_model baselines, metrics, split policy.

See RESEARCH_SPRINT.md for the full sprint narrative and roadmap.
"""

from .definitions import (
    clip_probability,
    p_market_fallback_no_from_yes_complement,
    p_market_from_token_mid,
    resolved_outcome_for_side,
)
from .dataset_row import ResearchDatasetRowV1
from .baselines import BaselineId, predict_baseline_market, predict_model_v1

__all__ = [
    "clip_probability",
    "p_market_fallback_no_from_yes_complement",
    "p_market_from_token_mid",
    "resolved_outcome_for_side",
    "ResearchDatasetRowV1",
    "BaselineId",
    "predict_baseline_market",
    "predict_model_v1",
]
