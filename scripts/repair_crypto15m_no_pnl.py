from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta

from dotenv import load_dotenv
from sqlalchemy import func

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.models import (  # noqa: E402
    AuditRow,
    LabEquityPointRow,
    LabFillRow,
    LabOrderRow,
    LabPortfolioRow,
    LabPositionRow,
)
from db.session import get_session  # noqa: E402
from lab.utils import utcnow  # noqa: E402


MARKER = "repair_crypto15m_no_native_token_pnl_20260421"


def _position_delta(entry_price: float, current_price: float) -> float:
    return float(current_price or entry_price) - float(entry_price or 0.0)


def repair(db_url: str, *, apply: bool, force: bool = False) -> dict[str, float | int]:
    session = get_session(db_url)
    try:
        marker_exists = (
            session.query(AuditRow)
            .filter(AuditRow.event_type == MARKER)
            .first()
            is not None
        )
        if marker_exists and not force:
            return {"skipped_marker_exists": 1, "positions": 0, "total_correction": 0.0}

        positions = (
            session.query(LabPositionRow)
            .join(LabPortfolioRow, LabPortfolioRow.id == LabPositionRow.portfolio_id)
            .filter(LabPortfolioRow.key.like("Crypto15m%"))
            .filter(LabPositionRow.side == "NO")
            .filter(LabPositionRow.status == "closed")
            .all()
        )

        total_correction = 0.0
        changed_positions: list[dict[str, float | int]] = []
        affected_portfolios: set[int] = set()
        for position in positions:
            if position.closed_at is None or position.opened_at is None:
                continue
            sell_fills = (
                session.query(LabFillRow)
                .join(LabOrderRow, LabOrderRow.id == LabFillRow.order_id)
                .filter(LabFillRow.portfolio_id == position.portfolio_id)
                .filter(LabFillRow.market_id == position.market_id)
                .filter(LabFillRow.token_id == position.token_id)
                .filter(LabOrderRow.action == "SELL")
                .filter(LabFillRow.timestamp >= position.opened_at)
                .filter(LabFillRow.timestamp <= position.closed_at + timedelta(seconds=1))
                .all()
            )
            sell_size = sum(float(fill.size or 0.0) for fill in sell_fills)
            sell_notional = sum(float(fill.notional or 0.0) for fill in sell_fills)
            if sell_size <= 0.0:
                continue

            entry_notional = float(position.entry_price or 0.0) * sell_size
            correction = 2.0 * (sell_notional - entry_notional)
            if abs(correction) <= 1e-9:
                continue

            old_pnl = float(position.realized_pnl or position.pnl or 0.0)
            new_pnl = old_pnl + correction
            total_correction += correction
            changed_positions.append({
                "position_id": int(position.id),
                "portfolio_id": int(position.portfolio_id),
                "old_pnl": old_pnl,
                "new_pnl": new_pnl,
                "correction": correction,
            })
            affected_portfolios.add(int(position.portfolio_id))
            if apply:
                position.realized_pnl = new_pnl
                position.pnl = new_pnl

        if apply:
            now = utcnow()
            for portfolio_id in affected_portfolios:
                portfolio = session.get(LabPortfolioRow, portfolio_id)
                if portfolio is None:
                    continue
                realized = (
                    session.query(func.coalesce(func.sum(LabPositionRow.realized_pnl), 0.0))
                    .filter(LabPositionRow.portfolio_id == portfolio_id)
                    .filter(LabPositionRow.status == "closed")
                    .scalar()
                ) or 0.0
                open_positions = (
                    session.query(LabPositionRow)
                    .filter(LabPositionRow.portfolio_id == portfolio_id)
                    .filter(LabPositionRow.status == "open")
                    .all()
                )
                unrealized = sum(
                    _position_delta(float(position.entry_price or 0.0), float(position.current_price or position.entry_price or 0.0))
                    * float(position.size or 0.0)
                    for position in open_positions
                )
                bankroll = float(portfolio.initial_bankroll or 0.0) + float(realized)
                equity = bankroll + float(unrealized)
                previous_peak = (
                    session.query(func.max(LabEquityPointRow.equity))
                    .filter(LabEquityPointRow.portfolio_id == portfolio_id)
                    .scalar()
                ) or equity
                peak = max(float(previous_peak or equity), equity)
                drawdown = max(0.0, (peak - equity) / peak) if peak > 0 else 0.0
                session.add(LabEquityPointRow(
                    portfolio_id=portfolio_id,
                    timestamp=now,
                    bankroll=bankroll,
                    realized_pnl=float(realized),
                    unrealized_pnl=float(unrealized),
                    equity=equity,
                    drawdown_pct=drawdown,
                ))
            session.add(AuditRow(
                timestamp=now,
                event_type=MARKER,
                details=json.dumps({
                    "positions": len(changed_positions),
                    "total_correction": total_correction,
                    "sample": changed_positions[:20],
                }),
            ))
            session.commit()

        return {
            "positions": len(changed_positions),
            "total_correction": total_correction,
            "affected_portfolios": len(affected_portfolios),
            "skipped_marker_exists": 0,
        }
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    load_dotenv(".env")
    db_url = args.db_url or os.environ.get("PREDICTION_TRADER_DATABASE_URL") or "sqlite:///prediction_trader.db"
    result = repair(db_url, apply=bool(args.apply), force=bool(args.force))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
