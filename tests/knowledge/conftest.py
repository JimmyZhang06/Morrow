from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from life_coach.modules.knowledge import models as knowledge_models  # noqa: F401
from life_coach.modules.model_runs import models as model_run_models  # noqa: F401
from life_coach.shared.database import Base
from tests.knowledge.fakes import record_authoritative_source


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(database, "connect")
    def enable_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(database)
    try:
        yield database
    finally:
        Base.metadata.drop_all(database)
        database.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as database_session:
        yield database_session
        database_session.rollback()


@pytest.fixture
def add_fragment(session: Session):
    def add(
        *,
        vault_id: uuid.UUID,
        body: str | None = None,
        recorded_at: datetime = datetime(2025, 1, 1, tzinfo=UTC),
        data_class: str = "normal",
        authorization_snapshot_id: uuid.UUID | None = None,
        policy_epoch: int = 1,
        source_generation: int = 1,
        tombstoned: bool = False,
        consent_allowed: bool = True,
    ) -> uuid.UUID:
        return record_authoritative_source(
            session,
            vault_id=vault_id,
            body=body,
            recorded_at=recorded_at,
            data_class=data_class,
            authorization_snapshot_id=authorization_snapshot_id,
            policy_epoch=policy_epoch,
            source_generation=source_generation,
            tombstoned=tombstoned,
            consent_allowed=consent_allowed,
        )

    return add
