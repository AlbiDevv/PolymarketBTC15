from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from loguru import logger


@dataclass
class MonteCarloResult:
    n_simulations: int
    median_pnl: float
    mean_pnl: float
    percentile_5: float
    percentile_25: float
    percentile_75: float
    percentile_95: float
    probability_of_loss: float
    max_drawdown_median: float
    max_drawdown_95: float
    acceptable: bool  # True if 5th percentile > -30% of bankroll


class MonteCarloSimulator:
    """
    Monte Carlo simulation: shuffle trade order 10,000 times
    to estimate worst-case P&L distribution.
    """

    def __init__(self, n_simulations: int = 10_000, seed: int = 42):
        self.n_simulations = n_simulations
        self.seed = seed

    def simulate(
        self,
        trade_pnls: list[float],
        initial_bankroll: float,
        max_acceptable_loss_pct: float = 0.30,
    ) -> MonteCarloResult:
        """
        Args:
            trade_pnls: List of PnL values from closed trades.
            initial_bankroll: Starting bankroll.
            max_acceptable_loss_pct: Max acceptable loss as fraction of bankroll.

        Returns:
            MonteCarloResult with distribution statistics.
        """
        if not trade_pnls:
            return MonteCarloResult(
                n_simulations=0,
                median_pnl=0,
                mean_pnl=0,
                percentile_5=0,
                percentile_25=0,
                percentile_75=0,
                percentile_95=0,
                probability_of_loss=0,
                max_drawdown_median=0,
                max_drawdown_95=0,
                acceptable=False,
            )

        rng = np.random.default_rng(self.seed)
        pnls = np.array(trade_pnls)
        n_trades = len(pnls)

        final_pnls = np.zeros(self.n_simulations)
        max_drawdowns = np.zeros(self.n_simulations)

        for i in range(self.n_simulations):
            shuffled = rng.permutation(pnls)
            cumulative = np.cumsum(shuffled)
            equity_curve = initial_bankroll + cumulative

            final_pnls[i] = cumulative[-1]

            peak = np.maximum.accumulate(equity_curve)
            drawdowns = (peak - equity_curve) / peak
            max_drawdowns[i] = drawdowns.max()

        median_pnl = float(np.median(final_pnls))
        mean_pnl = float(np.mean(final_pnls))
        p5 = float(np.percentile(final_pnls, 5))
        p25 = float(np.percentile(final_pnls, 25))
        p75 = float(np.percentile(final_pnls, 75))
        p95 = float(np.percentile(final_pnls, 95))
        prob_loss = float(np.mean(final_pnls < 0))

        dd_median = float(np.median(max_drawdowns))
        dd_95 = float(np.percentile(max_drawdowns, 95))

        acceptable = p5 > -(max_acceptable_loss_pct * initial_bankroll)

        logger.info(
            f"Monte Carlo ({self.n_simulations} sims): "
            f"median PnL=${median_pnl:.2f}, "
            f"5th pctl=${p5:.2f}, "
            f"P(loss)={prob_loss:.1%}, "
            f"max DD 95th={dd_95:.1%}"
        )

        return MonteCarloResult(
            n_simulations=self.n_simulations,
            median_pnl=median_pnl,
            mean_pnl=mean_pnl,
            percentile_5=p5,
            percentile_25=p25,
            percentile_75=p75,
            percentile_95=p95,
            probability_of_loss=prob_loss,
            max_drawdown_median=dd_median,
            max_drawdown_95=dd_95,
            acceptable=acceptable,
        )
