from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.maintenance_vps import backup_candidates, plan_db_cleanup


def test_backup_candidates_never_include_current_project(tmp_path):
    apps_root = tmp_path / "apps"
    project = apps_root / "prediction_trader"
    project.mkdir(parents=True)
    (project / "prediction_trader.db").write_text("keep", encoding="utf-8")
    backup = apps_root / "prediction_trader_backup_20260413_120000"
    backup.mkdir()
    (backup / "old.db").write_text("delete", encoding="utf-8")
    archive = apps_root / "prediction_trader_crypto15m_vps.tar.gz"
    archive.write_text("archive", encoding="utf-8")

    candidates = backup_candidates(apps_root, project)

    paths = {item.path for item in candidates}
    assert project not in paths
    assert backup.resolve() in paths
    assert archive.resolve() in paths


def test_db_cleanup_plan_counts_retention_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE orderbooks_raw (id INTEGER PRIMARY KEY, timestamp TEXT)")
    conn.execute("CREATE TABLE lab_decision_audit (id INTEGER PRIMARY KEY, timestamp TEXT)")
    now = datetime(2026, 4, 13, tzinfo=timezone.utc)
    old_market = (now - timedelta(hours=72)).isoformat()
    fresh_market = (now - timedelta(hours=1)).isoformat()
    old_runtime = (now - timedelta(days=20)).isoformat()
    conn.executemany("INSERT INTO orderbooks_raw(timestamp) VALUES (?)", [(old_market,), (fresh_market,)])
    conn.executemany("INSERT INTO lab_decision_audit(timestamp) VALUES (?)", [(old_runtime,), (fresh_market,)])

    actions, cutoffs = plan_db_cleanup(
        conn,
        now=now,
        runtime_retention_days=14,
        marketdata_retention_hours=48,
        equity_highres_hours=48,
    )

    by_table = {item["table"]: item for item in actions}
    assert by_table["orderbooks_raw"]["rows"] == 1
    assert by_table["lab_decision_audit"]["rows"] == 1
    assert cutoffs["runtime_cutoff"].startswith("2026-03-30")
    assert cutoffs["marketdata_cutoff"].startswith("2026-04-11")
