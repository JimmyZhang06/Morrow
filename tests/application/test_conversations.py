# ruff: noqa: RUF001
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from life_coach.application.conversations import (
    ConversationAuthority,
    ConversationPersister,
    ConversationReply,
    ConversationTitleConflict,
    ReplyCitation,
    conversation_view,
    delete_conversation,
    enqueue_turn,
    new_conversation,
    rename_conversation,
)
from life_coach.application.model_gateway import ModelInvocationDenied
from life_coach.modules.consent import ConsentAction, ConsentPurpose
from life_coach.modules.conversations import ConversationTurn
from life_coach.modules.identity import create_vault
from life_coach.modules.identity.models import Principal
from life_coach.modules.sources import list_source_documents, tombstone_source_document
from tests.application.test_local_search import PROTECTOR, entry, grant
from tests.application.test_local_search import session as source_session


@pytest.fixture
def session():
    yield from source_session.__wrapped__()


def setup_chat(db):
    vault = create_vault(db).id
    principal = Principal(issuer="synthetic", subject_fingerprint=uuid4().hex * 2)
    db.add(principal)
    db.flush()
    note = entry(db, vault, "今天在公园散步，回来后把项目原型完成了。")
    grant(db, vault, purpose=ConsentPurpose.PASSIVE_QA)
    chat = new_conversation(
        db, vault_id=vault, principal_id=principal.id, entry_ids=[note.id], allow_history=True
    )
    turn = enqueue_turn(
        db,
        vault_id=vault,
        conversation_id=chat.id,
        principal_id=principal.id,
        membership_generation=1,
        request_id=uuid4(),
        question="我最近有什么变化？",
        model_binding="a" * 64,
        protector=PROTECTOR,
    )
    return vault, principal, note, chat, turn


def prepare(db, vault, turn):
    turn.state = "running"
    db.flush()
    authority = ConversationAuthority(PROTECTOR)
    snapshot = authority.prepare(session=db, vault_id=vault, context_id=turn.id)
    sources = authority.sources.prepare(
        session=db,
        vault_id=vault,
        fragment_ids=snapshot.source_fragment_ids,
        purpose=ConsentPurpose.PASSIVE_QA,
    )
    context = SimpleNamespace(
        vault_id=vault, prepared=SimpleNamespace(context_snapshot=snapshot, snapshot=sources)
    )
    return authority, context


class AsyncAdapter:
    def __init__(self, db):
        self.db = db

    async def run_sync(self, fn):
        return fn(self.db)


def test_saved_question_is_source_but_does_not_pollute_diary_list(session):
    vault, _, note, chat, turn = setup_chat(session)
    assert [doc.id for doc in list_source_documents(session, vault_id=vault)] == [note.id]
    assert (
        conversation_view(session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR)[
            "turns"
        ][0]["question"]
        == "我最近有什么变化？"
    )
    assert turn.question_fragment_id is not None


def test_chat_requires_only_prose_and_program_normalizes_optional_metadata():
    assert ConversationReply.model_json_schema()["required"] == ["answer"]
    reply = ConversationReply.model_validate(
        {
            "answer": "A" * 7000,
            "uncertainty": None,
            "title": [],
            "care_letter": {"invalid": True},
            "citations": [None, {"quote": "invalid"}],
        }
    )
    assert len(reply.answer) == 7000
    assert reply.uncertainty
    assert reply.title is None
    assert reply.care_letter is None
    assert reply.citations == []


async def test_unrelated_diary_does_not_discard_inflight_reply(session):
    vault, _, _, chat, turn = setup_chat(session)
    authority, context = prepare(session, vault, turn)
    entry(session, vault, "An unrelated new diary entry.")
    await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(answer="Still valid", uncertainty="Limited to this conversation"),
    )
    result = conversation_view(
        session,
        vault_id=vault,
        conversation_id=chat.id,
        protector=PROTECTOR,
    )
    assert result["turns"][0]["reply"]["answer"] == "Still valid"


