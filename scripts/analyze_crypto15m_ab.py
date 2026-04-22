from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


def _db_target(db: str | Path) -> tuple[str, bool]:
    raw = str(db)
    if raw.lower() in {"postgres", "postgresql", "pg"}:
        url = os.environ.get("PREDICTION_TRADER_DATABASE_URL", "")
        return url, bool(url)
    if "://" in raw:
        return raw, True
    path = Path(raw)
    return f"sqlite:///{path}", path.exists()


def _dicts(conn, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rows = conn.execute(text(query), params or {})
    return [dict(row._mapping) for row in rows]


def analyze(db_path: str | Path, *, initial_bankroll: float = 500.0) -> dict:
    db_target, exists = _db_target(db_path)
    if not exists:
        return {"db": str(db_path), "exists": False, "portfolios": [], "verdict": "missing_db"}

    engine = create_engine(db_target)
    with engine.begin() as conn:
        portfolios = _dicts(
            conn,
            """
            SELECT id, key, initial_bankroll
            FROM lab_portfolios
            WHERE key LIKE 'Crypto15m%'
            ORDER BY key
            """,
        )
        summaries: list[dict[str, Any]] = []
        for portfolio in portfolios:
            pid = int(portfolio["id"])
            orders = _dicts(
                conn,
                """
                SELECT
                  COUNT(*) AS orders,
                  SUM(CASE WHEN order_kind='maker' THEN 1 ELSE 0 END) AS maker_orders,
                  SUM(CASE WHEN order_kind='taker' THEN 1 ELSE 0 END) AS taker_orders
                FROM lab_orders WHERE portfolio_id=:pid
                """,
                {"pid": pid},
            )[0]
            fills = _dicts(
                conn,
                """
                SELECT COUNT(*) AS fills, COALESCE(SUM(notional), 0) AS filled_notional
                FROM lab_fills WHERE portfolio_id=:pid
                """,
                {"pid": pid},
            )[0]
            decisions = _dicts(
                conn,
                """
                SELECT
                  SUM(CASE WHEN decision='candidate' THEN 1 ELSE 0 END) AS candidates,
                  SUM(CASE WHEN decision='accepted' THEN 1 ELSE 0 END) AS accepted,
                  SUM(CASE WHEN decision='rejected' THEN 1 ELSE 0 END) AS rejected
                FROM lab_decision_audit WHERE portfolio_id=:pid
                """,
                {"pid": pid},
            )[0]
            latest_equity = _dicts(
                conn,
                """
                SELECT equity, realized_pnl, unrealized_pnl, drawdown_pct, timestamp
                FROM lab_equity_points
                WHERE portfolio_id=:pid
                ORDER BY timestamp DESC, id DESC
                LIMIT 1
                """,
                {"pid": pid},
            )
            max_dd = _dicts(
                conn,
                """
                SELECT COALESCE(MAX(drawdown_pct), 0) AS max_drawdown
                FROM lab_equity_points
                WHERE portfolio_id=:pid
                """,
                {"pid": pid},
            )[0]["max_drawdown"]
            open_positions = _dicts(
                conn,
                """
                SELECT COUNT(*) AS open_positions
                FROM lab_positions
                WHERE portfolio_id=:pid AND status='open'
                """,
                {"pid": pid},
            )[0]["open_positions"]

            orders_count = int(orders["orders"] or 0)
            fills_count = int(fills["fills"] or 0)
            equity = latest_equity[0]["equity"] if latest_equity else float(portfolio["initial_bankroll"] or initial_bankroll)
            realized = latest_equity[0]["realized_pnl"] if latest_equity else 0.0
            unrealized = latest_equity[0]["unrealized_pnl"] if latest_equity else 0.0
            summaries.append({
                "portfolio": portfolio["key"],
                "equity": float(equity or 0.0),
                "realized_pnl": float(realized or 0.0),
                "unrealized_pnl": float(unrealized or 0.0),
                "orders": orders_count,
                "fills": fills_count,
                "fill_rate": (fills_count / orders_count) if orders_count else 0.0,
                "maker_orders": int(orders["maker_orders"] or 0),
                "taker_orders": int(orders["taker_orders"] or 0),
                "filled_notional": float(fills["filled_notional"] or 0.0),
                "open_positions": int(open_positions or 0),
                "max_drawdown": float(max_dd or 0.0),
                "candidates": int(decisions["candidates"] or 0),
                "accepted": int(decisions["accepted"] or 0),
                "rejected": int(decisions["rejected"] or 0),
            })

    learned = [row for row in summaries if "_learned" in row["portfolio"]]
    control = [row for row in summaries if row["portfolio"].endswith("_control")]
    best = max(summaries, key=lambda row: row["equity"], default=None)
    learned_best = max(learned, key=lambda row: row["equity"], default=None)
    control_best = max(control, key=lambda row: row["equity"], default=None)
    if not summaries:
        verdict = "no_crypto15m_portfolios"
    elif sum(row["fills"] for row in summaries) == 0:
        verdict = "inconclusive_no_fills"
    elif learned_best and learned_best["equity"] > initial_bankroll and (
        control_best is None or learned_best["equity"] > control_best["equity"]
    ):
        verdict = "positive"
    elif learned_best and learned_best["equity"] > initial_bankroll:
        verdict = "positive_but_not_ab_winner"
    else:
        verdict = "negative_or_inconclusive"
    return {
        "db": str(db_path),
        "exists": True,
        "verdict": verdict,
        "best_portfolio": best,
        "best_learned": learned_best,
        "best_control": control_best,
        "portfolios": summaries,
    }


def _print_markdown(report: dict):
    print(f"Crypto15m A/B report: {report['verdict']}")
    print("")
    print("| Portfolio | Equity | Realized | Unrealized | Orders | Fills | Fill rate | Maker | Taker | Candidates | Accepted | Max DD |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in report["portfolios"]:
        print(
            f"| {row['portfolio']} | {row['equity']:.2f} | {row['realized_pnl']:.2f} | "
            f"{row['unrealized_pnl']:.2f} | {row['orders']} | {row['fills']} | "
            f"{row['fill_rate']:.2%} | {row['maker_orders']} | {row['taker_orders']} | "
            f"{row['candidates']} | {row['accepted']} | {row['max_drawdown']:.2%} |"
        )
    print("")
    print(json.dumps(report, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Analyze Crypto15m threshold A/B shadow run")
    parser.add_argument("--db", default="prediction_trader.db")
    parser.add_argument("--initial-bankroll", type=float, default=500.0)
    args = parser.parse_args()
    _print_markdown(analyze(args.db, initial_bankroll=args.initial_bankroll))


if __name__ == "__main__":
    main()
