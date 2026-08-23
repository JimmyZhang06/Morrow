from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from life_coach.modules.identity import (
    StaleVaultSnapshot,
    capture_snapshot,
    create_vault,
    increment_policy_epoch,
    increment_source_generation,
    require_current_snapshot,
)
from life_coach.shared.database import Base


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]

    import life_coach.modules.consent.models
    import life_coach.modules.sources.models  # noqa: F401

    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def test_vault_fences_are_monotonic_and_invalidate_snapshots(session: Session) -> None:
    vault = create_vault(session)
    initial = capture_snapshot(session, vault.id)

    assert initial.policy_epoch == 0
    assert initial.source_generation == 0
    require_current_snapshot(session, initial)

    assert increment_source_generation(session, vault.id) == 1
    assert increment_source_generation(session, vault.id) == 2
    assert increment_policy_epoch(session, vault.id) == 1

    with pytest.raises(StaleVaultSnapshot):
        require_current_snapshot(session, initial)

    current = capture_snapshot(session, vault.id)
    assert current.policy_epoch == 1
    assert current.source_generation == 2
    require_current_snapshot(session, current)
