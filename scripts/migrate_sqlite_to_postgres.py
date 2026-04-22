from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import inspect, select, text

import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.models import Base
from db.session import get_engine, init_db, reset_db_sessions


def _table_row_count(engine, table_name: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar() or 0)


def _truncate_tables(engine) -> None:
    table_names = [table.name for table in reversed(Base.metadata.sorted_tables)]
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            rendered = ", ".join(f'"{name}"' for name in table_names)
            conn.execute(text(f"TRUNCATE TABLE {rendered} RESTART IDENTITY CASCADE"))
            return
        for name in table_names:
            conn.execute(text(f'DELETE FROM "{name}"'))


def _copy_table(source_engine, target_engine, table, chunk_size: int) -> int:
    copied = 0
    offset = 0
    source_table = table
    while True:
        with source_engine.connect() as source_conn:
            rows = source_conn.execute(
                select(source_table).offset(offset).limit(chunk_size)
            ).mappings().all()
        if not rows:
            break
        payload = [dict(row) for row in rows]
        with target_engine.begin() as target_conn:
            target_conn.execute(table.insert(), payload)
        copied += len(payload)
        offset += len(payload)
    return copied


def _reset_postgres_sequences(engine) -> None:
    if engine.dialect.name != "postgresql":
        return
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if "id" not in {column["name"] for column in inspector.get_columns(table.name)}:
                continue
            conn.execute(
                text(
                    """
                    SELECT setval(
                        pg_get_serial_sequence(:table_name, 'id'),
                        COALESCE((SELECT MAX(id) FROM {table_name}), 1),
                        (SELECT COUNT(*) > 0 FROM {table_name})
                    )
                    """.replace("{table_name}", f'"{table.name}"')
                ),
                {"table_name": table.name},
            )


def migrate(source_url: str, target_url: str, *, truncate: bool, chunk_size: int) -> dict:
    reset_db_sessions()
    source_engine = get_engine(source_url)
    init_db(target_url)
    target_engine = get_engine(target_url)
    if truncate:
        _truncate_tables(target_engine)

    summary: dict[str, object] = {
        "source_url": source_url,
        "target_url": target_url,
        "tables": [],
    }
    for table in Base.metadata.sorted_tables:
        copied = _copy_table(source_engine, target_engine, table, chunk_size)
        summary["tables"].append(
            {
                "table": table.name,
                "rows_copied": copied,
                "target_rows": _table_row_count(target_engine, table.name),
            }
        )
    _reset_postgres_sequences(target_engine)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy runtime DB from SQLite into PostgreSQL (or another SQLAlchemy target).")
    parser.add_argument("--source-url", default="sqlite:///prediction_trader.db")
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--truncate", action="store_true")
    parser.add_argument("--chunk-size", type=int, default=1000)
    args = parser.parse_args()

    result = migrate(
        args.source_url,
        args.target_url,
        truncate=args.truncate,
        chunk_size=max(1, args.chunk_size),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
