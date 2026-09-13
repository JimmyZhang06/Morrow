# ruff: noqa: RUF001
from uuid import uuid4

import pytest
from sqlalchemy import select

from life_coach.application.conversations import (
    ConversationPersister,
    ConversationReply,
    ReplyCitation,
    conversation_view,
    enqueue_turn,
    new_conversation,
)
from life_coach.application.model_runtime import ModelResultRejected
from life_coach.modules.consent import ConsentAction, ConsentPurpose
from life_coach.modules.conversations import clear_conversation_answers
from life_coach.modules.identity import create_vault
from life_coach.modules.sources.models import SourceFragment, SourceRevision
from tests.application.test_conversations import AsyncAdapter, prepare, setup_chat
from tests.application.test_conversations import session as chat_session
from tests.application.test_local_search import PROTECTOR, entry, grant


@pytest.fixture
def session():
    yield from chat_session.__wrapped__()


def seed(db):
    vault, principal, _, _, _ = setup_chat(db)
    grant(db, vault)
    note = entry(db, vault, "长跑训练完成后睡得很好。")
    chat = new_conversation(
        db, vault_id=vault, principal_id=principal.id, entry_ids=[], allow_history=True
    )
    return vault, principal, note, chat


def ask(db, vault, principal, chat, question="长跑训练", **kwargs):
    return enqueue_turn(
        db,
        vault_id=vault,
        principal_id=principal.id,
        conversation_id=chat.id,
        membership_generation=1,
        request_id=kwargs.pop("request_id", uuid4()),
        question=question,
        model_binding="a" * 64,
        protector=PROTECTOR,
        auto_retrieve=True,
        **kwargs,
    )


def test_automatic_recall_uses_current_question_and_immutable_manifest(session):
    vault, principal, note, chat = seed(session)
    other = entry(session, vault, "烘焙蛋糕成功。")
    turn = ask(session, vault, principal, chat)
    assert turn.retrieval["matched"] == 1
    _, context = prepare(session, vault, turn)
    docs = {item.document_id for item in context.prepared.snapshot.fragments}
    assert note.id in docs and other.id not in docs
    assert ask(session, vault, principal, chat, request_id=turn.request_id).id == turn.id
    turn.state = "completed"
    following = ask(session, vault, principal, chat, "烘焙蛋糕")
    _, next_context = prepare(session, vault, following)
    docs = {item.document_id for item in next_context.prepared.snapshot.fragments}
    assert other.id in docs and note.id not in docs
    assert next_context.prepared.context_snapshot.data["history"][0]["assistant"] is None


@pytest.mark.parametrize(
    "purpose",
    [ConsentPurpose.SEARCH, ConsentPurpose.PASSIVE_QA, ConsentPurpose.CROSS_RECORD_ANALYSIS],
)
def test_auto_recall_excludes_source_opt_out(session, purpose):
    vault, principal, note, chat = seed(session)
    grant(session, vault, source_id=note.id, purpose=purpose, action=ConsentAction.REVOKE)
    turn = ask(session, vault, principal, chat)
    assert turn.retrieval["matched"] == 0
    _, context = prepare(session, vault, turn)
    assert note.id not in {item.document_id for item in context.prepared.snapshot.fragments}


def test_auto_recall_excludes_other_vault_sensitive_and_no_match(session):
    vault, principal, _, chat = seed(session)
    other_vault = create_vault(session).id
    grant(session, other_vault)
    entry(session, other_vault, "星际飞船")
    entry(session, vault, "星际飞船", sensitive=True)
    turn = ask(session, vault, principal, chat, "星际飞船")
    assert turn.retrieval["matched"] == 0


def test_followup_reuses_previous_question_for_search(session):
    vault, principal, _, chat = seed(session)
    turn = ask(session, vault, principal, chat)
    turn.state = "completed"
    following = ask(session, vault, principal, chat, "为什么？")
    assert following.retrieval["matched"] == 1
    assert following.retrieval["used_previous_question"] is True


async def test_retrieved_source_change_hides_and_clears_saved_answer(session):
    vault, principal, note, chat = seed(session)
    turn = ask(session, vault, principal, chat)
    authority, context = prepare(session, vault, turn)
    fragment = session.scalar(
        select(SourceFragment).join(SourceRevision).where(SourceRevision.document_id == note.id)
    )
    await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(
            answer="训练后睡眠改善是一条线索。",
            uncertainty="单次记录不足以说明因果。",
            citations=[
                ReplyCitation(source_fragment_id=fragment.id, quote="长跑训练完成后睡得很好。")
            ],
        ),
    )
    assert (
        conversation_view(session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR)[
            "turns"
        ][0]["reply"]
        is not None
    )
    grant(
        session,
        vault,
        source_id=note.id,
        purpose=ConsentPurpose.SEARCH,
        action=ConsentAction.REVOKE,
    )
    assert (
        conversation_view(session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR)[
            "turns"
        ][0]["reply"]
        is None
    )
    clear_conversation_answers(session, vault_id=vault, document_id=note.id)
    assert turn.answer_ciphertext is None


def test_recall_cap_and_manifest_do_not_contain_diary_plaintext(session):
    vault, principal, _, chat = seed(session)
    for i in range(12):
        entry(session, vault, f"长跑训练记录 {i}")
    turn = ask(session, vault, principal, chat)
    assert turn.retrieval["matched"] == 8
    assert "长跑" not in str(turn.retrieval)


