import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from risk.limits import RiskLimits, PositionInfo, LimitCheckResult


def _make_positions(n: int, event_id: str = "evt_1") -> list[PositionInfo]:
    return [
        PositionInfo(
            market_id=i,
            event_id=event_id,
            side="YES",
            size=2.0,
            entry_price=0.50,
        )
        for i in range(n)
    ]


def test_max_positions_limit():
    limits = RiskLimits(max_positions=5)
    positions = _make_positions(5)
    result = limits.check_all(
        bankroll=1000, initial_bankroll=1000,
        open_positions=positions, new_event_id="evt_new", new_stake=2.0,
    )
    assert not result.allowed
    assert "Max positions" in result.reason


def test_allows_below_max_positions():
    limits = RiskLimits(max_positions=5)
    positions = _make_positions(3)
    result = limits.check_all(
        bankroll=1000, initial_bankroll=1000,
        open_positions=positions, new_event_id="evt_new", new_stake=2.0,
    )
    assert result.allowed


def test_concentration_limit():
    limits = RiskLimits(max_concentration=0.10)
    # 9 positions in same event, each $2 * 0.50 = $1 exposure → $9 total
    positions = _make_positions(9, event_id="evt_1")
    # Adding $2 to same event would make $11 / $100 = 11% > 10%
    result = limits.check_all(
        bankroll=100, initial_bankroll=100,
        open_positions=positions, new_event_id="evt_1", new_stake=2.0,
    )
    assert not result.allowed
    assert "Concentration" in result.reason


def test_total_drawdown_stop():
    limits = RiskLimits(total_drawdown_stop=0.20)
    result = limits.check_all(
        bankroll=790, initial_bankroll=1000,
        open_positions=[], new_event_id="evt_1", new_stake=2.0,
    )
    assert not result.allowed
    assert "FULL STOP" in result.reason


def test_daily_loss_limit():
    limits = RiskLimits(daily_loss_limit=0.05)
    limits.update_pnl(-55)  # lost $55

    result = limits.check_all(
        bankroll=945, initial_bankroll=1000,
        open_positions=[], new_event_id="evt_1", new_stake=2.0,
    )
    assert not result.allowed
    assert "Daily loss" in result.reason
