from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from sqlite3 import Connection as SQLiteConnection

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
    with engine.connect() as connection:
        driver_connection = connection.connection.driver_connection
        assert isinstance(driver_connection, SQLiteConnection)
        driver_connection.create_function(
            "clock_timestamp",
            0,
            lambda: datetime.now(UTC).isoformat(sep=" "),
        )
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(sqlite_engine: Engine) -> Iterator[Session]:
    with Session(sqlite_engine) as session:
        yield session
