# ruff: noqa: RUF001
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from life_coach.application.care_letters import check_diaries, configure, eligible, respond, view
from life_coach.application.conversations import ConversationPersister, ConversationReply
from life_coach.modules.consent import ConsentAction, ConsentPurpose
from life_coach.modules.conversations import Conversation, ConversationTurn
from life_coach.modules.identity.models import Vault
from life_coach.shared.database import utc_now
from tests.application.test_conversations import AsyncAdapter, prepare, setup_chat
from tests.application.test_conversations import session as chat_session
from tests.application.test_local_search import PROTECTOR, entry, grant


@pytest.fixture
def session():
    yield from chat_session.__wrapped__()


def enable(db, vault, principal):
    configure(db, vault_id=vault, principal_id=principal.id, enabled=True, presentation="gentle")


async def test_default_off_discards_unsolicited_model_letter(session):
    vault, _, _, _, turn = setup_chat(session)
    authority, context = prepare(session, vault, turn)
    assert context.prepared.context_snapshot.data["care_allowed"] is False
    await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(
            answer="正常回答", uncertainty="仍需核对", care_letter="不应出现的信"
        ),
    )
    assert view(session, vault_id=vault, protector=PROTECTOR)["letter"] is None
    assert not eligible(session, vault, [])


async def test_opt_in_letter_encrypted_persistent_and_rate_limited(session):
    vault, principal, note, _, turn = setup_chat(session)
    enable(session, vault, principal)
    # Refresh the running turn's authorization fence after explicit opt-in.
    from life_coach.modules.identity import capture_snapshot

    fence = capture_snapshot(session, vault)
    turn.policy_epoch, turn.source_generation = fence.policy_epoch, fence.source_generation
    authority, context = prepare(session, vault, turn)
    assert context.prepared.context_snapshot.data["care_allowed"] is True
    body = "最近似乎有些辛苦。\n如果你愿意，我们可以慢慢聊。"
    await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(answer="正常回答", uncertainty="仍需核对", care_letter=body),
    )
    assert body.encode() not in turn.answer_ciphertext
    session.expire_all()
    assert view(session, vault_id=vault, protector=PROTECTOR)["letter"]["body"] == body
    assert not eligible(session, vault, [note.id])
    respond(session, vault_id=vault, action="dismiss", letter_id=str(uuid4()))
    assert view(session, vault_id=vault, protector=PROTECTOR)["letter"] is not None
    respond(session, vault_id=vault, action="dismiss", letter_id=str(turn.id))
    assert view(session, vault_id=vault, protector=PROTECTOR)["letter"] is None
    assert not eligible(session, vault, [note.id])
    owner = session.get(Vault, vault)
    owner.care_settings = {
        **owner.care_settings,
        "last_letter_at": (utc_now() - timedelta(days=4)).isoformat(),
    }
    assert eligible(session, vault, [note.id])
    respond(session, vault_id=vault, action="pause", letter_id=None)
    assert not eligible(session, vault, [])
    respond(session, vault_id=vault, action="resume", letter_id=None)
    assert eligible(session, vault, [])
    configure(
        session, vault_id=vault, principal_id=principal.id, enabled=False, presentation="inbox"
    )
    assert not eligible(session, vault, [])


def test_source_opt_out_denies_proactive_use(session):
    vault, principal, note, _, _ = setup_chat(session)
    enable(session, vault, principal)
    grant(
        session,
        vault,
        purpose=ConsentPurpose.PROACTIVE_RESURFACING,
        source_id=note.id,
        action=ConsentAction.REVOKE,
    )
    assert not eligible(session, vault, [note.id])


def test_private_diary_requires_separate_opt_in(session):
    from life_coach.modules.identity.models import DataClass

    vault, principal, note, _, turn = setup_chat(session)
    note.data_class = DataClass.SENSITIVE
    enable(session, vault, principal)
    _, context = prepare(session, vault, turn)
    assert not context.prepared.context_snapshot.data["care_allowed"]
    configure(
        session,
        vault_id=vault,
        principal_id=principal.id,
        enabled=True,
        presentation="inbox",
        include_private_diaries=True,
    )
    _, context = prepare(session, vault, turn)
    assert context.prepared.context_snapshot.data["care_allowed"]
    configure(
        session,
        vault_id=vault,
        principal_id=principal.id,
        enabled=True,
        presentation="inbox",
        include_private_diaries=False,
    )
    _, context = prepare(session, vault, turn)
    assert not context.prepared.context_snapshot.data["care_allowed"]


def test_new_diary_check_is_hidden_and_not_repeated(session):
    vault, principal, _, _, first = setup_chat(session)
    first.state = "completed"
    enable(session, vault, principal)
    owner = session.get(Vault, vault)
    owner.care_settings = {
        **owner.care_settings,
        "checked_at": (utc_now() - timedelta(hours=1)).isoformat(),
    }
    entry(session, vault, "最近连续几天压力很大，总觉得事情做不完。")
    args = dict(
        vault_id=vault,
        principal_id=principal.id,
        membership_generation=1,
        model_binding="a" * 64,
        protector=PROTECTOR,
    )
    check_diaries(session, **args)
    hidden = session.scalar(select(Conversation).where(Conversation.care_origin.is_(True)))
    assert hidden is not None
    turn = session.scalar(
        select(ConversationTurn).where(ConversationTurn.conversation_id == hidden.id)
    )
    _, context = prepare(session, vault, turn)
    assert context.prepared.context_snapshot.data["care_check_only"] is True
    check_diaries(session, **args)
    assert (
        len(list(session.scalars(select(Conversation).where(Conversation.care_origin.is_(True)))))
        == 1
    )