async def test_titles_are_available_without_opening_and_manual_names_survive_reload(session):
    vault, _, _, chat, turn = setup_chat(session)
    args = dict(vault_id=vault, conversation_id=chat.id, protector=PROTECTOR, summary_only=True)
    assert conversation_view(session, **args)["title"] == "我最近有什么变化？"
    authority, context = prepare(session, vault, turn)
    await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(answer="正文", uncertainty="待核对", title="最近的变化"),
    )
    summary = conversation_view(session, **args)
    assert summary["title"] == "最近的变化"
    assert summary["title_source"] == "ai"
    assert "最近的变化".encode() not in turn.answer_ciphertext
    rename_conversation(
        session,
        vault_id=vault,
        conversation_id=chat.id,
        protector=PROTECTOR,
        title="  我想保留的名字  ",
        expected_revision=0,
    )
    assert "我想保留的名字".encode() not in chat.title_ciphertext
    session.flush()
    session.expire_all()
    summary = conversation_view(session, **args)
    assert summary["title"] == "我想保留的名字"
    assert summary["title_source"] == "manual"
    assert summary["title_revision"] == 1
    with pytest.raises(ConversationTitleConflict):
        rename_conversation(
            session,
            vault_id=vault,
            conversation_id=chat.id,
            protector=PROTECTOR,
            title="过时的修改",
            expected_revision=0,
        )
    assert conversation_view(session, **args)["title"] == "我想保留的名字"
    with pytest.raises(ModelInvocationDenied):
        rename_conversation(
            session,
            vault_id=create_vault(session).id,
            conversation_id=chat.id,
            protector=PROTECTOR,
            title="越界",
            expected_revision=1,
        )
    delete_conversation(session, vault_id=vault, conversation_id=chat.id)
    assert chat.title_ciphertext is None
    with pytest.raises(ModelInvocationDenied):
        conversation_view(session, **args)


async def test_ai_title_is_hidden_when_its_evidence_is_revoked(session):
    vault, _, note, chat, turn = setup_chat(session)
    authority, context = prepare(session, vault, turn)
    await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(answer="正文", uncertainty="待核对", title="散步带来的变化"),
    )
    tombstone_source_document(session, vault_id=vault, document_id=note.id)
    summary = conversation_view(
        session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR, summary_only=True
    )
    assert summary["title"] == "我最近有什么变化？"
    assert summary["title_source"] == "question"


@pytest.mark.parametrize("value", ["", "   ", "过" * 61])
def test_empty_or_overlong_manual_names_are_rejected(session, value):
    vault, _, _, chat, _ = setup_chat(session)
    with pytest.raises(ValueError):
        rename_conversation(
            session,
            vault_id=vault,
            conversation_id=chat.id,
            protector=PROTECTOR,
            title=value,
            expected_revision=0,
        )


def test_bad_optional_ai_title_does_not_discard_the_answer():
    assert (
        ConversationReply(answer="正文", uncertainty="待核对", title="字" * 80).title == "字" * 60
    )
    assert (
        ConversationReply.model_validate(
            {"answer": "正文", "uncertainty": "待核对", "title": []}
        ).title
        is None
    )


async def test_legacy_chat_gets_ai_name_on_a_later_reply(session):
    vault, principal, _, chat, turn = setup_chat(session)
    authority, context = prepare(session, vault, turn)
    await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(answer="旧版本没有名称字段", uncertainty="待核对"),
    )
    later = enqueue_turn(
        session,
        vault_id=vault,
        conversation_id=chat.id,
        principal_id=principal.id,
        membership_generation=1,
        request_id=uuid4(),
        question="继续聊聊",
        model_binding="a" * 64,
        protector=PROTECTOR,
    )
    authority, context = prepare(session, vault, later)
    await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(answer="继续回答", uncertainty="待核对", title="项目与生活变化"),
    )
    for summary_only in (True, False):
        assert (
            conversation_view(
                session,
                vault_id=vault,
                conversation_id=chat.id,
                protector=PROTECTOR,
                summary_only=summary_only,
            )["title"]
            == "项目与生活变化"
        )


def test_idempotent_send_and_busy_guard(session):
    vault, principal, _, chat, turn = setup_chat(session)
    args = dict(
        vault_id=vault,
        conversation_id=chat.id,
        principal_id=principal.id,
        membership_generation=1,
        question="我最近有什么变化？",
        model_binding="a" * 64,
        protector=PROTECTOR,
    )
    assert enqueue_turn(session, request_id=turn.request_id, **args).id == turn.id
    with pytest.raises(ModelInvocationDenied):
        enqueue_turn(session, request_id=uuid4(), **args)
    assert len(session.scalars(select(ConversationTurn)).all()) == 1


async def test_encrypted_reply_exact_citation_and_typed_artifact(session):
    vault, _, note, chat, turn = setup_chat(session)
    authority, context = prepare(session, vault, turn)
    fragment = next(
        item for item in context.prepared.snapshot.fragments if item.document_id == note.id
    )
    artifact = await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(
            answer="这次散步后你完成了原型。是否更容易开始了？",
            uncertainty="只依据本次选定的记录，不能断定长期规律。",
            citations=[ReplyCitation(source_fragment_id=fragment.fragment_id, quote="在公园散步")],
        ),
    )
    assert artifact.conversation_turn_id == turn.id
    assert "完成了原型".encode() not in turn.answer_ciphertext
    view = conversation_view(session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR)
    assert view["turns"][0]["reply"]["citations"][0]["quote"] == "在公园散步"
    assert "quote" not in turn.citations[0]


