"""Database session management — shared by scrapers, the FastAPI app, and the CLI.

Two entry points:

- `session_scope()` is a context manager for scripts and scraper code. It
  commits on successful exit and rolls back on exception.
- `get_session()` is a generator for FastAPI's `Depends()` dependency injection.

Both use the same `SessionLocal` sessionmaker bound to the engine built from
`settings.database_url`. The default URL is a local SQLite file; override via
`.env.local` once PostgreSQL is available.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    # Only applies to SQLite — allows the engine to be used across threads.
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Context manager for use in scripts and scraper code.

    Commits on successful exit; rolls back on any exception; always closes the
    session. Caller writes:

        with session_scope() as db:
            db.add(obj)
            ...
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency — yields a session, closes on request end.

    Does not commit on its own; endpoints commit explicitly when they mutate.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
