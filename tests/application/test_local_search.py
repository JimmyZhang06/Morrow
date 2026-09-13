"""Real encrypted sources and database policies, not mocked search results."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from life_coach.application.local_search import index_diary_fragment, search_diaries
from life_coach.application.source_entries import LocalAesGcmSourceContentProtector
from life_coach.modules.consent import (
    ConsentAction,
    ConsentPurpose,
    UserConsentCommand,
    grant_consent,
    revoke_consent,
)
from life_coach.modules.consent.service import resolve_consent, resolve_source_consents
from life_coach.modules.identity import DataClass, create_vault
from life_coach.modules.sources import (
    FragmentKind,
    SearchProjection,
    SourceType,
    append_source_revision,
    create_source_document,
    create_source_fragment,
    tombstone_source_document,
)
from life_coach.platform.model_registry import load_model_registry
from life_coach.shared.database import utc_now
from tests.sources.test_source_service import session as source_session

PROTECTOR = LocalAesGcmSourceContentProtector(b"local-search-tests-key-material-32-bytes")


@pytest.fixture
def session():
    load_model_registry()
    yield from source_session.__wrapped__()


def grant(db, vault_id, *, source_id=None, action=ConsentAction.GRANT,
          purpose=ConsentPurpose.SEARCH):
    now = utc_now()
    command = UserConsentCommand(
        vault_id=vault_id, principal_id=uuid4(), purpose=purpose, action=action,
        interaction_id=uuid4(), issued_at=now, expires_at=now + timedelta(minutes=1),
        source_document_id=source_id,
    )
    (grant_consent if action is ConsentAction.GRANT else revoke_consent)(db, command=command)


def entry(db, vault_id, content, *, document=None, sensitive=False,
          source_type=SourceType.NOTE):
    import hashlib

    doc_id = document.id if document else uuid4()
    rev_id, fragment_id = uuid4(), uuid4()
    number = 2 if document else 1
    digest = hashlib.sha256(content.encode()).hexdigest()
    ciphertext = PROTECTOR.seal(
        vault_id=vault_id, document_id=doc_id, object_id=rev_id,
        revision_no=number, kind="revision", plaintext=content,
    )
    if document:
        result = append_source_revision(
            db, vault_id=vault_id, document_id=doc_id, revision_id=rev_id,
            expected_revision=1, content_ciphertext=ciphertext,
            content_hash=digest, content_mime="text/plain",
        )
    else:
        result = create_source_document(
            db, vault_id=vault_id, document_id=doc_id, revision_id=rev_id,
            content_ciphertext=ciphertext, content_hash=digest,
            content_mime="text/plain", capture_timezone="Asia/Shanghai",
            data_class=DataClass.HIGHLY_SENSITIVE if sensitive else DataClass.NORMAL,
            source_type=source_type,
        )
    fragment = create_source_fragment(
        db, vault_id=vault_id, fragment_id=fragment_id, revision_id=rev_id,
        ordinal=0, char_start=0, char_end=len(content),
        fragment_kind=FragmentKind.PARAGRAPH,
        text_hash=digest,
        text_ciphertext=PROTECTOR.seal(
            vault_id=vault_id, document_id=doc_id, object_id=fragment_id,
            revision_no=number, kind="fragment", plaintext=content,
        ),
        data_class=DataClass.HIGHLY_SENSITIVE if sensitive else DataClass.NORMAL,
    )
    index_diary_fragment(db, vault_id=vault_id, fragment_id=fragment.id, plaintext=content)
    return result.document


def search(db, vault, query="项目", **kwargs):
    return search_diaries(db, vault_id=vault, query=query, protector=PROTECTOR, **kwargs)


def test_index_survives_unrelated_new_diary_and_unrelated_consent(session):
    vault = create_vault(session).id
    grant(session, vault)
    first = entry(session, vault, "今天完成了项目,很开心😀。")
    entry(session, vault, "今天去公园散步。")
    grant(session, vault, purpose=ConsentPurpose.PASSIVE_QA)
    result = search(session, vault)
    assert [hit.entry_id for hit in result.items] == [first.id]
    assert result.items[0].excerpt == "今天完成了项目,很开心😀。"
    assert result.items[0].quote_end == len(result.items[0].excerpt)


def test_revision_and_delete_never_return_old_material(session):
    vault = create_vault(session).id
    grant(session, vault)
    document = entry(session, vault, "项目今天进展顺利")
    entry(session, vault, "今天只想休息", document=document)
    assert not search(session, vault).items
    assert search(session, vault, "休息").items[0].revision == 2
    tombstone_source_document(session, vault_id=vault, document_id=document.id)
    assert not search(session, vault, "休息").items


def test_default_deny_and_explicit_backfill(session):
    vault = create_vault(session).id
    entry(session, vault, "我的项目已经完成")
    assert not session.scalars(select(SearchProjection)).all()
    assert not search(session, vault).items
    grant(session, vault)
    assert not search(session, vault).items
    assert search(session, vault, rebuild=True).indexed == 1
    assert len(search(session, vault).items) == 1


def test_source_opt_out_stays_excluded_and_other_vault_is_not_visible(session):
    first, second = create_vault(session).id, create_vault(session).id
    grant(session, first)
    grant(session, second)
    document = entry(session, first, "私人项目")
    entry(session, second, "另一个人的项目")
    grant(session, first, source_id=document.id, action=ConsentAction.REVOKE)
    grant(session, first)
    assert not search(session, first).items
    assert len(search(session, second).items) == 1


def test_revoke_clears_terms_and_regrant_requires_rebuild(session):
    vault = create_vault(session).id
    grant(session, vault)
    entry(session, vault, "项目要继续")
    grant(session, vault, action=ConsentAction.REVOKE)
    assert not search(session, vault).items
    for projection in session.scalars(select(SearchProjection)):
        assert projection.lexical_terms is None
    grant(session, vault)
    assert not search(session, vault).items
    search(session, vault, rebuild=True)
    assert search(session, vault).items


def test_sensitive_and_conversation_material_not_indexed(session):
    vault = create_vault(session).id
    grant(session, vault)
    entry(session, vault, "高度敏感项目", sensitive=True)
    entry(session, vault, "助手关于项目的回复", source_type=SourceType.CONVERSATION)
    assert not search(session, vault).items
    assert search(session, vault, rebuild=True).indexed == 0


def test_batch_consent_matches_single_resolver(session):
    vault = create_vault(session).id
    docs = [entry(session, vault, f"第 {i} 个记录") for i in range(4)]
    grant(session, vault)
    grant(session, vault, source_id=docs[0].id, action=ConsentAction.REVOKE)
    grant(session, vault, source_id=docs[1].id)
    grant(session, vault, action=ConsentAction.REVOKE)
    grant(session, vault, source_id=docs[2].id)
    foreign = create_vault(session).id
    other = entry(session, foreign, "另一个空间")
    batched = resolve_source_consents(
        session, vault_id=vault, purpose=ConsentPurpose.SEARCH,
        source_document_ids={doc.id for doc in [*docs, other]},
    )
    assert other.id not in batched
    for doc in docs:
        assert batched[doc.id] == resolve_consent(
            session, vault_id=vault, purpose=ConsentPurpose.SEARCH, source_document_id=doc.id,
        )


def test_backfill_advances_without_rebuilding_completed_rows(session, monkeypatch):
    monkeypatch.setattr("life_coach.application.local_search.REBUILD_BATCH", 2)
    vault = create_vault(session).id
    for i in range(3):
        entry(session, vault, f"项目 {i}")
    grant(session, vault)
    first = search(session, vault, rebuild=True)
    assert (first.rebuilt, first.pending, first.indexed) == (2, 1, 2)
    second = search(session, vault, rebuild=True)
    assert (second.rebuilt, second.pending, second.indexed) == (1, 0, 3)
    assert search(session, vault, rebuild=True).rebuilt == 0


def test_scan_limit_reports_partial_coverage(session, monkeypatch):
    monkeypatch.setattr("life_coach.application.local_search.MAX_SCAN", 2)
    vault = create_vault(session).id
    grant(session, vault)
    for i in range(3):
        entry(session, vault, f"项目 {i}")
    result = search(session, vault)
    assert result.truncated
    assert result.scanned == result.indexed == len(result.items) == 2


def test_long_diary_returns_relevant_passage_and_original_offsets(session):
    vault = create_vault(session).id
    grant(session, vault)
    content = "🙂今天散步。" * 200 + "项目原型已经成功完成。"
    entry(session, vault, content)
    hit = search(session, vault).items[0]
    assert "项目原型" in hit.excerpt
    assert hit.passage_start > 0
    assert content[hit.quote_start:hit.quote_end] == hit.excerpt
    assert hit.passage_start <= hit.quote_start < hit.quote_end <= hit.passage_end
    projection = session.scalar(select(SearchProjection))
    assert len(projection.lexical_passages) > 1
    grant(session, vault, action=ConsentAction.REVOKE)
    assert projection.lexical_passages is None
