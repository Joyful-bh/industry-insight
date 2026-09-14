from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from track_insight.config import get_settings


@lru_cache
def get_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    return create_engine(url, pool_pre_ping=True)


@contextmanager
def session_scope(database_url: str | None = None) -> Iterator[Session]:
    factory = sessionmaker(bind=get_engine(database_url), expire_on_commit=False)
    with factory.begin() as session:
        yield session


def check_database(engine: Engine | None = None) -> None:
    target = engine or get_engine()
    with target.connect() as connection:
        connection.execute(text("SELECT 1"))
