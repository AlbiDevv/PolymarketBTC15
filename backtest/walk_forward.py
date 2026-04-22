from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd
from loguru import logger

from .engine import BacktestEngine, BacktestResult


@dataclass
class WalkForwardResult:
    periods: list[PeriodResult]
    aggregate_pnl: float
    aggregate_sharpe: float
    aggregate_hit_rate: float
    all_periods_positive: bool
    launch_ready: bool  # all §5.4 criteria met


@dataclass
class PeriodResult:
    period_idx: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    backtest: BacktestResult


class WalkForwardValidator:
    """
    Walk-forward validation: train on window N, test on N+1, slide.

    Prevents overfitting by ensuring the model never sees future data.
    """

    def __init__(
        self,
        train_days: int = 60,
        test_days: int = 20,
        step_days: int = 20,
        min_periods: int = 3,
    ):
        self.train_days = train_days
        self.test_days = test_days
        self.step_days = step_days
        self.min_periods = min_periods

    def validate(
        self,
        data: pd.DataFrame,
        model_factory: Callable[[pd.DataFrame], Callable[[pd.Series], float]],
        engine: BacktestEngine,
    ) -> WalkForwardResult:
        """
        Args:
            data: Full historical dataset with 'timestamp' column.
            model_factory: Function(train_data) → probability_model(row).
                           Called once per period with training data.
            engine: BacktestEngine with desired parameters.

        Returns:
            WalkForwardResult with per-period and aggregate metrics.
        """
        data = data.sort_values("timestamp").reset_index(drop=True)
        data["timestamp"] = pd.to_datetime(data["timestamp"])

        min_date = data["timestamp"].min()
        max_date = data["timestamp"].max()

        periods: list[PeriodResult] = []
        period_idx = 0

        train_start = min_date
        while True:
            train_end = train_start + pd.Timedelta(days=self.train_days)
            test_start = train_end
            test_end = test_start + pd.Timedelta(days=self.test_days)

            if test_end > max_date:
                break

            train_data = data[
                (data["timestamp"] >= train_start)
                & (data["timestamp"] < train_end)
            ]
            test_data = data[
                (data["timestamp"] >= test_start)
                & (data["timestamp"] < test_end)
            ]

            if len(train_data) < 50 or len(test_data) < 10:
                train_start += pd.Timedelta(days=self.step_days)
                continue

            logger.info(
                f"WF Period {period_idx}: train {train_start.date()} → {train_end.date()}, "
                f"test {test_start.date()} → {test_end.date()}"
            )

            # Train model on training window only
            model = model_factory(train_data)

            # Test on hold-out window
            result = engine.run(test_data, model)

            periods.append(
                PeriodResult(
                    period_idx=period_idx,
                    train_start=str(train_start.date()),
                    train_end=str(train_end.date()),
                    test_start=str(test_start.date()),
                    test_end=str(test_end.date()),
                    backtest=result,
                )
            )

            logger.info(
                f"  PnL: ${result.total_pnl:.2f} | "
                f"Sharpe: {result.sharpe_ratio:.2f} | "
                f"Trades: {result.trade_count} | "
                f"Hit: {result.hit_rate:.1%}"
            )

            period_idx += 1
            train_start += pd.Timedelta(days=self.step_days)

        if not periods:
            return WalkForwardResult(
                periods=[],
                aggregate_pnl=0,
                aggregate_sharpe=0,
                aggregate_hit_rate=0,
                all_periods_positive=False,
                launch_ready=False,
            )

        # Aggregate
        total_pnl = sum(p.backtest.total_pnl for p in periods)
        total_trades = sum(p.backtest.trade_count for p in periods)
        total_wins = sum(p.backtest.win_count for p in periods)
        all_positive = all(p.backtest.total_pnl > 0 for p in periods)

        import numpy as np

        all_daily = []
        for p in periods:
            all_daily.extend(p.backtest.daily_pnl)
        agg_sharpe = 0.0
        if len(all_daily) >= 2:
            arr = np.array(all_daily)
            if arr.std() > 0:
                agg_sharpe = float((arr.mean() / arr.std()) * np.sqrt(252))

        agg_hit = total_wins / total_trades if total_trades > 0 else 0

        # §5.4 criteria
        launch_ready = (
            len(periods) >= self.min_periods
            and total_pnl > 0
            and agg_sharpe > 0.5
            and total_trades >= 200
            and all_positive
        )

        return WalkForwardResult(
            periods=periods,
            aggregate_pnl=total_pnl,
            aggregate_sharpe=agg_sharpe,
            aggregate_hit_rate=agg_hit,
            all_periods_positive=all_positive,
            launch_ready=launch_ready,
        )
