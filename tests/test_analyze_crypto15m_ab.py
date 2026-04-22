import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.analyze_crypto15m_ab import analyze


def _seed_db(path: Path):
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.executescript(
        """
        create table lab_portfolios (
          id integer primary key,
          key text not null,
          initial_bankroll real
        );
        create table lab_orders (
          id integer primary key,
          portfolio_id integer not null,
          order_kind text
        );
        create table lab_fills (
          id integer primary key,
          portfolio_id integer not null,
          notional real
        );
        create table lab_decision_audit (
          id integer primary key,
          portfolio_id integer not null,
          decision text
        );
        create table lab_equity_points (
          id integer primary key,
          portfolio_id integer not null,
          equity real,
          realized_pnl real,
          unrealized_pnl real,
          drawdown_pct real,
          timestamp text
        );
        create table lab_positions (
          id integer primary key,
          portfolio_id integer not null,
          status text
        );
        """
    )
    cur.execute("insert into lab_portfolios(id, key, initial_bankroll) values (1, 'Crypto15m_t70_learned', 1500.0)")
    cur.execute("insert into lab_portfolios(id, key, initial_bankroll) values (2, 'Crypto15m_control', 1500.0)")
    cur.execute("insert into lab_orders(portfolio_id, order_kind) values (1, 'maker')")
    cur.execute("insert into lab_orders(portfolio_id, order_kind) values (1, 'taker')")
    cur.execute("insert into lab_fills(portfolio_id, notional) values (1, 10.0)")
    cur.execute("insert into lab_decision_audit(portfolio_id, decision) values (1, 'candidate')")
    cur.execute("insert into lab_decision_audit(portfolio_id, decision) values (1, 'accepted')")
    cur.execute("insert into lab_decision_audit(portfolio_id, decision) values (1, 'rejected')")
    cur.execute(
        """
        insert into lab_equity_points(portfolio_id, equity, realized_pnl, unrealized_pnl, drawdown_pct, timestamp)
        values (1, 1502.5, 2.5, 0.0, 0.01, '2026-04-19T12:00:00')
        """
    )
    con.commit()
    con.close()


def test_analyze_supports_sqlite_url(tmp_path):
    db_path = tmp_path / "crypto15m.sqlite"
    _seed_db(db_path)
    report = analyze(f"sqlite:///{db_path}", initial_bankroll=1500.0)
    assert report["exists"] is True
    assert report["verdict"] == "positive"
    assert report["best_learned"]["portfolio"] == "Crypto15m_t70_learned"
    assert report["best_learned"]["orders"] == 2
    assert report["best_learned"]["fills"] == 1
