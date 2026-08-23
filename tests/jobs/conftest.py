from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from life_coach.jobs.models import Job  # noqa: F401 - registers tables on Base.metadata
from life_coach.shared.database import Base


@pytest.fixture
def vault_a() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def vault_b() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(sqlite_engine: Engine) -> Iterator[Session]:
    with Session(sqlite_engine) as session:
        yield session