async def test_past_letter_requires_original_diary_citation(session):
    vault, principal, _, chat = seed(session)
    turn = ask(session, vault, principal, chat, experience="past_letter")
    authority, context = prepare(session, vault, turn)
    assert context.prepared.context_snapshot.data["experience"] == "past_letter"
    with pytest.raises(ModelResultRejected):
        await ConversationPersister(authority).persist(
            AsyncAdapter(session),
            context=context,
            result=ConversationReply(answer="没有原文的回信", uncertainty="测试", citations=[]),
        )
    with pytest.raises(ModelResultRejected):
        await ConversationPersister(authority).persist(
            AsyncAdapter(session),
            context=context,
            result=ConversationReply(
                answer="把提问冒充旧日记",
                uncertainty="测试",
                citations=[
                    ReplyCitation(source_fragment_id=turn.question_fragment_id, quote="长跑训练")
                ],
            ),
        )


def test_letter_mode_is_part_of_idempotency(session):
    from life_coach.application.model_gateway import ModelInvocationDenied

    vault, principal, _, chat = seed(session)
    turn = ask(session, vault, principal, chat, experience="past_letter")
    with pytest.raises(ModelInvocationDenied):
        ask(session, vault, principal, chat, request_id=turn.request_id)


async def test_no_match_letter_does_not_require_fabricated_evidence(session):
    vault, principal, _, chat = seed(session)
    turn = ask(session, vault, principal, chat, "无匹配词xyz", experience="past_letter")
    authority, context = prepare(session, vault, turn)
    await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(
            answer="没有找到相关日记，可以换个具体关键词。", uncertainty="检索范围有限。"
        ),
    )
    assert (
        conversation_view(session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR)[
            "turns"
        ][0]["retrieval"]["experience"]
        == "past_letter"
    )


async def test_complete_answers_survive_changed_retrieval_and_letter_mode(session):
    vault, principal, note, chat = seed(session)
    other = entry(session, vault, "烘焙蛋糕成功。")
    first = ask(session, vault, principal, chat)
    authority, context = prepare(session, vault, first)
    await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(answer="我们约好每周三回顾训练。", uncertainty="可随时调整"),
    )
    second = ask(session, vault, principal, chat, "烘焙蛋糕", experience="past_letter")
    _, context = prepare(session, vault, second)
    data = context.prepared.context_snapshot.data
    assert data["history"][0]["assistant"]["answer"] == "我们约好每周三回顾训练。"
    assert data["history"][0]["question"] == "长跑训练"
    assert data["history_coverage"] == {
        "previous_turns": 1,
        "included_turns": 1,
        "truncated": False,
    }
    assert {note.id, other.id} <= {f.document_id for f in context.prepared.snapshot.fragments}


async def test_long_conversation_keeps_first_fact_and_later_correction(session):
    from life_coach.application.conversations import ConversationAuthority

    vault, principal, _, chat = seed(session)
    questions = ["我的猫叫小雪。", "更正：我的猫叫小雨。"] + [
        f"继续讨论第{i}件事" for i in range(33)
    ]
    for question in questions:
        turn = ask(session, vault, principal, chat, question)
        turn.state = "completed"
        session.flush()
    final = ask(session, vault, principal, chat, "我的猫叫什么？")
    final.state = "running"
    session.flush()
    authority = ConversationAuthority(PROTECTOR)
    snapshot = authority.prepare(session=session, vault_id=vault, context_id=final.id)
    sources = authority.sources.prepare(
        session=session,
        vault_id=vault,
        fragment_ids=snapshot.source_fragment_ids,
        purpose=ConsentPurpose.PASSIVE_QA,
        max_fragments=256,
    )
    assert len(sources.fragments) > 32
    assert [item["question"] for item in snapshot.data["history"]] == questions
    assert snapshot.data["history_coverage"]["truncated"] is False


def test_context_limit_rejects_send_without_truncating_history(session, monkeypatch):
    import life_coach.application.conversations as conversations

    vault, principal, _, chat = seed(session)
    monkeypatch.setattr(conversations, "MAX_CONTEXT_CHARS", 10)
    with pytest.raises(conversations.ConversationContextLimit), session.begin_nested():
        ask(session, vault, principal, chat, "这段信息不能被静默截断")
    assert (
        conversation_view(session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR)[
            "turns"
        ]
        == []
    )


async def test_revoked_early_evidence_invalidates_dependent_answers(session):
    vault, principal, note, chat = seed(session)
    entry(session, vault, "烘焙蛋糕成功。")
    for question in ["长跑训练", "烘焙蛋糕"]:
        turn = ask(session, vault, principal, chat, question)
        authority, context = prepare(session, vault, turn)
        await ConversationPersister(authority).persist(
            AsyncAdapter(session),
            context=context,
            result=ConversationReply(answer="结合之前聊过的内容。", uncertainty="有待核对"),
        )
    grant(
        session,
        vault,
        source_id=note.id,
        purpose=ConsentPurpose.SEARCH,
        action=ConsentAction.REVOKE,
    )
    view = conversation_view(session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR)
    assert all(item["reply"] is None for item in view["turns"])
    assert view["turns"][1]["state"] == "outdated"
    final = ask(session, vault, principal, chat, "继续聊烘焙蛋糕")
    _, context = prepare(session, vault, final)
    assert all(
        item["assistant"] is None for item in context.prepared.context_snapshot.data["history"]
    )
