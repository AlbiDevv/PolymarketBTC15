import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exchange_client.base import Orderbook, OrderbookLevel
from models.hypothesis import H1_NewsLag, H2_RoundNumberBias, H4_UnderpricedTails


def _make_ob(bid: float = 0.49, ask: float = 0.51) -> Orderbook:
    return Orderbook(
        market_id="test",
        bids=[OrderbookLevel(bid, 100)],
        asks=[OrderbookLevel(ask, 100)],
        timestamp=0,
    )


def test_h1_no_external_data():
    h1 = H1_NewsLag()
    signal = h1.evaluate("m1", "Will X happen?", _make_ob())
    assert signal.side is None


def test_h1_with_divergence():
    h1 = H1_NewsLag()
    signal = h1.evaluate(
        "m1", "Will X happen?", _make_ob(0.49, 0.51),
        external_data={"probability": 0.65},
    )
    assert signal.side == "YES"
    assert signal.edge > 0


def test_h1_small_divergence_no_signal():
    h1 = H1_NewsLag()
    signal = h1.evaluate(
        "m1", "Will X happen?", _make_ob(0.49, 0.51),
        external_data={"probability": 0.52},
    )
    assert signal.side is None


def test_h2_round_50():
    h2 = H2_RoundNumberBias()
    signal = h2.evaluate("m1", "Question?", _make_ob(0.49, 0.51))
    assert signal.side is not None
    assert signal.hypothesis_id == "H2"


def test_h2_non_round():
    h2 = H2_RoundNumberBias()
    signal = h2.evaluate("m1", "Question?", _make_ob(0.29, 0.31))
    assert signal.side is None


def test_h4_tail_event():
    h4 = H4_UnderpricedTails()
    signal = h4.evaluate("m1", "Longshot?", _make_ob(0.04, 0.06))
    assert signal.side == "YES"
    assert signal.edge > 0


def test_h4_non_tail():
    h4 = H4_UnderpricedTails()
    signal = h4.evaluate("m1", "Normal?", _make_ob(0.49, 0.51))
    assert signal.side is None
