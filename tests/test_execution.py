import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exchange_client.base import Orderbook, OrderbookLevel
from models.execution import FeeModel, ExecutionCostModel, FillModel, estimate_execution


def _make_ob() -> Orderbook:
    return Orderbook(
        market_id="test",
        bids=[OrderbookLevel(0.48, 50), OrderbookLevel(0.47, 100)],
        asks=[OrderbookLevel(0.52, 50), OrderbookLevel(0.53, 100)],
        timestamp=0,
    )


def test_fee_model_default():
    fm = FeeModel(default_fee_pct=0.02)
    assert fm.get_fee() == 0.02
    assert fm.fee_on_profit(10.0) == 0.20
    assert fm.fee_on_profit(-5.0) == 0.0


def test_fee_model_override():
    fm = FeeModel(default_fee_pct=0.02)
    fm.set_market_fee("special", 0.05)
    assert fm.get_fee("special") == 0.05
    assert fm.get_fee("other") == 0.02


def test_slippage_positive():
    em = ExecutionCostModel(latency_ticks=1, tick_size=0.01)
    ob = _make_ob()
    slip = em.estimate_slippage(ob, size=5.0, side="YES")
    assert slip >= 0


def test_effective_price_higher_than_ask():
    em = ExecutionCostModel(latency_ticks=1)
    ob = _make_ob()
    price = em.effective_entry_price(ob, size=5.0, side="YES")
    assert price >= ob.best_ask


def test_fill_probability_bounded():
    fm = FillModel(base_fill_rate=0.80)
    ob = _make_ob()
    p = fm.estimate_fill_probability(ob, limit_price=0.52, side="YES")
    assert 0 < p <= 1.0


def test_estimate_execution_full():
    ob = _make_ob()
    est = estimate_execution(ob, side="YES", size=5.0)
    assert est.effective_price >= est.raw_price
    assert est.fee > 0
    assert 0 < est.fill_probability <= 1.0
    assert est.net_payout >= 0
