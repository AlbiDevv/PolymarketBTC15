from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research.motif_learning import evaluate_holdout, frame_readiness, publish_artifact
from research.trade_costs import (
    fee_rate_bps_to_fraction,
    net_ev_per_share,
    polymarket_taker_fee_per_share,
    polymarket_taker_fee_usdc,
)


class DummyModel:
    def predict_proba(self, rows):
        return [[0.4, 0.6] for _ in rows]


def test_frame_readiness_requires_markets_and_coverage():
    now = datetime.now(timezone.utc)
    frame = pd.DataFrame([
        {"market_id": "m1", "settled_at": now - timedelta(days=5)},
        {"market_id": "m2", "settled_at": now - timedelta(days=3)},
        {"market_id": "m3", "settled_at": now},
    ])
    readiness = frame_readiness(frame, min_markets_required=5, min_coverage_days=10)
    assert readiness["ready"] is False
    assert readiness["markets_used"] == 3
    assert readiness["coverage_days"] == 5
    assert "min_markets_required" in readiness["reason"]
    assert "min_coverage_days" in readiness["reason"]


def test_publish_artifact_marks_support_failures(tmp_path):
    holdouts = [{
        "rows": 40,
        "accuracy": 0.90,
        "high_conf_accuracy": 0.94,
        "high_conf_count": 10,
        "high_conf_ratio": 0.25,
        "high_conf_net_ev": -0.01,
        "calibration_error": 0.10,
        "from": "2025-01-01T00:00:00+00:00",
        "to": "2025-03-31T00:00:00+00:00",
    }]
    artifact = publish_artifact(
        out_dir=tmp_path,
        model=DummyModel(),
        category_priors={"crypto": 0.5},
        holdouts=holdouts,
        motifs=[],
        high_conf_threshold=0.65,
        fee_rate=0.02,
        min_high_conf_accuracy=0.95,
        max_calibration_error=0.08,
        min_rows_per_holdout=100,
        min_high_conf_count_per_holdout=25,
        readiness={"ready": True, "reason": "ready", "coverage_days": 400, "markets_used": 2200},
    )
    assert artifact.accepted is False
    assert artifact.verdict["accepted"] is False
    assert artifact.verdict["coverage_days"] == 400
    assert artifact.verdict["markets_used"] == 2200
    assert "holdout_1_rows" in artifact.verdict["reason"]
    latest_verdict = json.loads((tmp_path / "latest_verdict.json").read_text(encoding="utf-8"))
    assert latest_verdict["accepted"] is False
    assert latest_verdict["markets_used"] == 2200


def test_polymarket_fee_formula_is_price_sensitive():
    assert polymarket_taker_fee_per_share(0.50, fee_rate=0.02, fees_enabled=True) == 0.005
    assert round(polymarket_taker_fee_per_share(0.99, fee_rate=0.02, fees_enabled=True), 6) == 0.000198
    assert polymarket_taker_fee_per_share(0.99, fee_rate=0.02, fees_enabled=False) == 0.0


def test_polymarket_fee_formula_accepts_dynamic_bps():
    fee_rate = fee_rate_bps_to_fraction(30)
    assert fee_rate == 0.003
    assert polymarket_taker_fee_usdc(contracts=100, price=0.50, fee_rate_bps=30) == 0.075
    assert polymarket_taker_fee_usdc(contracts=100, price=0.50, fee_rate_bps=30, fees_enabled=False) == 0.0


def test_late_obvious_trade_can_be_rejected_when_entry_leaves_no_edge():
    assert net_ev_per_share(
        win_probability=1.0,
        entry_price=0.9995,
        fee_rate=0.02,
        fees_enabled=True,
    ) < 0.001


def test_evaluate_holdout_filters_to_tradable_positive_ev_subset():
    class FixedModel:
        def predict_proba(self, rows):
            return pd.DataFrame([[0.01, 0.99] for _ in range(len(rows))]).to_numpy()

    holdout = pd.DataFrame([
        {
            "market_yes": 0.90,
            "entry_price": 0.90,
            "price_return_60m": 0.1,
            "price_range_60m": 0.1,
            "volatility_60m": 0.01,
            "volume_24h": 1000,
            "liquidity": 1000,
            "samples_pre": 20,
            "extreme_yes_share": 1.0,
            "extreme_no_share": 0.0,
            "pre_event_window_minutes": 60,
            "time_to_resolution_sec": 300,
            "category_prior": 0.5,
            "late_stage_high_conf_flag": True,
            "fee_rate": 0.02,
            "fees_enabled": True,
            "outcome_yes": 1,
            "settled_at": datetime.now(timezone.utc),
        },
        {
            "market_yes": 0.9995,
            "entry_price": 0.9995,
            "price_return_60m": 0.1,
            "price_range_60m": 0.1,
            "volatility_60m": 0.01,
            "volume_24h": 1000,
            "liquidity": 1000,
            "samples_pre": 20,
            "extreme_yes_share": 1.0,
            "extreme_no_share": 0.0,
            "pre_event_window_minutes": 60,
            "time_to_resolution_sec": 300,
            "category_prior": 0.5,
            "late_stage_high_conf_flag": True,
            "fee_rate": 0.02,
            "fees_enabled": True,
            "outcome_yes": 1,
            "settled_at": datetime.now(timezone.utc),
        },
    ])

    report = evaluate_holdout(
        FixedModel(),
        holdout,
        high_conf_threshold=0.65,
        fee_rate=0.02,
        min_candidate_net_ev=0.001,
        max_candidate_entry_price=0.995,
    )

    assert report["high_conf_count"] == 1
    assert report["high_conf_accuracy"] == 1.0
    assert report["high_conf_net_ev"] > 0
