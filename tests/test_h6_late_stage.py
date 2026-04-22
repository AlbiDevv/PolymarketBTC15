import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exchange_client.base import Orderbook, OrderbookLevel
from models.hypothesis import H6_LateStagePressure


def _book() -> Orderbook:
    return Orderbook(
        market_id="yes",
        bids=[OrderbookLevel(0.94, 600.0)],
        asks=[OrderbookLevel(0.95, 550.0)],
        timestamp=0.0,
    )


def test_h6_emits_yes_signal_when_extreme_persists_and_book_agrees():
    hypothesis = H6_LateStagePressure()
    signal = hypothesis.evaluate(
        market_id="m1",
        question="Will event resolve today?",
        orderbook=_book(),
        external_data={
            "yes_mid": 0.945,
            "extreme_yes_min": 0.92,
            "extreme_yes_max": 0.08,
            "persistence_required_sec": 120,
            "imbalance_ratio_min": 2.5,
            "yes_extreme_persistence_sec": 240,
            "yes_imbalance_ratio": 3.4,
            "yes_direction_agrees": True,
            "fee_plus_slippage": 0.01,
        },
    )
    assert signal.side == "YES"
    assert signal.hypothesis_id == "H6"
    assert signal.edge > 0


def test_h6_blocks_signal_when_persistence_missing():
    hypothesis = H6_LateStagePressure()
    signal = hypothesis.evaluate(
        market_id="m1",
        question="Will event resolve today?",
        orderbook=_book(),
        external_data={
            "yes_mid": 0.945,
            "extreme_yes_min": 0.92,
            "extreme_yes_max": 0.08,
            "persistence_required_sec": 120,
            "imbalance_ratio_min": 2.5,
            "yes_extreme_persistence_sec": 30,
            "yes_imbalance_ratio": 3.4,
            "yes_direction_agrees": True,
            "fee_plus_slippage": 0.01,
        },
    )
    assert signal.side is None
