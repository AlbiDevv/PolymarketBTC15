import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research.data_quality import validate_row_v1
from research.dataset_row import ResearchDatasetRowV1


def _minimal_row(**kwargs):
    base = dict(
        dataset_version="ds_v1",
        feature_version="fv1",
        split_version="sv1",
        market_id="m1",
        event_id="e1",
        token_id="t1",
        side="YES",
        decision_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
        category="test",
        time_to_resolution_sec=86400.0,
        tte_bucket="1-7d",
        spread=0.02,
        spread_bucket="low",
        liquidity_proxy=1000.0,
        liquidity_bucket="med",
        depth_bid=1.0,
        depth_ask=1.0,
        p_market=0.5,
        mid_price_if_needed_for_reference=0.5,
        execution_price_assumption="mid",
        entry_fee_assumption=0.02,
        exit_fee_assumption=0.02,
        source="historical",
        resolved_outcome_for_side=1,
        resolved_ts=datetime(2025, 2, 1, tzinfo=timezone.utc),
        quality_flags=[],
        p_market_source="native_yes",
    )
    base.update(kwargs)
    return ResearchDatasetRowV1(**base)


def test_valid_row():
    r = validate_row_v1(_minimal_row())
    assert r.ok
    assert not r.errors


def test_invalid_target():
    r = validate_row_v1(_minimal_row(resolved_outcome_for_side=2))
    assert not r.ok
