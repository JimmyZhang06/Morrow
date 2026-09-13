from uuid import uuid4

import pytest
from sqlalchemy import select

from life_coach.application.diary_index_jobs import run_index_batch
from life_coach.modules.consent import ConsentAction
from life_coach.modules.identity import create_vault
from life_coach.modules.identity.models import Principal
from life_coach.modules.sources.index_jobs import DiaryIndexJob
from life_coach.modules.sources.models import SearchProjection
from tests.application.test_local_search import (
    PROTECTOR,
    entry,
    grant,
    search,
)
from tests.application.test_local_search import (
    session as search_session,
)


@pytest.fixture
def session():
    yield from search_session.__wrapped__()


def job(db, vault):
    principal = Principal(issuer="synthetic", subject_fingerprint=uuid4().hex * 2)
    db.add(principal)
    db.flush()
    result = DiaryIndexJob(vault_id=vault, principal_id=principal.id, membership_generation=1)
    db.add(result)
    db.flush()
    return result


def test_cursor_survives_session_reload_and_resumes_without_duplicate_work(session, monkeypatch):
    monkeypatch.setattr("life_coach.application.diary_index_jobs.BATCH_SIZE", 2)
    vault = create_vault(session).id
    for i in range(5):
        entry(session, vault, f"项目 {i}")
    grant(session, vault)
    task = job(session, vault)
    task_id = task.id
    run_index_batch(session, vault_id=vault, job_id=task_id, protector=PROTECTOR)
    session.commit()
    session.expunge_all()
    task = session.get(DiaryIndexJob, task_id)
    assert task.processed == 2
    for _ in range(2):
        run_index_batch(session, vault_id=vault, job_id=task_id, protector=PROTECTOR)
        session.flush()
    assert (task.state, task.processed, task.indexed) == ("completed", 5, 5)
    assert len(search(session, vault).items) == 5


def test_batch_failure_rolls_back_cursor_and_indexes_together(session):
    vault = create_vault(session).id
    entry(session, vault, "项目记录")
    grant(session, vault)
    task = job(session, vault)
    with pytest.raises(RuntimeError), session.begin_nested():
        run_index_batch(session, vault_id=vault, job_id=task.id, protector=PROTECTOR)
        session.flush()
        raise RuntimeError("synthetic crash before commit")
    assert task.processed == 0
    assert not session.scalars(select(SearchProjection)).all()
    run_index_batch(session, vault_id=vault, job_id=task.id, protector=PROTECTOR)
    assert task.state == "completed"


@pytest.mark.parametrize("cancel", [True, False])
def test_cancel_or_revoke_prevents_further_work(session, cancel):
    vault = create_vault(session).id
    entry(session, vault, "项目记录")
    grant(session, vault)
    task = job(session, vault)
    if cancel:
        task.state = "canceled"
    else:
        grant(session, vault, action=ConsentAction.REVOKE)
    run_index_batch(session, vault_id=vault, job_id=task.id, protector=PROTECTOR)
    assert task.state == "canceled"
    assert task.processed == 0
    assert not session.scalars(select(SearchProjection)).all()


def test_other_vault_job_id_does_not_run(session):
    vault, other = create_vault(session).id, create_vault(session).id
    entry(session, vault, "项目记录")
    grant(session, vault)
    task = job(session, vault)
    run_index_batch(session, vault_id=other, job_id=task.id, protector=PROTECTOR)
    assert task.processed == 0
