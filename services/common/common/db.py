from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache

from sqlmodel import Session, SQLModel, create_engine

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://nexus_rag:nexus_rag@postgres:5432/nexus_rag"
)


@lru_cache(maxsize=1)
def get_engine():
    return create_engine(DATABASE_URL, echo=False)


def init_db() -> None:
    """Create tables if they don't exist. Fine for a dev skeleton; a real
    migration tool (e.g. Alembic) should replace this before production use."""
    SQLModel.metadata.create_all(get_engine())


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
