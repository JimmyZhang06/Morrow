# ruff: noqa: RUF001
import hashlib
from datetime import UTC
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import set_committed_value

from life_coach.application.conversation_memories import reviewed_context
from life_coach.application.conversations import (
    ConversationPersister,
    ConversationReply,
    conversation_view,
    enqueue_turn,
)
from life_coach.application.model_gateway import (
    KnowledgeAuthorizationSnapshotAdapter,
    KnowledgeEvidenceAuthorityAdapter,
    ModelInvocationDenied,
    SourceConsentAuthority,
)
from life_coach.application.source_entries import (
    ProtectedCorrectionSourceRecorder,
    ProtectedSourceFragmentPlaintextReader,
)
from life_coach.modules.consent import ConsentAction, ConsentPurpose
from life_coach.modules.identity import capture_snapshot
from life_coach.modules.knowledge.contracts import (
    ClaimProposal,
    CorrectionReplacement,
    EvidenceAnchor,
)
from life_coach.modules.knowledge.enums import (
    Attribution,
    CorrectionMode,
    EpistemicType,
    EvidenceExtractionReason,
    EvidenceRelation,
    EvidenceStrength,
    MemoryClaimKind,
    VerdictType,
)
from life_coach.modules.knowledge.service import MemoryService
from life_coach.modules.sources.models import SourceFragment, SourceRevision
from life_coach.shared.database import utc_now
from tests.application.test_conversations import AsyncAdapter, prepare, setup_chat
from tests.application.test_conversations import session as chat_session
from tests.application.test_local_search import PROTECTOR, grant


@pytest.fixture
def session():
    yield from chat_session.__wrapped__()


def seed(db):
    vault, principal, note, chat, turn = setup_chat(db)
    chat.include_reviewed_memories = True
    grant(db, vault, purpose=ConsentPurpose.LONG_TERM_INFERENCE)
    fragment = db.scalar(
        select(SourceFragment).join(SourceRevision).where(SourceRevision.document_id == note.id)
    )
    revisions = list(db.scalars(select(SourceRevision)))
    for revision in revisions:
        set_committed_value(revision, "created_at", revision.created_at.replace(tzinfo=UTC))
    db.info["keep_revisions"] = revisions
    authority = SourceConsentAuthority(ProtectedSourceFragmentPlaintextReader(PROTECTOR))

    class SQLiteCorrectionRecorder(ProtectedCorrectionSourceRecorder):
        def record_correction(self, **kwargs):
            result = super().record_correction(**kwargs)
            row = db.get(SourceFragment, result.source_fragment_id)
            revision = db.get(SourceRevision, row.revision_id)
            set_committed_value(revision, "created_at", revision.created_at.replace(tzinfo=UTC))
            db.info["keep_revisions"].append(revision)
            return result

    service = MemoryService(
        db,
        evidence_source_verifier=KnowledgeEvidenceAuthorityAdapter(authority),
        authorization_verifier=KnowledgeAuthorizationSnapshotAdapter(),
        correction_source_recorder=SQLiteCorrectionRecorder(PROTECTOR),
    )
    quote = "今天在公园散步"
    detail = service.create_claim(
        vault_id=vault,
        proposal=ClaimProposal(
            kind=MemoryClaimKind.EXPLICIT_FACT,
            canonical_text="散步后更容易开始工作。",
            epistemic_type=EpistemicType.STATED,
            attribution=Attribution.SELF_REPORT,
            valid_from=utc_now(),
            evidence=(
                EvidenceAnchor(
                    source_fragment_id=fragment.id,
                    relation=EvidenceRelation.SUPPORTS,
                    quote_hash=hashlib.sha256(quote.encode()).hexdigest(),
                    quote_start=0,
                    quote_end=len(quote),
                    extractor_reason=EvidenceExtractionReason.EXPLICIT_STATEMENT,
                    strength_band=EvidenceStrength.STRONG,
                ),
            ),
        ),
    )
    return vault, principal, note, chat, turn, fragment, service, detail


def refresh_fence(db, vault, turn):
    fence = capture_snapshot(db, vault)
    turn.policy_epoch, turn.source_generation = fence.policy_epoch, fence.source_generation
    db.flush()


def load(db, vault, fragment):
    return reviewed_context(db, vault_id=vault, material_ids=[fragment.id], protector=PROTECTOR)


def verdict(db, vault, service, detail, value, replacement=None):
    latest = service.get_detail(vault_id=vault, memory_id=detail.memory_id)
    return service.record_verdict(
        vault_id=vault,
        memory_id=detail.memory_id,
        verdict=value,
        expected_etag=latest.etag,
        replacement=replacement,
    )


def test_only_approved_current_memory_enters_context(session):
    vault, _, _, _, _, fragment, service, detail = seed(session)
    assert load(session, vault, fragment).items == []
    verdict(session, vault, service, detail, VerdictType.CONFIRM)
    context = load(session, vault, fragment)
    assert context.items[0]["statement"] == "散步后更容易开始工作。"
    assert context.input_refs[0].object_id == str(detail.version.derived_object_id)
    assert fragment.id in context.fragment_ids
    assert (
        reviewed_context(
            session, vault_id=uuid4(), material_ids=[fragment.id], protector=PROTECTOR
        ).items
        == []
    )