@pytest.mark.parametrize("wrong_id", [False, True])
async def test_invalid_quote_or_id_cannot_be_persisted(session, wrong_id):
    vault, _, note, _, turn = setup_chat(session)
    authority, context = prepare(session, vault, turn)
    fragment = next(
        item for item in context.prepared.snapshot.fragments if item.document_id == note.id
    )
    await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(
            answer="错误引用",
            uncertainty="待核对",
            citations=[
                ReplyCitation(
                    source_fragment_id=uuid4() if wrong_id else fragment.fragment_id,
                    quote="原文中不存在的话",
                )
            ],
        ),
    )
    assert turn.answer_ciphertext is not None
    assert turn.citations == []
    assert turn.state == "completed"


@pytest.mark.parametrize("change", ["edit", "delete", "revoke", "cancel"])
async def test_changed_source_or_authority_rejects_inflight_reply(session, change):
    vault, _, note, _, turn = setup_chat(session)
    authority, context = prepare(session, vault, turn)
    if change == "edit":
        entry(session, vault, "新版本", document=note)
    elif change == "delete":
        tombstone_source_document(session, vault_id=vault, document_id=note.id)
    elif change == "revoke":
        grant(
            session,
            vault,
            purpose=ConsentPurpose.CROSS_RECORD_ANALYSIS,
            action=ConsentAction.REVOKE,
        )
    else:
        turn.state = "canceled"
    with pytest.raises(ModelInvocationDenied):
        await ConversationPersister(authority).persist(
            AsyncAdapter(session),
            context=context,
            result=ConversationReply(answer="迟到的回复", uncertainty="测试"),
        )
    assert turn.answer_ciphertext is None


async def test_source_deletion_clears_old_answer_and_chat_delete_hides_questions(session):
    vault, _, note, chat, turn = setup_chat(session)
    authority, context = prepare(session, vault, turn)
    await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(answer="回答已保存", uncertainty="测试"),
    )
    tombstone_source_document(session, vault_id=vault, document_id=note.id)
    assert turn.answer_ciphertext is None
    assert conversation_view(session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR)[
        "blocked"
    ]
    delete_conversation(session, vault_id=vault, conversation_id=chat.id)
    with pytest.raises(ModelInvocationDenied):
        conversation_view(session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR)


def test_foreign_chat_and_material_are_rejected(session):
    _vault, principal, note, chat, _ = setup_chat(session)
    other = create_vault(session).id
    with pytest.raises(ModelInvocationDenied):
        conversation_view(session, vault_id=other, conversation_id=chat.id, protector=PROTECTOR)
    grant(session, other, purpose=ConsentPurpose.PASSIVE_QA)
    with pytest.raises(ModelInvocationDenied):
        new_conversation(
            session,
            vault_id=other,
            principal_id=principal.id,
            entry_ids=[note.id],
            allow_history=True,
        )


@pytest.mark.parametrize(
    "purpose", [ConsentPurpose.PASSIVE_QA, ConsentPurpose.CROSS_RECORD_ANALYSIS]
)
@pytest.mark.parametrize("source_scoped", [False, True])
async def test_revocation_and_regrant_do_not_resurrect_saved_answer(
    session, purpose, source_scoped
):
    vault, _, note, chat, turn = setup_chat(session)
    authority, context = prepare(session, vault, turn)
    await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(answer="旧回答", uncertainty="测试"),
    )
    grant(
        session,
        vault,
        purpose=purpose,
        source_id=note.id if source_scoped else None,
        action=ConsentAction.REVOKE,
    )
    assert turn.answer_ciphertext is None
    grant(session, vault, purpose=purpose, source_id=note.id if source_scoped else None)
    view = conversation_view(session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR)
    assert view["turns"][0]["reply"] is None


async def test_editing_material_clears_completed_answer(session):
    vault, _, note, _, turn = setup_chat(session)
    authority, context = prepare(session, vault, turn)
    await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(answer="依据旧版的回答", uncertainty="测试"),
    )
    entry(session, vault, "修订后的日记", document=note)
    assert turn.answer_ciphertext is None
    assert turn.state == "canceled"


def test_source_optout_checked_before_decrypt_even_with_current_fence(session, monkeypatch):
    from life_coach.modules.identity import capture_snapshot

    vault, _, note, _, turn = setup_chat(session)
    grant(
        session,
        vault,
        purpose=ConsentPurpose.CROSS_RECORD_ANALYSIS,
        source_id=note.id,
        action=ConsentAction.REVOKE,
    )
    snapshot = capture_snapshot(session, vault)
    turn.state = "running"
    turn.policy_epoch = snapshot.policy_epoch
    turn.source_generation = snapshot.source_generation
    session.flush()

    def forbidden(*args, **kwargs):
        raise AssertionError("plaintext must not be opened after an opt-out")

    monkeypatch.setattr(type(PROTECTOR), "open", forbidden)
    with pytest.raises(ModelInvocationDenied, match="permission"):
        ConversationAuthority(PROTECTOR).prepare(
            session=session, vault_id=vault, context_id=turn.id
        )
