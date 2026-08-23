from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Column, MetaData, Table, UniqueConstraint, Uuid, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from life_coach.modules.knowledge import models as knowledge_models  # noqa: F401
from life_coach.shared.database import Base


def _source_fragment_table(metadata: MetaData) -> Table:
    existing = metadata.tables.get("source_fragment")
    if existing is not None:
        return existing
    return Table(
        "source_fragment",
        metadata,
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("vault_id", Uuid(as_uuid=True), nullable=False),
        UniqueConstraint("vault_id", "id", name="uq_source_fragment_vault_id_id"),
    )


SOURCE_FRAGMENT = _source_fragment_table(Base.metadata)


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
    def add(*, vault_id: uuid.UUID, fragment_id: uuid.UUID | None = None) -> uuid.UUID:
        value = fragment_id or uuid.uuid4()
        session.execute(SOURCE_FRAGMENT.insert().values(id=value, vault_id=vault_id))
        return value

    return add
