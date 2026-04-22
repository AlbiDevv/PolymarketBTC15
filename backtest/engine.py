from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd
from loguru import logger


@dataclass
class BacktestTrade:
    timestamp: str
    market_id: str
    side: str
    entry_price: float
    stake: float
    model_prob: float
    market_prob: float
    edge: float
    outcome: str | None = None  # YES/NO after settlement
    pnl: float | None = None


@dataclass
class BacktestResult:
    trades: list[BacktestTrade]
    initial_bankroll: float
    final_bankroll: float
    total_pnl: float
    trade_count: int
    win_count: int
    hit_rate: float
    mean_edge: float
    sharpe_ratio: float
    max_drawdown: float
    max_drawdown_duration_days: int
    ev_expected: float
    ev_actual: float
    calibration: dict  # predicted prob bucket → actual win rate
    daily_pnl: list[float]


class BacktestEngine:
    """
    Simulates trading strategy on historical data.

    Features:
    - Realistic execution: slippage, latency, partial fills
    - Commission and spread handling
    - Kelly sizing
    - All risk limits
    """

    def __init__(
        self,
        initial_bankroll: float = 500,
        fee: float = 0.02,
        slippage_ticks: int = 1,
        latency_bars: int = 1,
        fill_rate: float = 0.8,
        edge_threshold: float = 0.05,
        kelly_fraction: float = 0.25,
        stake_max: float = 2.0,
        max_positions: int = 30,
        daily_loss_limit: float = 0.05,
    ):
        self.initial_bankroll = initial_bankroll
        self.fee = fee
        self.slippage_ticks = slippage_ticks
        self.latency_bars = latency_bars
        self.fill_rate = fill_rate
        self.edge_threshold = edge_threshold
        self.kelly_fraction = kelly_fraction
        self.stake_max = stake_max
        self.max_positions = max_positions
        self.daily_loss_limit = daily_loss_limit

    def run(
        self,
        data: pd.DataFrame,
        probability_model: Callable[[pd.Series], float],
    ) -> BacktestResult:
        """
        Run backtest on historical data.

        Args:
            data: DataFrame with columns:
                market_id, timestamp, bid, ask, mid, volume_24h,
                outcome (YES/NO/null), settled_at
            probability_model: Function(row) → estimated probability [0,1]

        Returns:
            BacktestResult with all metrics.
        """
        bankroll = self.initial_bankroll
        trades: list[BacktestTrade] = []
        open_positions: list[BacktestTrade] = []
        daily_pnl_map: dict[str, float] = {}
        peak_bankroll = bankroll
        max_dd = 0.0
        dd_start = None
        max_dd_duration = 0

        data = data.sort_values("timestamp").reset_index(drop=True)

        for idx, row in data.iterrows():
            date_str = str(row["timestamp"])[:10]

            # Check for settlements in open positions
            settled = []
            for pos in open_positions:
                if pos.market_id == row["market_id"] and row.get("outcome"):
                    won = (
                        (pos.side == "YES" and row["outcome"] == "YES")
                        or (pos.side == "NO" and row["outcome"] == "NO")
                    )
                    if won:
                        pos.pnl = pos.stake * (1 - pos.entry_price) * (1 - self.fee)
                    else:
                        pos.pnl = -pos.stake * pos.entry_price
                    pos.outcome = row["outcome"]
                    bankroll += pos.pnl
                    daily_pnl_map[date_str] = daily_pnl_map.get(date_str, 0) + pos.pnl
                    settled.append(pos)

            open_positions = [p for p in open_positions if p not in settled]

            # Skip if not enough data (latency simulation)
            if idx < self.latency_bars:
                continue

            # Skip if outcome already known (no look-ahead)
            if row.get("outcome"):
                continue

            # Risk checks
            if len(open_positions) >= self.max_positions:
                continue

            daily_loss = daily_pnl_map.get(date_str, 0)
            if bankroll > 0 and daily_loss < 0 and abs(daily_loss) / bankroll >= self.daily_loss_limit:
                continue

            # Get model probability
            p_model = probability_model(row)

            bid = row.get("bid", row["mid"] - 0.01)
            ask = row.get("ask", row["mid"] + 0.01)

            # Apply slippage
            ask_with_slippage = ask + self.slippage_ticks * 0.01
            bid_with_slippage = bid - self.slippage_ticks * 0.01

            # Calculate EV
            q = 1 - p_model
            ev_yes = p_model * (1 - ask_with_slippage - self.fee) - q * ask_with_slippage
            ev_no = q * (1 - (1 - bid_with_slippage) - self.fee) - p_model * (1 - bid_with_slippage)

            if ev_yes >= ev_no and ev_yes >= self.edge_threshold:
                side = "YES"
                price = ask_with_slippage
                edge = ev_yes
                p = p_model
            elif ev_no > ev_yes and ev_no >= self.edge_threshold:
                side = "NO"
                price = 1 - bid_with_slippage
                edge = ev_no
                p = 1 - p_model
            else:
                continue

            # Kelly sizing
            if price <= 0 or price >= 1:
                continue
            b = (1 - price) / price
            f = (b * p - (1 - p)) / b
            if f <= 0:
                continue
            stake = min(f * bankroll * self.kelly_fraction, self.stake_max)
            if stake < 1.0:
                continue

            # Partial fill simulation
            if np.random.random() > self.fill_rate:
                continue

            # Record trade
            trade = BacktestTrade(
                timestamp=str(row["timestamp"]),
                market_id=str(row["market_id"]),
                side=side,
                entry_price=price,
                stake=stake,
                model_prob=p_model,
                market_prob=row["mid"],
                edge=edge,
            )
            trades.append(trade)
            open_positions.append(trade)

            # Drawdown tracking
            if bankroll > peak_bankroll:
                peak_bankroll = bankroll
                dd_start = None
            dd = (peak_bankroll - bankroll) / peak_bankroll if peak_bankroll > 0 else 0
            max_dd = max(max_dd, dd)

        # Final metrics
        closed = [t for t in trades if t.pnl is not None]
        total_pnl = sum(t.pnl for t in closed)
        win_count = sum(1 for t in closed if t.pnl > 0)
        hit_rate = win_count / len(closed) if closed else 0

        daily_pnls = list(daily_pnl_map.values())
        sharpe = self._compute_sharpe(daily_pnls)

        ev_expected = np.mean([t.edge for t in trades]) if trades else 0
        ev_actual = np.mean([t.pnl / t.stake for t in closed]) if closed else 0

        calibration = self._compute_calibration(closed)

        return BacktestResult(
            trades=trades,
            initial_bankroll=self.initial_bankroll,
            final_bankroll=bankroll,
            total_pnl=total_pnl,
            trade_count=len(closed),
            win_count=win_count,
            hit_rate=hit_rate,
            mean_edge=float(ev_expected),
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            max_drawdown_duration_days=max_dd_duration,
            ev_expected=float(ev_expected),
            ev_actual=float(ev_actual),
            calibration=calibration,
            daily_pnl=daily_pnls,
        )

    @staticmethod
    def _compute_sharpe(daily_pnls: list[float]) -> float:
        if len(daily_pnls) < 2:
            return 0.0
        arr = np.array(daily_pnls)
        mean = arr.mean()
        std = arr.std()
        if std == 0:
            return 0.0
        return float((mean / std) * np.sqrt(252))

    @staticmethod
    def _compute_calibration(closed_trades: list[BacktestTrade]) -> dict:
        buckets = {
            "0.0-0.2": {"predicted": [], "actual": []},
            "0.2-0.4": {"predicted": [], "actual": []},
            "0.4-0.6": {"predicted": [], "actual": []},
            "0.6-0.8": {"predicted": [], "actual": []},
            "0.8-1.0": {"predicted": [], "actual": []},
        }

        for t in closed_trades:
            p = t.model_prob
            won = 1 if t.pnl and t.pnl > 0 else 0

            if p < 0.2:
                key = "0.0-0.2"
            elif p < 0.4:
                key = "0.2-0.4"
            elif p < 0.6:
                key = "0.4-0.6"
            elif p < 0.8:
                key = "0.6-0.8"
            else:
                key = "0.8-1.0"

            buckets[key]["predicted"].append(p)
            buckets[key]["actual"].append(won)

        result = {}
        for key, vals in buckets.items():
            if vals["predicted"]:
                result[key] = {
                    "mean_predicted": float(np.mean(vals["predicted"])),
                    "actual_win_rate": float(np.mean(vals["actual"])),
                    "count": len(vals["predicted"]),
                }
        return result
