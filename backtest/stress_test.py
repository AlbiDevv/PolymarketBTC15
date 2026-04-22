"""
Stress Tests — v3.0 §12.3 addition.

Simulates adverse conditions to test strategy robustness:
- Spread widening
- Fill rate degradation
- Latency increase
- Partial liquidity disappearance
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd
from loguru import logger

from .engine import BacktestEngine, BacktestResult


@dataclass
class StressScenario:
    name: str
    spread_multiplier: float = 1.0   # 1.0 = normal, 2.0 = double spread
    fill_rate_multiplier: float = 1.0  # 1.0 = normal, 0.5 = half fill rate
    latency_multiplier: int = 1        # 1 = normal, 3 = triple latency
    slippage_multiplier: int = 1       # 1 = normal, 3 = triple slippage


@dataclass
class StressTestResult:
    scenario: StressScenario
    backtest: BacktestResult
    pnl_vs_baseline: float     # % change relative to baseline
    sharpe_vs_baseline: float
    still_profitable: bool


DEFAULT_SCENARIOS = [
    StressScenario(name="Baseline"),
    StressScenario(name="2x Spread", spread_multiplier=2.0),
    StressScenario(name="3x Spread", spread_multiplier=3.0),
    StressScenario(name="50% Fill Rate", fill_rate_multiplier=0.5),
    StressScenario(name="25% Fill Rate", fill_rate_multiplier=0.25),
    StressScenario(name="3x Latency", latency_multiplier=3),
    StressScenario(name="3x Slippage", slippage_multiplier=3),
    StressScenario(name="Combined Stress", spread_multiplier=2.0, fill_rate_multiplier=0.5, slippage_multiplier=2),
]


class StressTester:
    def __init__(self, base_engine: BacktestEngine, scenarios: list[StressScenario] | None = None):
        self._base = base_engine
        self._scenarios = scenarios or DEFAULT_SCENARIOS

    def run(
        self,
        data: pd.DataFrame,
        probability_model: Callable[[pd.Series], float],
    ) -> list[StressTestResult]:
        results = []
        baseline_pnl: float | None = None
        baseline_sharpe: float | None = None

        for scenario in self._scenarios:
            engine = BacktestEngine(
                initial_bankroll=self._base.initial_bankroll,
                fee=self._base.fee,
                slippage_ticks=self._base.slippage_ticks * scenario.slippage_multiplier,
                latency_bars=self._base.latency_bars * scenario.latency_multiplier,
                fill_rate=min(self._base.fill_rate * scenario.fill_rate_multiplier, 1.0),
                edge_threshold=self._base.edge_threshold,
                kelly_fraction=self._base.kelly_fraction,
                stake_max=self._base.stake_max,
                max_positions=self._base.max_positions,
                daily_loss_limit=self._base.daily_loss_limit,
            )

            stressed_data = data.copy()
            if scenario.spread_multiplier != 1.0 and "bid" in data.columns and "ask" in data.columns:
                mid = (stressed_data["bid"] + stressed_data["ask"]) / 2
                half_spread = (stressed_data["ask"] - stressed_data["bid"]) / 2
                stressed_data["bid"] = mid - half_spread * scenario.spread_multiplier
                stressed_data["ask"] = mid + half_spread * scenario.spread_multiplier

            bt = engine.run(stressed_data, probability_model)

            if baseline_pnl is None:
                baseline_pnl = bt.total_pnl
                baseline_sharpe = bt.sharpe_ratio

            pnl_change = (
                (bt.total_pnl - baseline_pnl) / abs(baseline_pnl)
                if baseline_pnl != 0 else 0
            )
            sharpe_change = (
                (bt.sharpe_ratio - baseline_sharpe) / abs(baseline_sharpe)
                if baseline_sharpe != 0 else 0
            )

            results.append(StressTestResult(
                scenario=scenario,
                backtest=bt,
                pnl_vs_baseline=pnl_change,
                sharpe_vs_baseline=sharpe_change,
                still_profitable=bt.total_pnl > 0,
            ))

            logger.info(
                f"Stress [{scenario.name}]: "
                f"PnL=${bt.total_pnl:.2f} ({pnl_change:+.0%} vs baseline) | "
                f"Sharpe={bt.sharpe_ratio:.2f} | "
                f"Profitable={'YES' if bt.total_pnl > 0 else 'NO'}"
            )

        return results
