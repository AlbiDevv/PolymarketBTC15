import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.models import LabRuntimeStatusRow, MarketRow
from db.session import get_session, init_db, reset_db_sessions
from scripts.migrate_sqlite_to_postgres import migrate


def test_migrate_copies_runtime_tables_between_sqlalchemy_urls(tmp_path):
    source_url = f"sqlite:///{tmp_path / 'source.db'}"
    target_url = f"sqlite:///{tmp_path / 'target.db'}"
    reset_db_sessions()
    init_db(source_url)
    session = get_session(source_url)
    try:
        session.add(
            MarketRow(
                polymarket_id="btc-updown-1",
                question="Bitcoin Up or Down?",
                category="crypto",
                outcome="YES",
            )
        )
        session.add(
            LabRuntimeStatusRow(
                mode="shadow_maker",
                ws_connected=True,
                markets_fetched_last=1,
                eligible_markets_last=1,
                subscribed_tokens_last=2,
            )
        )
        session.commit()
    finally:
        session.close()

    result = migrate(source_url, target_url, truncate=True, chunk_size=100)

    assert any(item["table"] == "markets" and item["rows_copied"] == 1 for item in result["tables"])
    assert any(item["table"] == "lab_runtime_status" and item["rows_copied"] == 1 for item in result["tables"])

    target_session = get_session(target_url)
    try:
        assert target_session.query(MarketRow).count() == 1
        assert target_session.query(LabRuntimeStatusRow).count() == 1
    finally:
        target_session.close()