def test_correction_replaces_statement_with_user_source(session):
    vault, _, _, _, _, fragment, service, detail = seed(session)
    verdict(
        session,
        vault,
        service,
        detail,
        VerdictType.CORRECT,
        CorrectionReplacement(
            statement="只有在项目目标明确时，散步后我才更容易开始。",
            mode=CorrectionMode.INTERPRETATION_ERROR,
        ),
    )
    context = load(session, vault, fragment)
    assert len(context.items) == 1
    assert context.items[0]["review"] == "correct"
    assert context.items[0]["version"] == 2
    assert "目标明确" in context.items[0]["statement"]
    assert len(context.fragment_ids) >= 1
    assert context.input_refs[0].object_id != str(detail.version.derived_object_id)


@pytest.mark.parametrize("value", [VerdictType.REJECT, VerdictType.SNOOZE, VerdictType.RETRACT])
def test_rejected_snoozed_retracted_text_does_not_enter_prompt(session, value):
    vault, _, _, _, _, fragment, service, detail = seed(session)
    verdict(session, vault, service, detail, value)
    assert load(session, vault, fragment).items == []


def test_cross_record_source_optout_excludes_memory(session):
    vault, _, note, _, _, fragment, service, detail = seed(session)
    verdict(session, vault, service, detail, VerdictType.CONFIRM)
    assert load(session, vault, fragment).items
    grant(
        session,
        vault,
        source_id=note.id,
        purpose=ConsentPurpose.CROSS_RECORD_ANALYSIS,
        action=ConsentAction.REVOKE,
    )
    assert load(session, vault, fragment).items == []


async def test_review_change_blocks_inflight_answer(session):
    vault, _, _, _, turn, _, service, detail = seed(session)
    verdict(session, vault, service, detail, VerdictType.CONFIRM)
    refresh_fence(session, vault, turn)
    authority, context = prepare(session, vault, turn)
    verdict(session, vault, service, detail, VerdictType.REJECT)
    with pytest.raises(ModelInvocationDenied):
        await ConversationPersister(authority).persist(
            AsyncAdapter(session),
            context=context,
            result=ConversationReply(answer="过时回答", uncertainty="测试"),
        )
    assert turn.answer_ciphertext is None


async def test_old_answer_hidden_and_excluded_from_history_after_review_change(session):
    vault, principal, _, chat, turn, _, service, detail = seed(session)
    verdict(session, vault, service, detail, VerdictType.CONFIRM)
    refresh_fence(session, vault, turn)
    authority, context = prepare(session, vault, turn)
    await ConversationPersister(authority).persist(
        AsyncAdapter(session),
        context=context,
        result=ConversationReply(answer="原先的解释", uncertainty="测试"),
    )
    verdict(session, vault, service, detail, VerdictType.REJECT)
    view = conversation_view(session, vault_id=vault, conversation_id=chat.id, protector=PROTECTOR)
    assert view["turns"][0]["state"] == "outdated"
    assert view["turns"][0]["reply"] is None
    following = enqueue_turn(
        session,
        vault_id=vault,
        conversation_id=chat.id,
        principal_id=principal.id,
        membership_generation=1,
        request_id=uuid4(),
        question="重新想想",
        model_binding="a" * 64,
        protector=PROTECTOR,
    )
    _, next_context = prepare(session, vault, following)
    assert next_context.prepared.context_snapshot.data["history"][0]["assistant"] is None
    assert next_context.prepared.context_snapshot.data["reviewed_memories"] == []


def test_unrelated_policy_change_revalidates_original_evidence(session):
    vault, _, _, _, _, fragment, service, detail = seed(session)
    verdict(session, vault, service, detail, VerdictType.CONFIRM)
    before = load(session, vault, fragment)
    grant(session, vault, purpose=ConsentPurpose.SEARCH)
    after = load(session, vault, fragment)
    assert after.items == before.items
    assert after.fingerprint == before.fingerprint


def test_current_original_quote_must_still_match(session):
    from life_coach.modules.knowledge.models import EvidenceLink

    vault, _, _, _, _, fragment, service, detail = seed(session)
    verdict(session, vault, service, detail, VerdictType.CONFIRM)
    link = session.scalar(
        select(EvidenceLink).where(
            EvidenceLink.target_derived_object_id == detail.version.derived_object_id
        )
    )
    link.quote_hash = "0" * 64
    session.flush()
    assert load(session, vault, fragment).items == []


def test_old_source_revision_cannot_be_used_after_edit(session):
    from tests.application.test_local_search import entry

    vault, _, note, _, _, fragment, service, detail = seed(session)
    verdict(session, vault, service, detail, VerdictType.CONFIRM)
    entry(session, vault, "修改后的事实", document=note)
    assert load(session, vault, fragment).items == []


def test_corrected_memory_rejection_is_not_overridden_by_origin(session):
    vault, _, _, _, _, fragment, service, detail = seed(session)
    verdict(
        session,
        vault,
        service,
        detail,
        VerdictType.CORRECT,
        CorrectionReplacement(statement="修正的解释", mode=CorrectionMode.INTERPRETATION_ERROR),
    )
    assert load(session, vault, fragment).items
    verdict(session, vault, service, detail, VerdictType.REJECT)
    assert load(session, vault, fragment).items == []
