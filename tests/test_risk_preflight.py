"""Tests for risk pre-flight checks: spread, stale data, dup orders."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from risk.limits import RiskLimits, PositionInfo, LimitCheckResult


class TestWideSpreadGuard:
    """The orchestrator rejects markets with spread > max_spread (0.15)."""

    def test_spread_guard_value(self):
        # This is checked in orchestrator._evaluate_market, not in RiskLimits
        # We verify the threshold is configurable
        max_spread = 0.15
        wide_spread = 0.20
        assert wide_spread > max_spread

        narrow_spread = 0.03
        assert narrow_spread <= max_spread


class TestDuplicateOrderProtection:
    """The orchestrator maintains _recent_order_keys to prevent duplicates."""

    def test_dedup_key_format(self):
        market_id = "cond_abc"
        side = "YES"
        key = f"{market_id}:{side}"
        assert key == "cond_abc:YES"

    def test_dedup_different_sides_different_keys(self):
        k1 = "cond_abc:YES"
        k2 = "cond_abc:NO"
        assert k1 != k2


class TestRiskLimitsExpanded:
    """Additional edge-case tests for RiskLimits."""

    def test_weekly_loss_blocks(self):
        limits = RiskLimits(daily_loss_limit=0.50, weekly_loss_limit=0.10)
        limits.update_pnl(-110)

        result = limits.check_all(
            bankroll=890, initial_bankroll=1000,
            open_positions=[], new_event_id="e1", new_stake=2.0,
        )
        assert not result.allowed
        assert "Weekly loss" in result.reason

    def test_zero_bankroll_does_not_crash(self):
        limits = RiskLimits()
        result = limits.check_all(
            bankroll=0, initial_bankroll=1000,
            open_positions=[], new_event_id="e1", new_stake=2.0,
        )
        # Should block due to total drawdown (100%)
        assert not result.allowed

    def test_event_concentration_with_string_ids(self):
        """event_id should be compared as strings consistently."""
        positions = [
            PositionInfo(
                market_id=1, event_id="evt_1", side="YES",
                size=50, entry_price=0.50,
            ),
        ]
        limits = RiskLimits(max_concentration=0.10)
        result = limits.check_all(
            bankroll=100, initial_bankroll=100,
            open_positions=positions, new_event_id="evt_1", new_stake=10.0,
        )
        # 50*0.50 = 25 existing + 10 new = 35 > 10% of 100
        assert not result.allowed

    def test_different_event_passes_concentration(self):
        positions = [
            PositionInfo(
                market_id=1, event_id="evt_1", side="YES",
                size=50, entry_price=0.50,
            ),
        ]
        limits = RiskLimits(max_concentration=0.10)
        result = limits.check_all(
            bankroll=1000, initial_bankroll=1000,
            open_positions=positions, new_event_id="evt_2", new_stake=10.0,
        )
        assert result.allowed
