from collections.abc import Iterator
from contextlib import contextmanager

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from track_insight.infrastructure.models import Base
from track_insight.settings import get_settings


def get_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    if url.startswith("sqlite"):
        database_path = _sqlite_path(url)
        if database_path is not None:
            database_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


def initialize_database(engine: Engine | None = None) -> None:
    active_engine = engine or get_engine()
    Base.metadata.create_all(active_engine)


@contextmanager
def session_scope(database_url: str | None = None) -> Iterator[Session]:
    with Session(get_engine(database_url)) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def check_database(engine: Engine | None = None) -> None:
    active_engine = engine or get_engine()
    with active_engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def _sqlite_path(url: str) -> Path | None:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    value = url[len(prefix) :]
    if not value or value == ":memory:":
        return None
    return Path(value).expanduser()
