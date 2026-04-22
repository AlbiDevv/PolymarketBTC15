from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from .models import Base
from .schema_patch import patch_sqlite_schema

_engines: dict[str, Engine] = {}
_session_factories: dict[str, sessionmaker] = {}


def _engine_kwargs(db_url: str) -> dict:
    kwargs: dict = {
        "echo": False,
        "pool_pre_ping": True,
    }
    if db_url.startswith("sqlite:///"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if db_url.endswith(":memory:"):
            kwargs["poolclass"] = StaticPool
    elif db_url.startswith("postgresql"):
        kwargs.update(
            pool_size=10,
            max_overflow=20,
            pool_recycle=1800,
        )
    return kwargs


def get_engine(db_url: str = "sqlite:///prediction_trader.db") -> Engine:
    engine = _engines.get(db_url)
    if engine is None:
        engine = create_engine(db_url, **_engine_kwargs(db_url))
        if engine.dialect.name == "sqlite":
            _install_sqlite_pragmas(engine)
        _engines[db_url] = engine
    return engine


def _install_sqlite_pragmas(engine: Engine) -> None:
    """Tune SQLite for the live shadow workload without changing semantics."""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA cache_size=-65536")
        cursor.close()


def get_session(db_url: str = "sqlite:///prediction_trader.db") -> Session:
    factory = _session_factories.get(db_url)
    if factory is None:
        engine = get_engine(db_url)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        _session_factories[db_url] = factory
    return factory()


def init_db(db_url: str = "sqlite:///prediction_trader.db"):
    engine = get_engine(db_url)
    Base.metadata.create_all(engine)
    patch_sqlite_schema(engine)


def reset_db_sessions() -> None:
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
    _session_factories.clear()
