from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from track_insight.settings import get_settings


def get_engine(database_url: str | None = None) -> Engine:
    return create_engine(database_url or get_settings().database_url, pool_pre_ping=True)


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
