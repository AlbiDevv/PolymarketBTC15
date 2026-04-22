import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.ev import calculate_ev


def test_ev_positive_edge_yes():
    """When model thinks YES is more likely than market price, edge on YES is positive."""
    result = calculate_ev(
        p_model=0.70,
        price_ask=0.55,
        price_bid=0.53,
        fee=0.02,
        edge_threshold=0.05,
    )
    assert result.ev_yes > 0
    assert result.best_side == "YES"
    assert result.recommended is True


def test_ev_no_edge_when_fair():
    """When model agrees with market, no edge after fees."""
    result = calculate_ev(
        p_model=0.55,
        price_ask=0.55,
        price_bid=0.53,
        fee=0.02,
        edge_threshold=0.05,
    )
    assert result.best_edge < 0.05
    assert result.recommended is False


def test_ev_negative_edge():
    """When model thinks YES is less likely than market, EV on YES is negative."""
    result = calculate_ev(
        p_model=0.40,
        price_ask=0.55,
        price_bid=0.53,
        fee=0.02,
    )
    assert result.ev_yes < 0


def test_ev_no_side_edge():
    """When model thinks NO is much more likely, best side should be NO."""
    result = calculate_ev(
        p_model=0.20,
        price_ask=0.55,
        price_bid=0.53,
        fee=0.02,
        edge_threshold=0.05,
    )
    assert result.best_side == "NO"
    assert result.ev_no > result.ev_yes


def test_ev_fee_matters():
    """Edge should be smaller with higher fees."""
    r1 = calculate_ev(p_model=0.70, price_ask=0.55, price_bid=0.53, fee=0.02)
    r2 = calculate_ev(p_model=0.70, price_ask=0.55, price_bid=0.53, fee=0.10)
    assert r1.best_edge > r2.best_edge
