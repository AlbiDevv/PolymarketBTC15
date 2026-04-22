from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
from sqlalchemy.orm import Session

from db.models import PositionRow, PnlLogRow, SignalRow


class MetricsCalculator:
    """Computes performance metrics from trade history."""

    def __init__(self, session: Session):
        self._session = session

    def realized_pnl(self, since: datetime | None = None) -> float:
        q = self._session.query(PositionRow).filter(PositionRow.status == "closed")
        if since:
            q = q.filter(PositionRow.closed_at >= since)
        return sum(p.pnl or 0 for p in q.all())

    def trade_count(self, since: datetime | None = None) -> int:
        q = self._session.query(PositionRow).filter(PositionRow.status == "closed")
        if since:
            q = q.filter(PositionRow.closed_at >= since)
        return q.count()

    def hit_rate(self, since: datetime | None = None) -> float:
        q = self._session.query(PositionRow).filter(PositionRow.status == "closed")
        if since:
            q = q.filter(PositionRow.closed_at >= since)
        positions = q.all()
        if not positions:
            return 0.0
        wins = sum(1 for p in positions if (p.pnl or 0) > 0)
        return wins / len(positions)

    def max_drawdown(self) -> float:
        logs = (
            self._session.query(PnlLogRow)
            .order_by(PnlLogRow.date)
            .all()
        )
        if not logs:
            return 0.0

        peak = logs[0].bankroll
        max_dd = 0.0
        for log in logs:
            if log.bankroll > peak:
                peak = log.bankroll
            dd = (peak - log.bankroll) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
        return max_dd

    def sharpe_ratio(self, risk_free_rate: float = 0.0) -> float:
        logs = (
            self._session.query(PnlLogRow)
            .order_by(PnlLogRow.date)
            .all()
        )
        if len(logs) < 2:
            return 0.0

        daily_returns = []
        for i in range(1, len(logs)):
            prev = logs[i - 1].bankroll
            curr = logs[i].bankroll
            if prev > 0:
                daily_returns.append((curr - prev) / prev)

        if not daily_returns:
            return 0.0

        arr = np.array(daily_returns)
        mean_ret = arr.mean() - risk_free_rate / 252
        std_ret = arr.std()
        if std_ret == 0:
            return 0.0

        return float((mean_ret / std_ret) * np.sqrt(252))

    def edge_distribution(self, since: datetime | None = None) -> dict:
        q = self._session.query(PositionRow).filter(PositionRow.status == "closed")
        if since:
            q = q.filter(PositionRow.closed_at >= since)
        positions = q.all()

        if not positions:
            return {"mean": 0, "std": 0, "min": 0, "max": 0, "count": 0}

        pnls = [p.pnl or 0 for p in positions]
        arr = np.array(pnls)

        return {
            "mean": float(arr.mean()),
            "std": float(arr.std()),
            "min": float(arr.min()),
            "max": float(arr.max()),
            "count": len(pnls),
        }

    def brier_score(self, since: datetime | None = None) -> float:
        """
        Brier score: mean squared error between predicted probability and outcome.
        Lower is better. 0 = perfect, 0.25 = random (for 50/50).
        v3.0 addition for model calibration assessment.
        """
        q = self._session.query(SignalRow).filter(SignalRow.action_taken.is_(True))
        if since:
            q = q.filter(SignalRow.timestamp >= since)
        signals = q.all()

        if not signals:
            return 0.0

        scores = []
        for sig in signals:
            # Check if associated market has settled
            from db.models import MarketRow
            market = self._session.query(MarketRow).get(sig.market_id)
            if not market or not market.outcome:
                continue

            actual = 1.0 if market.outcome == "YES" else 0.0
            predicted = sig.model_probability
            scores.append((predicted - actual) ** 2)

        return float(np.mean(scores)) if scores else 0.0

    def calibration_by_bucket(self, since: datetime | None = None) -> dict:
        """
        v3.0: predicted probability bucket → actual win rate.
        """
        q = self._session.query(SignalRow).filter(SignalRow.action_taken.is_(True))
        if since:
            q = q.filter(SignalRow.timestamp >= since)
        signals = q.all()

        buckets: dict[str, list[tuple[float, float]]] = {
            "0.0-0.2": [], "0.2-0.4": [], "0.4-0.6": [],
            "0.6-0.8": [], "0.8-1.0": [],
        }

        for sig in signals:
            from db.models import MarketRow
            market = self._session.query(MarketRow).get(sig.market_id)
            if not market or not market.outcome:
                continue
            actual = 1.0 if market.outcome == "YES" else 0.0
            p = sig.model_probability

            if p < 0.2:
                buckets["0.0-0.2"].append((p, actual))
            elif p < 0.4:
                buckets["0.2-0.4"].append((p, actual))
            elif p < 0.6:
                buckets["0.4-0.6"].append((p, actual))
            elif p < 0.8:
                buckets["0.6-0.8"].append((p, actual))
            else:
                buckets["0.8-1.0"].append((p, actual))

        result = {}
        for key, pairs in buckets.items():
            if pairs:
                preds, actuals = zip(*pairs)
                result[key] = {
                    "mean_predicted": float(np.mean(preds)),
                    "actual_rate": float(np.mean(actuals)),
                    "count": len(pairs),
                    "calibration_error": abs(float(np.mean(preds)) - float(np.mean(actuals))),
                }
        return result

    def edge_forecast_vs_realized(self, since: datetime | None = None) -> dict:
        """v3.0: Compare predicted edge with realized PnL per signal."""
        q = self._session.query(SignalRow).filter(SignalRow.action_taken.is_(True))
        if since:
            q = q.filter(SignalRow.timestamp >= since)
        signals = q.all()

        forecast_edges = []
        realized_edges = []

        for sig in signals:
            positions = (
                self._session.query(PositionRow)
                .filter(PositionRow.market_id == sig.market_id)
                .filter(PositionRow.status == "closed")
                .all()
            )
            if not positions:
                continue
            avg_pnl = np.mean([p.pnl for p in positions if p.pnl is not None])
            avg_stake = np.mean([p.size for p in positions if p.size])
            if avg_stake > 0:
                forecast_edges.append(sig.edge)
                realized_edges.append(avg_pnl / avg_stake)

        if not forecast_edges:
            return {"forecast_mean": 0, "realized_mean": 0, "drift": 0}

        fm = float(np.mean(forecast_edges))
        rm = float(np.mean(realized_edges))
        return {
            "forecast_mean": fm,
            "realized_mean": rm,
            "drift": rm - fm,
            "count": len(forecast_edges),
        }

    def summary(self) -> dict:
        from datetime import timezone
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = today - timedelta(days=7)

        return {
            "total_pnl": self.realized_pnl(),
            "daily_pnl": self.realized_pnl(since=today),
            "weekly_pnl": self.realized_pnl(since=week_ago),
            "total_trades": self.trade_count(),
            "daily_trades": self.trade_count(since=today),
            "hit_rate": self.hit_rate(),
            "max_drawdown": self.max_drawdown(),
            "sharpe_ratio": self.sharpe_ratio(),
            "brier_score": self.brier_score(),
            "edge_drift": self.edge_forecast_vs_realized(),
        }
