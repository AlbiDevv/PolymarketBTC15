import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research.cost_assumptions import (
    COST_ASSUMPTIONS_VERSION,
    DEFAULT_ASSUMPTIONS,
    ev_proxy_per_row,
)
from research.evaluate import evaluate_split


def test_ev_proxy_matches_formula():
    y = np.array([1.0, 0.0])
    p = np.array([0.5, 0.5])
    fee = DEFAULT_ASSUMPTIONS.flat_fee_per_unit
    ev = ev_proxy_per_row(y, p, fee=fee)
    assert np.allclose(ev, np.array([0.5 - fee, -0.5 - fee]))


def test_evaluate_split_uses_same_fee():
    import pandas as pd

    df = pd.DataFrame({
        "resolved_outcome_for_side": [1, 0],
        "p_market": [0.4, 0.6],
    })
    r = evaluate_split("t", df, None, None, fee=0.02)
    assert r["cost_assumptions_version"] == COST_ASSUMPTIONS_VERSION
    assert abs(r["mean_ev_A"] - np.mean([1 - 0.4 - 0.02, 0 - 0.6 - 0.02])) < 1e-6
