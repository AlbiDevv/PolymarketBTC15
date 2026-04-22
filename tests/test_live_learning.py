import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Settings
from exchange_client.base import Market, Orderbook, OrderbookLevel, Token
from lab.live_learning import LearnedModelGate


class DummyModel:
    def __init__(self):
        self.n_jobs = -1

    def predict_proba(self, rows):
        out = []
        for row in rows:
            prob_yes = 0.99 if float(row[0]) >= 0.90 else 0.1
            out.append([1.0 - prob_yes, prob_yes])
        return out


def _market() -> Market:
    return Market(
        id="m1",
        question="Will YES resolve?",
        category="crypto",
        end_date="2026-04-10T12:00:00+00:00",
        resolution_source="",
        active=True,
        volume_24h=10000.0,
        tokens=[
            Token(token_id="yes", outcome="Yes", price=0.95),
            Token(token_id="no", outcome="No", price=0.05),
        ],
        event_id="e1",
    )


def _book(bid: float, ask: float) -> Orderbook:
    return Orderbook(
        market_id="yes",
        bids=[OrderbookLevel(bid, 1000)],
        asks=[OrderbookLevel(ask, 1000)],
        timestamp=0.0,
    )


def _write_artifact(tmp_path: Path, *, accepted: bool = True) -> Path:
    model_path = tmp_path / "model.pkl"
    with open(model_path, "wb") as fh:
        pickle.dump({
            "model": DummyModel(),
            "feature_columns": [
                "market_yes",
                "price_return_60m",
                "price_range_60m",
                "volatility_60m",
                "volume_24h",
                "liquidity",
                "samples_pre",
                "extreme_yes_share",
                "extreme_no_share",
                "pre_event_window_minutes",
                "category_prior",
            ],
            "category_priors": {"crypto": 0.7},
        }, fh)
    manifest_path = tmp_path / "latest_manifest.json"
    manifest_path.write_text(json.dumps({
        "accepted": accepted,
        "model_path": str(model_path),
        "high_conf_threshold": 0.60,
    }), encoding="utf-8")
    return manifest_path


def test_learned_gate_vetoes_when_artifact_not_accepted(tmp_path):
    settings = Settings()
    settings.strategy.learned_model.artifact_path = str(_write_artifact(tmp_path, accepted=False))
    gate = LearnedModelGate(settings)
    decision = gate.score_candidate(
        _market(),
        _book(0.94, 0.96),
        _book(0.04, 0.06),
        side="YES",
        market_probability=0.95,
        external_data={"yes_mid": 0.95},
    )
    assert decision.should_veto is True
    assert decision.reason == "artifact_not_accepted"


def test_learned_gate_allows_high_conf_candidate(tmp_path):
    settings = Settings()
    settings.strategy.learned_model.artifact_path = str(_write_artifact(tmp_path, accepted=True))
    settings.strategy.learned_model.veto_margin = 0.0
    gate = LearnedModelGate(settings)
    decision = gate.score_candidate(
        _market(),
        _book(0.94, 0.96),
        _book(0.04, 0.06),
        side="YES",
        market_probability=0.50,
        external_data={"yes_mid": 0.95},
    )
    assert decision.enabled is True
    assert decision.should_veto is False
    assert decision.predicted_yes_probability > 0.5
    assert decision.expected_net_ev > 0
    assert gate._bundle["model"].n_jobs == 1


def test_learned_gate_rejects_fee_adjusted_negative_ev(tmp_path):
    settings = Settings()
    settings.strategy.learned_model.artifact_path = str(_write_artifact(tmp_path, accepted=True))
    settings.strategy.learned_model.veto_margin = 0.0
    gate = LearnedModelGate(settings)
    decision = gate.score_candidate(
        _market(),
        _book(0.995, 0.9995),
        _book(0.0005, 0.005),
        side="YES",
        market_probability=0.9995,
        external_data={"yes_mid": 0.9995},
    )
    assert decision.should_veto is True
    assert decision.reason in {"price_too_late", "fee_adjusted_ev_negative"}
