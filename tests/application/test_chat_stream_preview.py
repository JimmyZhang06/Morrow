from uuid import uuid4

import pytest

from life_coach.ai.chat_stream import stream_turn
from life_coach.application.conversations import (
    conversation_view,
    enqueue_turn,
    preserve_interrupted_answer,
)
from life_coach.modules.consent import ConsentAction, ConsentPurpose
from life_coach.modules.knowledge.enums import VerdictType
from tests.application.test_conversation_memories import (
    refresh_fence,
    seed,
    verdict,
)
from tests.application.test_conversations import (
    prepare,
    setup_chat,
)
from tests.application.test_conversations import session as chat_session
from tests.application.test_local_search import PROTECTOR, grant


@pytest.fixture
def session():
    yield from chat_session.__wrapped__()


def test_preview_is_ephemeral_and_current_context_bound(session):
    vault, _, _, chat, turn = setup_chat(session)
    session.expire_all()
    _, context = prepare(session, vault, turn)
    with stream_turn(vault, turn.id) as stream:
        stream.bind(context.prepared.context_snapshot.content_hash)
        stream.publish("unfinished draft")
        result = conversation_view(
            session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR
        )
        assert result["turns"][0]["preview"] == "unfinished draft"
        assert result["turns"][0]["reply"] is None
        assert turn.answer_ciphertext is None
        grant(session, vault, purpose=ConsentPurpose.SEARCH)
        result = conversation_view(
            session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR
        )
        assert result["turns"][0]["preview"] == "unfinished draft"
        grant(session, vault, purpose=ConsentPurpose.PASSIVE_QA, action=ConsentAction.REVOKE)
        result = conversation_view(
            session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR
        )
        assert result["turns"][0]["preview"] == ""
        assert stream.closed


def test_review_change_hides_draft_even_without_source_policy_change(session):
    vault, _, _, chat, turn, _, service, memory = seed(session)
    verdict(session, vault, service, memory, VerdictType.CONFIRM)
    refresh_fence(session, vault, turn)
    session.expire_all()
    _, context = prepare(session, vault, turn)
    with stream_turn(vault, turn.id) as stream:
        stream.bind(context.prepared.context_snapshot.content_hash)
        stream.publish("draft based on old interpretation")
        verdict(session, vault, service, memory, VerdictType.REJECT)
        view = conversation_view(
            session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR
        )
        assert view["turns"][0]["preview"] == ""


def test_canceled_turn_never_serves_partial_answer(session):
    vault, _, _, chat, turn = setup_chat(session)
    session.expire_all()
    _, context = prepare(session, vault, turn)
    with stream_turn(vault, turn.id) as stream:
        stream.bind(context.prepared.context_snapshot.content_hash)
        stream.publish("draft")
        turn.state = "canceled"
        result = conversation_view(
            session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR
        )
        assert result["turns"][0]["preview"] == ""
        assert stream.closed


def test_queued_snapshot_does_not_cancel_newly_started_stream(session):
    vault, _, _, chat, turn = setup_chat(session)
    # Simulate a view that read queued just before the worker claimed the turn.
    turn.state = "queued"
    with stream_turn(vault, turn.id) as stream:
        stream.bind("worker-context")
        stream.publish("draft")
        result = conversation_view(
            session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR
        )
        assert result["turns"][0]["preview"] == ""
        assert not stream.closed
        assert stream.preview("worker-context") == "draft"


@pytest.mark.parametrize("state", ["failed", "unknown"])
def test_interrupted_prose_survives_reload_and_can_be_continued(session, state):
    vault, principal, _, chat, turn = setup_chat(session)
    session.expire_all()
    _, context = prepare(session, vault, turn)
    with stream_turn(vault, turn.id) as stream:
        stream.bind(context.prepared.context_snapshot.content_hash)
        stream.publish("Received draft, not a verified claim")
        preserve_interrupted_answer(
            session, vault_id=vault, turn_id=turn.id, protector=PROTECTOR, stream=stream
        )
        turn.state = state
    session.flush()
    assert b"Received draft" not in turn.answer_ciphertext
    session.expire_all()
    view = conversation_view(session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR)
    assert view["turns"][0]["reply"] is None
    assert view["turns"][0]["partial"] == {"answer": "Received draft, not a verified claim"}
    following = enqueue_turn(
        session,
        vault_id=vault,
        conversation_id=chat.id,
        principal_id=principal.id,
        membership_generation=1,
        request_id=uuid4(),
        question="Please continue",
        model_binding="a" * 64,
        protector=PROTECTOR,
    )
    _, next_context = prepare(session, vault, following)
    history = next_context.prepared.context_snapshot.data["history"]
    assert history[0]["assistant_state"] == "incomplete_unverified"
    assert history[0]["assistant"]["answer"] == "Received draft, not a verified claim"
    grant(session, vault, purpose=ConsentPurpose.PASSIVE_QA, action=ConsentAction.REVOKE)
    hidden = conversation_view(
        session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR
    )
    assert hidden["turns"][0]["partial"] is None


def test_changed_context_does_not_preserve_old_draft(session):
    vault, _, _, _, turn = setup_chat(session)
    prepare(session, vault, turn)
    with stream_turn(vault, turn.id) as stream:
        stream.bind("old-context")
        stream.publish("stale draft")
        preserve_interrupted_answer(
            session, vault_id=vault, turn_id=turn.id, protector=PROTECTOR, stream=stream
        )
        assert turn.answer_ciphertext is None
