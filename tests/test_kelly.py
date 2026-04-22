import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.kelly import kelly_stake, kelly_stake_with_drawdown_adjustment


def test_kelly_positive_edge():
    """With positive edge, Kelly should return a non-zero stake."""
    stake = kelly_stake(p=0.65, price=0.50, bankroll=1000, k=0.25)
    assert stake > 0
    assert stake <= 2.0  # default max


def test_kelly_no_edge():
    """When p < price (implied prob), Kelly should be zero."""
    stake = kelly_stake(p=0.40, price=0.50, bankroll=1000, k=0.25)
    assert stake == 0.0


def test_kelly_respects_max():
    """Kelly should never exceed stake_max."""
    stake = kelly_stake(p=0.90, price=0.50, bankroll=10000, k=0.5, stake_max=5.0)
    assert stake <= 5.0


def test_kelly_below_min_returns_zero():
    """If Kelly calculation is below minimum, return 0."""
    stake = kelly_stake(p=0.51, price=0.50, bankroll=50, k=0.10, stake_min=1.0)
    assert stake == 0.0


def test_kelly_drawdown_reduces_stake():
    """In drawdown, adjusted Kelly should produce smaller stakes."""
    normal = kelly_stake_with_drawdown_adjustment(
        p=0.65, price=0.50, bankroll=1000, initial_bankroll=1000,
        k_base=0.25, stake_max=5.0,
    )
    in_drawdown = kelly_stake_with_drawdown_adjustment(
        p=0.65, price=0.50, bankroll=850, initial_bankroll=1000,
        k_base=0.25, stake_max=5.0,
    )
    assert in_drawdown <= normal


def test_kelly_total_stop_at_20pct():
    """At 20%+ drawdown, stake should be 0."""
    stake = kelly_stake_with_drawdown_adjustment(
        p=0.80, price=0.50, bankroll=790, initial_bankroll=1000,
        k_base=0.25, stake_max=5.0,
    )
    assert stake == 0.0
