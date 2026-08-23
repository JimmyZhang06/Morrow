from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from life_coach.modules.identity import Vault, create_vault
from life_coach.modules.knowledge import models as knowledge_models  # noqa: F401
from life_coach.modules.sources import (
    FragmentKind,
    create_source_document,
    create_source_fragment,
)
from life_coach.shared.database import Base


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
    def add(*, vault_id: uuid.UUID) -> uuid.UUID:
        if session.get(Vault, vault_id) is None:
            create_vault(session, vault_id=vault_id)
        source = create_source_document(
            session,
            vault_id=vault_id,
            content_ciphertext=b"test-encrypted-document",
            content_hash="a" * 64,
            content_mime="text/plain",
        )
        fragment = create_source_fragment(
            session,
            vault_id=vault_id,
            revision_id=source.revision.id,
            ordinal=0,
            text_ciphertext=b"test-encrypted-fragment",
            text_hash="b" * 64,
            fragment_kind=FragmentKind.PARAGRAPH,
        )
        return fragment.id

    return add
