"""Database engine and session factories."""

from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from api.config import get_settings


def resolve_database_url() -> str:
    """Explicit DATABASE_URL if set; otherwise start the embedded dev Postgres."""
    url = get_settings().database_url
    if url:
        return url
    import pgserver  # dev-only dependency; real Postgres 16, user-space

    # Not in the repo dir: pgserver can't handle spaces in the path ("NFL Fantasy"),
    # and unix-socket paths have length limits anyway.
    datadir = Path.home() / ".fantasy" / "pgdata"
    datadir.parent.mkdir(exist_ok=True)
    server = pgserver.get_server(datadir)  # type: ignore[attr-defined]  # re-export quirk
    return server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)


@lru_cache
def get_engine() -> Engine:
    return create_engine(resolve_database_url(), pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)
