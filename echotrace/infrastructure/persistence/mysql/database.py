"""Database and session factory helpers.

No connection is opened at import time.  Applications create one explicit
``Database`` from a URL and inject its unit of work into the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def create_engine(url: str, **kwargs: Any) -> Engine:
    """Create an engine without creating tables or performing I/O.

    ``pool_pre_ping`` is enabled for server databases by default; SQLite gets
    a small thread-safe configuration suitable for application and test use.
    """
    options = dict(kwargs)
    if url.startswith("sqlite"):
        options.setdefault("connect_args", {"check_same_thread": False})
    else:
        options.setdefault("pool_pre_ping", True)
        # Do not rely solely on a URL query parameter or a GUI client's
        # session defaults.  Every application connection must negotiate
        # utf8mb4 so Chinese objectives, titles, evidence and report JSON are
        # stored as Unicode instead of being lossy-converted to ``?``.
        connect_args = dict(options.get("connect_args") or {})
        connect_args.setdefault("charset", "utf8mb4")
        options["connect_args"] = connect_args
    return sa_create_engine(url, future=True, **options)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False, autoflush=False)


@dataclass(slots=True)
class Database:
    """Explicit database lifecycle used by app composition roots and tests."""

    engine: Engine
    session_factory: sessionmaker[Session]

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> "Database":
        engine = create_engine(url, **kwargs)
        return cls(engine=engine, session_factory=create_session_factory(engine))

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def drop_schema(self) -> None:
        Base.metadata.drop_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()


def create_database(url: str = "sqlite+pysqlite:///:memory:", **kwargs: Any) -> Database:
    return Database.from_url(url, **kwargs)


__all__ = ["Database", "create_database", "create_engine", "create_session_factory"]
