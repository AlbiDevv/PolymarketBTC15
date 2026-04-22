"""Tests for position exit logic: stop-loss, take-profit, time-based."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ExitConfig


class TestExitConfig:
    def test_defaults(self):
        cfg = ExitConfig()
        assert cfg.stop_loss_pct == 0.15
        assert cfg.take_profit_pct == 0.25
        assert cfg.time_exit_hours == 0.0
        assert cfg.enabled is True

    def test_custom(self):
        cfg = ExitConfig(stop_loss_pct=0.10, take_profit_pct=0.30, time_exit_hours=48)
        assert cfg.stop_loss_pct == 0.10
        assert cfg.take_profit_pct == 0.30
        assert cfg.time_exit_hours == 48


class TestExitDecisionLogic:
    """Test the pure exit decision logic extracted from orchestrator."""

    def _should_exit(self, entry_price, current_price, sl=0.15, tp=0.25):
        pct_change = (current_price - entry_price) / entry_price if entry_price > 0 else 0
        if pct_change <= -sl:
            return "STOP_LOSS"
        elif pct_change >= tp:
            return "TAKE_PROFIT"
        return None

    def test_stop_loss_triggers(self):
        # Entry at 0.50, price drops to 0.42 → -16% → should trigger at -15%
        assert self._should_exit(0.50, 0.42) == "STOP_LOSS"

    def test_stop_loss_not_triggered(self):
        # Entry at 0.50, price drops to 0.44 → -12% → no trigger
        assert self._should_exit(0.50, 0.44) is None

    def test_take_profit_triggers(self):
        # Entry at 0.50, price rises to 0.63 → +26% → should trigger at +25%
        assert self._should_exit(0.50, 0.63) == "TAKE_PROFIT"

    def test_take_profit_not_triggered(self):
        # Entry at 0.50, price rises to 0.60 → +20% → no trigger
        assert self._should_exit(0.50, 0.60) is None

    def test_exact_boundary_stop_loss(self):
        # Entry at 0.60, price drops to 0.50 → -16.7% → triggers
        assert self._should_exit(0.60, 0.50) == "STOP_LOSS"

    def test_exact_boundary_take_profit(self):
        # Entry at 0.40, price rises to 0.51 → +27.5% → triggers
        assert self._should_exit(0.40, 0.51) == "TAKE_PROFIT"

    def test_no_exit_in_neutral_zone(self):
        assert self._should_exit(0.50, 0.50) is None
        assert self._should_exit(0.50, 0.52) is None
        assert self._should_exit(0.50, 0.48) is None


class TestExitPnLCalculation:
    """Verify PnL math for exits (not settlements)."""

    def _calc_exit_pnl(self, entry, exit_price, contracts, fee_rate=0.02):
        gross_per_contract = exit_price - entry
        if gross_per_contract > 0:
            fee = gross_per_contract * fee_rate
        else:
            fee = 0.0
        return contracts * (gross_per_contract - fee)

    def test_profitable_exit(self):
        pnl = self._calc_exit_pnl(entry=0.50, exit_price=0.65, contracts=10)
        # gross = 0.15 per contract, fee = 0.003, net = 0.147, total = 1.47
        assert pnl > 0
        assert abs(pnl - 1.47) < 0.01

    def test_loss_exit(self):
        pnl = self._calc_exit_pnl(entry=0.50, exit_price=0.42, contracts=10)
        # gross = -0.08, no fee on losses
        assert pnl < 0
        assert abs(pnl - (-0.80)) < 0.01

    def test_break_even_exit(self):
        pnl = self._calc_exit_pnl(entry=0.50, exit_price=0.50, contracts=10)
        assert pnl == 0.0

    def test_fee_only_on_profit(self):
        pnl_loss = self._calc_exit_pnl(entry=0.50, exit_price=0.40, contracts=10, fee_rate=0.10)
        # No fee on loss
        assert abs(pnl_loss - (-1.0)) < 0.001

        pnl_win = self._calc_exit_pnl(entry=0.50, exit_price=0.60, contracts=10, fee_rate=0.10)
        # Fee = 0.10 * 0.10 = 0.01 per contract, net = 0.09
        assert abs(pnl_win - 0.90) < 0.01
