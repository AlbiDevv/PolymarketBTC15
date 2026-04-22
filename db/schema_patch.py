"""SQLite additive columns for existing deployments (no Alembic)."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def patch_sqlite_schema(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    insp = inspect(engine)
    with engine.connect() as conn:
        if "markets" in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns("markets")}
            if "end_date" not in cols:
                conn.execute(text("ALTER TABLE markets ADD COLUMN end_date DATETIME"))
                conn.commit()
            if "tags" not in cols:
                conn.execute(text("ALTER TABLE markets ADD COLUMN tags JSON"))
                conn.commit()
        if "price_history" in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns("price_history")}
            if "no_mid" not in cols:
                conn.execute(text("ALTER TABLE price_history ADD COLUMN no_mid FLOAT"))
                conn.commit()
        if "positions" in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns("positions")}
            if "exit_reason" not in cols:
                conn.execute(text("ALTER TABLE positions ADD COLUMN exit_reason VARCHAR(32)"))
                conn.commit()
        if "lab_orders" in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns("lab_orders")}
            if "close_reason" not in cols:
                conn.execute(text("ALTER TABLE lab_orders ADD COLUMN close_reason VARCHAR(64)"))
                conn.commit()
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_lab_orders_portfolio_created "
                "ON lab_orders (portfolio_id, created_at)"
            ))
            conn.commit()
        if "lab_decision_audit" in insp.get_table_names():
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_lab_decision_audit_portfolio_ts "
                "ON lab_decision_audit (portfolio_key, timestamp)"
            ))
            conn.commit()
