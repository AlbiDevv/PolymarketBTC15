import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.session import get_engine, get_session, init_db, reset_db_sessions


def test_get_engine_is_cached_per_db_url(tmp_path):
    db1 = f"sqlite:///{tmp_path / 'one.db'}"
    db2 = f"sqlite:///{tmp_path / 'two.db'}"
    reset_db_sessions()

    engine1a = get_engine(db1)
    engine1b = get_engine(db1)
    engine2 = get_engine(db2)

    assert engine1a is engine1b
    assert engine1a is not engine2


def test_get_session_uses_matching_db_factory(tmp_path):
    db1 = f"sqlite:///{tmp_path / 'one.db'}"
    db2 = f"sqlite:///{tmp_path / 'two.db'}"
    reset_db_sessions()
    init_db(db1)
    init_db(db2)

    session1 = get_session(db1)
    session2 = get_session(db2)
    try:
        assert str(session1.bind.url) == db1
        assert str(session2.bind.url) == db2
    finally:
        session1.close()
        session2.close()
