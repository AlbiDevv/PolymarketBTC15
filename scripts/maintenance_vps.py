from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class FileCandidate:
    path: Path
    kind: str
    size_bytes: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def backup_candidates(apps_root: Path, project_root: Path) -> list[FileCandidate]:
    """Return only deploy backups/archives that are safe to remove on VPS."""

    apps_root = apps_root.resolve()
    project_root = project_root.resolve()
    candidates: list[FileCandidate] = []
    patterns = [
        ("prediction_trader_backup_*", "deploy_backup"),
        ("prediction_trader_data_backup_*", "data_backup"),
        ("*.tar.gz", "deploy_archive"),
        ("_goclaw_backups", "goclaw_backup"),
    ]
    for pattern, kind in patterns:
        for path in apps_root.glob(pattern):
            resolved = path.resolve()
            if resolved == project_root or project_root in resolved.parents:
                continue
            if not resolved.exists():
                continue
            candidates.append(FileCandidate(resolved, kind, _path_size(resolved)))
    candidates.sort(key=lambda item: str(item.path))
    return candidates


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, sql: str, params: Iterable[object]) -> int:
    return int(conn.execute(sql, tuple(params)).fetchone()[0] or 0)


def _execute(conn: sqlite3.Connection, sql: str, params: Iterable[object]) -> int:
    cursor = conn.execute(sql, tuple(params))
    return int(cursor.rowcount if cursor.rowcount is not None else 0)


def plan_db_cleanup(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    runtime_retention_days: int,
    marketdata_retention_hours: int,
    equity_highres_hours: int,
) -> tuple[list[dict], dict[str, str]]:
    runtime_cutoff = now - timedelta(days=runtime_retention_days)
    marketdata_cutoff = now - timedelta(hours=marketdata_retention_hours)
    equity_highres_cutoff = now - timedelta(hours=equity_highres_hours)
    cutoffs = {
        "runtime_cutoff": _dt(runtime_cutoff),
        "marketdata_cutoff": _dt(marketdata_cutoff),
        "equity_highres_cutoff": _dt(equity_highres_cutoff),
    }
    actions: list[dict] = []

    specs = [
        ("orderbooks_raw", "timestamp", "timestamp < ?", (cutoffs["marketdata_cutoff"],)),
        ("price_history", "timestamp", "timestamp < ?", (cutoffs["marketdata_cutoff"],)),
        ("lab_decision_audit", "timestamp", "timestamp < ?", (cutoffs["runtime_cutoff"],)),
        (
            "lab_orders",
            "created_at",
            "created_at < ? AND status NOT IN ('working', 'partial')",
            (cutoffs["runtime_cutoff"],),
        ),
        ("lab_fills", "timestamp", "timestamp < ?", (cutoffs["runtime_cutoff"],)),
        (
            "lab_positions",
            "closed_at",
            "status = 'closed' AND closed_at IS NOT NULL AND closed_at < ?",
            (cutoffs["runtime_cutoff"],),
        ),
        ("lab_ws_metrics", "timestamp", "timestamp < ?", (cutoffs["runtime_cutoff"],)),
        ("audit_log", "timestamp", "timestamp < ?", (cutoffs["runtime_cutoff"],)),
        ("lab_equity_points", "timestamp", "timestamp < ?", (cutoffs["runtime_cutoff"],)),
    ]
    for table, column, where_sql, params in specs:
        if not _table_exists(conn, table):
            continue
        rows = _count(conn, f"SELECT COUNT(*) FROM {table} WHERE {where_sql}", params)
        actions.append({
            "table": table,
            "column": column,
            "where": where_sql,
            "params": list(params),
            "rows": rows,
            "kind": "delete",
        })

    if _table_exists(conn, "lab_equity_points"):
        downsample_sql = """
            timestamp < ?
            AND timestamp >= ?
            AND id NOT IN (
                SELECT MIN(id)
                FROM lab_equity_points
                WHERE timestamp < ? AND timestamp >= ?
                GROUP BY portfolio_id, strftime('%Y-%m-%d %H:%M', timestamp)
            )
        """
        params = (
            cutoffs["equity_highres_cutoff"],
            cutoffs["runtime_cutoff"],
            cutoffs["equity_highres_cutoff"],
            cutoffs["runtime_cutoff"],
        )
        rows = _count(conn, f"SELECT COUNT(*) FROM lab_equity_points WHERE {downsample_sql}", params)
        actions.append({
            "table": "lab_equity_points",
            "column": "timestamp",
            "where": "downsample_to_1m",
            "params": list(params),
            "rows": rows,
            "kind": "downsample",
            "delete_where_sql": downsample_sql,
        })
    return actions, cutoffs


def apply_db_cleanup(conn: sqlite3.Connection, actions: list[dict]) -> list[dict]:
    applied: list[dict] = []
    for action in actions:
        rows = int(action.get("rows") or 0)
        if rows <= 0:
            applied.append({**action, "deleted": 0})
            continue
        if action.get("kind") == "downsample":
            sql = f"DELETE FROM {action['table']} WHERE {action['delete_where_sql']}"
        else:
            sql = f"DELETE FROM {action['table']} WHERE {action['where']}"
        deleted = _execute(conn, sql, action.get("params") or [])
        applied.append({**action, "deleted": deleted})
    conn.commit()
    return applied


def remove_file_candidates(candidates: list[FileCandidate]) -> list[dict]:
    removed: list[dict] = []
    for item in candidates:
        path = item.path
        size = item.size_bytes
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        removed.append({
            "path": str(path),
            "kind": item.kind,
            "size_bytes": size,
            "removed": True,
        })
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="VPS retention and deploy-backup cleanup.")
    parser.add_argument("--db", default="prediction_trader.db")
    parser.add_argument("--apps-root", default=str(PROJECT_ROOT.parent))
    parser.add_argument("--runtime-retention-days", type=int, default=14)
    parser.add_argument("--marketdata-retention-hours", type=int, default=48)
    parser.add_argument("--equity-highres-hours", type=int, default=48)
    parser.add_argument("--cleanup-backups", action="store_true")
    parser.add_argument("--vacuum", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    apps_root = Path(args.apps_root)
    now = _utcnow()
    result: dict = {
        "mode": "apply" if args.apply else "dry_run",
        "db_path": str(db_path),
        "project_root": str(PROJECT_ROOT),
        "apps_root": str(apps_root),
        "file_candidates": [],
        "db_actions": [],
        "cutoffs": {},
    }

    if args.cleanup_backups:
        file_candidates = backup_candidates(apps_root, PROJECT_ROOT)
        result["file_candidates"] = [
            {"path": str(item.path), "kind": item.kind, "size_bytes": item.size_bytes}
            for item in file_candidates
        ]
        if args.apply:
            result["removed_files"] = remove_file_candidates(file_candidates)

    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            actions, cutoffs = plan_db_cleanup(
                conn,
                now=now,
                runtime_retention_days=args.runtime_retention_days,
                marketdata_retention_hours=args.marketdata_retention_hours,
                equity_highres_hours=args.equity_highres_hours,
            )
            result["db_actions"] = [
                {key: value for key, value in action.items() if key != "delete_where_sql"}
                for action in actions
            ]
            result["cutoffs"] = cutoffs
            if args.apply:
                applied = apply_db_cleanup(conn, actions)
                result["db_actions_applied"] = [
                    {key: value for key, value in action.items() if key != "delete_where_sql"}
                    for action in applied
                ]
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if args.vacuum:
                    conn.execute("VACUUM")
        finally:
            conn.close()
    else:
        result["db_missing"] = True

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
