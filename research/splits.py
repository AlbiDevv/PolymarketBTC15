"""
Temporal split policy — no random shuffle, no future leakage.

Concrete date cutoffs are chosen per dataset_version and recorded in frozen report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TemporalSplitPolicyV1:
    """Example structure; replace dates when building a real dataset."""

    policy_id: str
    train_end: datetime
    val_end: datetime
    holdout_end: datetime
    rationale: str


def assign_split(decision_ts: datetime, policy: TemporalSplitPolicyV1) -> str:
    if decision_ts <= policy.train_end:
        return "train"
    if decision_ts <= policy.val_end:
        return "validation"
    if decision_ts <= policy.holdout_end:
        return "hold_out"
    return "excluded"  # after hold-out window — or use for forward test
