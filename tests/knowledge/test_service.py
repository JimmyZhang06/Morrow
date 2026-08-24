from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from life_coach.modules.knowledge.contracts import (
    ClaimProposal,
    CorrectionReplacement,
    CorrectionSourceAnchor,
    EvidenceAnchor,
)
from life_coach.modules.knowledge.enums import (
    Attribution,
    ConfidenceBand,
    CorrectionMode,
    DataClass,
    EpistemicType,
    EvidenceRelation,
    LifecycleState,
    MemoryClaimKind,
    ValidTimePrecision,
    VerdictType,
)
from life_coach.modules.knowledge.exceptions import (
    InvalidEvidenceError,
    InvalidTemporalIntervalError,
    InvalidVerdictError,
    MemoryNotFoundError,
    PolicyViolationError,
    RevisionConflictError,
)
from life_coach.modules.knowledge.models import EvidenceLink, MemoryClaim, UserVerdict
from life_coach.shared.database import Base
from tests.knowledge.fakes import (
    AllowingSafetyClassifier,
    evidence_anchor,
    record_authoritative_source,
)
from tests.knowledge.fakes import make_memory_service as MemoryService

T0 = datetime(2026, 1, 10, 9, tzinfo=UTC)
T1 = datetime(2026, 6, 18, 12, tzinfo=UTC)
REAL_FROM = datetime(2025, 3, 1, tzinfo=UTC)
SOURCE_T0 = datetime(2025, 1, 1, tzinfo=UTC)
SOURCE_T1 = datetime(2025, 2, 1, tzinfo=UTC)
SOURCE_T2 = datetime(2025, 3, 1, tzinfo=UTC)


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


class SourceRecorder:
    def __init__(self, *, fail_after_insert: bool = False) -> None:
        self.fail_after_insert = fail_after_insert
        self.created: list[uuid.UUID] = []
        self.data_classes: list[DataClass] = []

    def record_correction(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        correction_text: str,
        data_class: DataClass,
        recorded_at: datetime,
    ) -> CorrectionSourceAnchor:
        del memory_id
        fragment_id = record_authoritative_source(
            session,
            vault_id=vault_id,
            body=correction_text,
            recorded_at=recorded_at,
            data_class=data_class,
        )
        self.created.append(fragment_id)
        self.data_classes.append(data_class)
        if self.fail_after_insert:
            raise RuntimeError("simulated Source failure")
        return CorrectionSourceAnchor(source_fragment_id=fragment_id)


def anchor(
    fragment_id: uuid.UUID,
    *,
    relation: EvidenceRelation = EvidenceRelation.SUPPORTS,
    recorded_at: datetime = T0,
) -> EvidenceAnchor:
    del recorded_at
    return evidence_anchor(fragment_id, relation=relation)


def proposal(
    *evidence: EvidenceAnchor,
    kind: MemoryClaimKind = MemoryClaimKind.EXPLICIT_FACT,
    text: str = "I prefer to write in Chinese.",
    epistemic_type: EpistemicType = EpistemicType.STATED,
    attribution: Attribution = Attribution.SELF_REPORT,
    data_class: DataClass = DataClass.NORMAL,
    uncertainty: str | None = None,
    valid_to: datetime | None = None,
) -> ClaimProposal:
    return ClaimProposal(
        kind=kind,
        canonical_text=text,
        structured_payload={"conditional": True},
        epistemic_type=epistemic_type,
        attribution=attribution,
        uncertainty_text=uncertainty,
        valid_from=REAL_FROM,
        valid_to=valid_to,
        valid_time_precision=ValidTimePrecision.MONTH,
        valid_time_original="around March 2025",
        valid_timezone="Asia/Shanghai",
        confidence_band=ConfidenceBand.HIGH,
        pipeline_version="test-pipeline-v1",
        data_class=data_class,
        evidence=tuple(evidence),
    )


def test_low_risk_explicit_statement_can_auto_activate(session: Session, add_fragment) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    service = MemoryService(session, clock=lambda: T0)

    detail = service.create_claim(
        vault_id=vault_id,
        proposal=proposal(anchor(fragment_id)),
    )

    assert detail.version.state is LifecycleState.ACTIVE
    assert detail.version.epistemic_type is EpistemicType.STATED
    assert detail.version.attribution is Attribution.SELF_REPORT
    assert detail.allowed_uses == ("answer_when_asked", "proactive_coaching")
    assert "does not by itself establish objective truth" in detail.source_semantics


@pytest.mark.parametrize(
    ("claim_run_id", "evidence_run_id"),
    [
        (None, uuid.UUID("00000000-0000-0000-0000-000000000101")),
        (uuid.UUID("00000000-0000-0000-0000-000000000102"), None),
        (
            uuid.UUID("00000000-0000-0000-0000-000000000103"),
            uuid.UUID("00000000-0000-0000-0000-000000000104"),
        ),
    ],
)
def test_create_claim_rejects_partial_or_mismatched_model_run_lineage(
    session: Session,
    add_fragment,
    claim_run_id: uuid.UUID | None,
    evidence_run_id: uuid.UUID | None,
) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    proposed_evidence = replace(anchor(fragment_id), model_run_id=evidence_run_id)
    proposed_claim = replace(
        proposal(proposed_evidence),
        model_run_id=claim_run_id,
    )
    service = MemoryService(session, clock=lambda: T0)

    with pytest.raises(InvalidEvidenceError, match="model-run lineage"):
        service.create_claim(vault_id=vault_id, proposal=proposed_claim)


def test_imported_record_waits_for_user_confirmation(session: Session, add_fragment) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    service = MemoryService(session, clock=lambda: T0)

    detail = service.create_claim(
        vault_id=vault_id,
        proposal=proposal(
            anchor(fragment_id),
            attribution=Attribution.IMPORTED_RECORD,
        ),
    )

    assert detail.version.state is LifecycleState.CANDIDATE


def test_unconfirmed_personality_hypothesis_never_auto_activates(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    first = add_fragment(vault_id=vault_id, recorded_at=SOURCE_T0)
    second = add_fragment(vault_id=vault_id, recorded_at=SOURCE_T1)
    service = MemoryService(session, clock=lambda: T0)

    detail = service.create_claim(
        vault_id=vault_id,
        proposal=proposal(
            anchor(first),
            anchor(second),
            kind=MemoryClaimKind.PATTERN_HYPOTHESIS,
            text="You may have a stable avoidant personality pattern.",
            epistemic_type=EpistemicType.INFERRED,
            attribution=Attribution.MODEL_HYPOTHESIS,
            uncertainty="This is only one possible interpretation.",
        ),
    )

    assert detail.version.state is LifecycleState.CANDIDATE
    assert detail.version.confidence_band is ConfidenceBand.HIGH
    assert detail.allowed_uses == ("review_only", "answer_when_asked")


def test_confirmed_hypothesis_needs_two_times_and_keeps_provenance(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    first = add_fragment(vault_id=vault_id, recorded_at=SOURCE_T0)
    second = add_fragment(vault_id=vault_id, recorded_at=SOURCE_T1)
    service = MemoryService(session, clock=lambda: T0)
    detail = service.create_claim(
        vault_id=vault_id,
        proposal=proposal(
            anchor(first),
            anchor(second),
            kind=MemoryClaimKind.PATTERN_HYPOTHESIS,
            text="I may hesitate before taking leadership roles.",
            epistemic_type=EpistemicType.INFERRED,
            attribution=Attribution.MODEL_HYPOTHESIS,
            uncertainty="Counterexamples may exist.",
        ),
    )

    outcome = service.record_verdict(
        vault_id=vault_id,
        memory_id=detail.memory_id,
        verdict=VerdictType.CONFIRM,
        expected_etag=detail.etag,
    )
    confirmed = service.get_detail(vault_id=vault_id, memory_id=detail.memory_id)

    assert outcome.state is LifecycleState.ACTIVE
    assert confirmed.version.epistemic_type is EpistemicType.INFERRED
    assert confirmed.version.attribution is Attribution.MODEL_HYPOTHESIS
    assert confirmed.version.uncertainty == "Counterexamples may exist."
    assert confirmed.allowed_uses == ("answer_when_asked",)

    after_support_loss = service.remove_evidence(
        vault_id=vault_id,
        evidence_id=confirmed.evidence[0].id,
        expected_etag=confirmed.etag,
    )
    assert after_support_loss.version.state is LifecycleState.DISPUTED


def test_confirmation_without_two_independent_times_does_not_activate_hypothesis(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    first_fragment = add_fragment(vault_id=vault_id, recorded_at=SOURCE_T0)
    second_fragment = add_fragment(vault_id=vault_id, recorded_at=SOURCE_T0)
    service = MemoryService(session, clock=lambda: T0)
    detail = service.create_claim(
        vault_id=vault_id,
        proposal=proposal(
            anchor(first_fragment),
            anchor(second_fragment),
            kind=MemoryClaimKind.SELF_DESCRIPTION,
            epistemic_type=EpistemicType.INFERRED,
            attribution=Attribution.MODEL_HYPOTHESIS,
        ),
    )

    outcome = service.record_verdict(
        vault_id=vault_id,
        memory_id=detail.memory_id,
        verdict=VerdictType.CONFIRM,
        expected_etag=detail.etag,
    )

    assert outcome.state is LifecycleState.CANDIDATE


def test_hypothesis_evidence_must_use_distinct_fragments_and_times(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    service = MemoryService(session, clock=lambda: T0)
    second_anchor = evidence_anchor(fragment_id, start=6, end=10)
    detail = service.create_claim(
        vault_id=vault_id,
        proposal=proposal(
            anchor(fragment_id),
            second_anchor,
            kind=MemoryClaimKind.PATTERN_HYPOTHESIS,
            epistemic_type=EpistemicType.INFERRED,
            attribution=Attribution.MODEL_HYPOTHESIS,
        ),
    )

    outcome = service.record_verdict(
        vault_id=vault_id,
        memory_id=detail.memory_id,
        verdict=VerdictType.CONFIRM,
        expected_etag=detail.etag,
    )

    assert outcome.state is LifecycleState.CANDIDATE


@pytest.mark.parametrize(
    ("text", "payload"),
    [
        ("You may have depression.", {"diagnosis": "depression"}),
        ("The user has major depressive disorder.", {}),
        ("The user has schizophrenia.", {}),
        ("我有精神分裂症。", {}),
        ("你患有广泛性焦虑障碍。", {}),
        ("你可能患有人格障碍。", {}),
        ("Estimated suicide risk", {"suicide_risk_score": 0.23}),
        ("Opaque model output", {"suicideRiskScore": 0.23}),
        ("Opaque model output", {"诊断": "F20"}),
        ("Opaque model output", {"自杀风险评分": 0.23}),
    ],
)
def test_model_diagnostic_conclusions_are_not_persisted(
    session: Session,
    text: str,
    payload: dict[str, object],
) -> None:
    vault_id = uuid.uuid4()
    service = MemoryService(session, clock=lambda: T0)
    unsafe = proposal(
        kind=MemoryClaimKind.PATTERN_HYPOTHESIS,
        text=text,
        epistemic_type=EpistemicType.INFERRED,
        attribution=Attribution.MODEL_HYPOTHESIS,
    )
    unsafe = replace(unsafe, structured_payload=payload)

    with pytest.raises(PolicyViolationError):
        service.create_claim(vault_id=vault_id, proposal=unsafe)

    assert session.scalar(select(func.count()).select_from(MemoryClaim)) == 0


def test_user_reported_diagnosis_stays_sensitive_self_report(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    service = MemoryService(session, clock=lambda: T0)

    detail = service.create_claim(
        vault_id=vault_id,
        proposal=proposal(
            anchor(fragment_id),
            text="I recorded that my doctor diagnosed depression.",
            epistemic_type=EpistemicType.STATED,
            attribution=Attribution.SELF_REPORT,
            data_class=DataClass.HIGHLY_SENSITIVE,
        ),
    )

    assert detail.version.state is LifecycleState.CANDIDATE
    assert detail.version.attribution is Attribution.SELF_REPORT
    assert "proactive_coaching" not in detail.allowed_uses


def test_clinical_self_report_cannot_use_normal_classification(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    service = MemoryService(session, clock=lambda: T0)

    with pytest.raises(PolicyViolationError):
        service.create_claim(
            vault_id=vault_id,
            proposal=proposal(
                anchor(fragment_id),
                text="My doctor diagnosed depression.",
                data_class=DataClass.NORMAL,
            ),
        )


def test_memory_requires_supporting_source_and_consistent_provenance(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    service = MemoryService(session, clock=lambda: T0)
    with pytest.raises(InvalidEvidenceError):
        service.create_claim(
            vault_id=vault_id,
            proposal=proposal(
                epistemic_type=EpistemicType.USER_AUTHORED,
                attribution=Attribution.SELF_REPORT,
            ),
        )

    fragment_id = add_fragment(vault_id=vault_id)
    with pytest.raises(PolicyViolationError):
        service.create_claim(
            vault_id=vault_id,
            proposal=proposal(
                anchor(fragment_id),
                epistemic_type=EpistemicType.USER_AUTHORED,
                attribution=Attribution.MODEL_HYPOTHESIS,
            ),
        )


def test_verdicts_append_and_stale_etag_conflicts_atomically(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    service = MemoryService(session, clock=lambda: T0)
    detail = service.create_claim(vault_id=vault_id, proposal=proposal(anchor(fragment_id)))

    snoozed = service.record_verdict(
        vault_id=vault_id,
        memory_id=detail.memory_id,
        verdict=VerdictType.SNOOZE,
        expected_etag=detail.etag,
    )
    snoozed_detail = service.get_detail(vault_id=vault_id, memory_id=detail.memory_id)
    assert snoozed_detail.allowed_uses == ("review_only",)
    with pytest.raises(RevisionConflictError):
        service.record_verdict(
            vault_id=vault_id,
            memory_id=detail.memory_id,
            verdict=VerdictType.REJECT,
            expected_etag=detail.etag,
        )
    assert session.scalar(select(func.count()).select_from(UserVerdict)) == 1

    rejected = service.record_verdict(
        vault_id=vault_id,
        memory_id=detail.memory_id,
        verdict=VerdictType.REJECT,
        expected_etag=snoozed.etag,
    )
    events = list(session.scalars(select(UserVerdict).order_by(UserVerdict.sequence_no)))

    assert rejected.state is LifecycleState.DISPUTED
    rejected_detail = service.get_detail(vault_id=vault_id, memory_id=detail.memory_id)
    assert rejected_detail.allowed_uses == ("review_only",)
    assert [item.verdict for item in events] == [VerdictType.SNOOZE, VerdictType.REJECT]
    assert events[0].created_at < events[1].created_at
    assert events[-1].reason_optional is None


def test_terminal_version_rejects_more_verdicts_without_appending(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    service = MemoryService(session, clock=lambda: T0)
    detail = service.create_claim(vault_id=vault_id, proposal=proposal(anchor(fragment_id)))
    retracted = service.record_verdict(
        vault_id=vault_id,
        memory_id=detail.memory_id,
        verdict=VerdictType.RETRACT,
        expected_etag=detail.etag,
    )

    with pytest.raises(InvalidVerdictError):
        service.record_verdict(
            vault_id=vault_id,
            memory_id=detail.memory_id,
            verdict=VerdictType.CONFIRM,
            expected_etag=retracted.etag,
        )

    assert session.scalar(select(func.count()).select_from(UserVerdict)) == 1


def test_correction_creates_new_source_and_bitemporal_version(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    clock = MutableClock(T0)
    recorder = SourceRecorder()
    service = MemoryService(
        session,
        correction_source_recorder=recorder,
        clock=clock,
    )
    original = service.create_claim(
        vault_id=vault_id,
        proposal=proposal(
            anchor(fragment_id),
            text="I do not want to manage people.",
            uncertainty="This reflected that period.",
        ),
    )
    clock.value = T1

    outcome = service.record_verdict(
        vault_id=vault_id,
        memory_id=original.memory_id,
        verdict=VerdictType.CORRECT,
        replacement=CorrectionReplacement(
            statement="At that time, I did want to manage people.",
            mode=CorrectionMode.INTERPRETATION_ERROR,
            uncertainty_text="This reflected that period.",
            confidence_band=ConfidenceBand.HIGH,
        ),
        expected_etag=original.etag,
    )
    current = service.get_detail(vault_id=vault_id, memory_id=original.memory_id)

    assert outcome.version_no == 2
    assert len(recorder.created) == 1
    assert len(current.history) == 2
    assert current.history[0].state is LifecycleState.SUPERSEDED
    assert current.history[0].system_to == current.history[1].system_from == T1
    assert current.version.statement == "At that time, I did want to manage people."
    assert current.version.epistemic_type is EpistemicType.USER_AUTHORED
    assert current.version.attribution is Attribution.SELF_REPORT
    assert current.version.uncertainty == "This reflected that period."
    assert current.verdicts[0].target_derived_object_id == original.version.derived_object_id

    old_view = service.get_as_of_version(
        vault_id=vault_id,
        memory_id=original.memory_id,
        real_at=REAL_FROM,
        system_at=T0 + timedelta(hours=1),
    )
    boundary_view = service.get_as_of_version(
        vault_id=vault_id,
        memory_id=original.memory_id,
        real_at=REAL_FROM,
        system_at=T1,
    )

    assert old_view.statement == "I do not want to manage people."
    assert old_view.state is LifecycleState.ACTIVE
    assert boundary_view.statement == "At that time, I did want to manage people."


def test_clinical_correction_escalates_source_claim_and_verdict_classification(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    recorder = SourceRecorder()
    clock = MutableClock(T0)
    service = MemoryService(
        session,
        correction_source_recorder=recorder,
        safety_classifier=AllowingSafetyClassifier(
            data_class=DataClass.HIGHLY_SENSITIVE,
            allows_proactive=False,
        ),
        clock=clock,
    )
    original = service.create_claim(vault_id=vault_id, proposal=proposal(anchor(fragment_id)))
    clock.value = T1

    outcome = service.record_verdict(
        vault_id=vault_id,
        memory_id=original.memory_id,
        verdict=VerdictType.CORRECT,
        replacement=CorrectionReplacement(
            statement="My doctor diagnosed depression.",
            mode=CorrectionMode.INTERPRETATION_ERROR,
        ),
        expected_etag=original.etag,
    )
    verdict = session.get(UserVerdict, outcome.verdict_id)
    claim = session.get(MemoryClaim, original.memory_id)

    assert recorder.data_classes == [DataClass.HIGHLY_SENSITIVE]
    assert verdict is not None and verdict.data_class is DataClass.HIGHLY_SENSITIVE
    assert claim is not None and claim.data_class is DataClass.HIGHLY_SENSITIVE


def test_real_memory_inbox_honors_vault_state_snooze_and_reject(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    other_vault = uuid.uuid4()
    candidate_fragment = add_fragment(vault_id=vault_id)
    active_fragment = add_fragment(vault_id=vault_id)
    other_fragment = add_fragment(vault_id=other_vault)
    service = MemoryService(session, clock=lambda: T0)
    candidate = service.create_claim(
        vault_id=vault_id,
        proposal=proposal(
            anchor(candidate_fragment),
            kind=MemoryClaimKind.PATTERN_HYPOTHESIS,
            epistemic_type=EpistemicType.INFERRED,
            attribution=Attribution.MODEL_HYPOTHESIS,
        ),
    )
    service.create_claim(vault_id=vault_id, proposal=proposal(anchor(active_fragment)))
    service.create_claim(
        vault_id=other_vault,
        proposal=proposal(
            anchor(other_fragment),
            kind=MemoryClaimKind.PATTERN_HYPOTHESIS,
            epistemic_type=EpistemicType.INFERRED,
            attribution=Attribution.MODEL_HYPOTHESIS,
        ),
    )

    page = service.list_inbox(vault_id=vault_id)
    assert [item.memory_id for item in page.items] == [candidate.memory_id]

    snoozed = service.record_verdict(
        vault_id=vault_id,
        memory_id=candidate.memory_id,
        verdict=VerdictType.SNOOZE,
        expected_etag=candidate.etag,
    )
    assert service.list_inbox(vault_id=vault_id).items == ()
    service.record_verdict(
        vault_id=vault_id,
        memory_id=candidate.memory_id,
        verdict=VerdictType.REJECT,
        expected_etag=snoozed.etag,
    )
    assert service.list_inbox(vault_id=vault_id).items == ()


def test_failed_source_correction_rolls_back_every_domain_side_effect(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    first_fragment = add_fragment(vault_id=vault_id)
    clock = MutableClock(T0)
    recorder = SourceRecorder(fail_after_insert=True)
    service = MemoryService(
        session,
        correction_source_recorder=recorder,
        clock=clock,
    )
    original = service.create_claim(
        vault_id=vault_id,
        proposal=proposal(anchor(first_fragment)),
    )
    clock.value = T1

    with pytest.raises(RuntimeError, match="Source failure"):
        service.record_verdict(
            vault_id=vault_id,
            memory_id=original.memory_id,
            verdict=VerdictType.CORRECT,
            replacement=CorrectionReplacement(
                statement="Corrected text",
                mode=CorrectionMode.INTERPRETATION_ERROR,
            ),
            expected_etag=original.etag,
        )

    after = service.get_detail(vault_id=vault_id, memory_id=original.memory_id)
    source_fragment = Base.metadata.tables["source_fragment"]
    assert after.etag == original.etag
    assert len(after.history) == 1
    assert after.verdicts == ()
    assert session.scalar(select(func.count()).select_from(source_fragment)) == 1


def test_removing_last_support_re_evaluates_unconfirmed_active_claim(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    service = MemoryService(session, clock=lambda: T0)
    detail = service.create_claim(vault_id=vault_id, proposal=proposal(anchor(fragment_id)))

    reevaluated = service.remove_evidence(
        vault_id=vault_id,
        evidence_id=detail.evidence[0].id,
        expected_etag=detail.etag,
    )

    assert reevaluated.version.state is LifecycleState.DISPUTED
    assert reevaluated.evidence == ()
    assert reevaluated.verdicts == ()
    assert reevaluated.etag != detail.etag


def test_removing_last_support_disputes_even_a_confirmed_claim(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    service = MemoryService(session, clock=lambda: T0)
    detail = service.create_claim(vault_id=vault_id, proposal=proposal(anchor(fragment_id)))
    confirmed = service.record_verdict(
        vault_id=vault_id,
        memory_id=detail.memory_id,
        verdict=VerdictType.CONFIRM,
        expected_etag=detail.etag,
    )
    confirmed_detail = service.get_detail(vault_id=vault_id, memory_id=detail.memory_id)

    reevaluated = service.remove_evidence(
        vault_id=vault_id,
        evidence_id=confirmed_detail.evidence[0].id,
        expected_etag=confirmed.etag,
    )

    assert reevaluated.version.state is LifecycleState.DISPUTED
    assert reevaluated.allowed_uses == ("review_only",)


def test_support_and_counterevidence_remain_distinct_after_deletion(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    support_fragment = add_fragment(vault_id=vault_id)
    counter_fragment = add_fragment(vault_id=vault_id)
    service = MemoryService(session, clock=lambda: T0)
    detail = service.create_claim(
        vault_id=vault_id,
        proposal=proposal(
            anchor(support_fragment),
            anchor(counter_fragment, relation=EvidenceRelation.CONTRADICTS),
        ),
    )

    assert detail.version.state is LifecycleState.DISPUTED
    assert len(detail.evidence) == 1
    assert len(detail.counterevidence) == 1

    after = service.remove_evidence(
        vault_id=vault_id,
        evidence_id=detail.evidence[0].id,
        expected_etag=detail.etag,
    )

    assert after.version.state is LifecycleState.DISPUTED
    assert after.evidence == ()
    assert len(after.counterevidence) == 1


def test_real_and_system_intervals_are_half_open_and_both_required(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    valid_to = REAL_FROM + timedelta(days=30)
    service = MemoryService(session, clock=lambda: T0)
    detail = service.create_claim(
        vault_id=vault_id,
        proposal=proposal(anchor(fragment_id), valid_to=valid_to),
    )

    at_start = service.get_as_of_version(
        vault_id=vault_id,
        memory_id=detail.memory_id,
        real_at=REAL_FROM,
        system_at=T0,
    )
    assert at_start.statement == detail.version.statement
    with pytest.raises(MemoryNotFoundError):
        service.get_as_of_version(
            vault_id=vault_id,
            memory_id=detail.memory_id,
            real_at=valid_to,
            system_at=T0,
        )
    with pytest.raises(InvalidTemporalIntervalError):
        service.get_detail(
            vault_id=vault_id,
            memory_id=detail.memory_id,
            real_at=REAL_FROM,
        )


def test_historical_lifecycle_replays_evidence_and_verdict_events(
    session: Session, add_fragment
) -> None:
    vault_id = uuid.uuid4()
    first_fragment = add_fragment(vault_id=vault_id, recorded_at=SOURCE_T0)
    second_fragment = add_fragment(vault_id=vault_id, recorded_at=SOURCE_T1)
    third_fragment = add_fragment(vault_id=vault_id, recorded_at=SOURCE_T2)
    clock = MutableClock(T0)
    service = MemoryService(session, clock=clock)
    detail = service.create_claim(
        vault_id=vault_id,
        proposal=proposal(
            anchor(first_fragment),
            anchor(second_fragment),
            kind=MemoryClaimKind.PATTERN_HYPOTHESIS,
            epistemic_type=EpistemicType.INFERRED,
            attribution=Attribution.MODEL_HYPOTHESIS,
        ),
    )

    clock.value = T1
    confirmed = service.record_verdict(
        vault_id=vault_id,
        memory_id=detail.memory_id,
        verdict=VerdictType.CONFIRM,
        expected_etag=detail.etag,
    )
    at_confirm = service.get_detail(vault_id=vault_id, memory_id=detail.memory_id)
    second_system_event = T1 + timedelta(days=1)
    clock.value = second_system_event
    disputed = service.remove_evidence(
        vault_id=vault_id,
        evidence_id=at_confirm.evidence[0].id,
        expected_etag=confirmed.etag,
    )
    third_system_event = T1 + timedelta(days=2)
    clock.value = third_system_event
    active_again = service.add_evidence(
        vault_id=vault_id,
        target_derived_object_id=detail.version.derived_object_id,
        anchor=anchor(third_fragment),
        expected_etag=disputed.etag,
    )

    verdict_event = session.get(UserVerdict, confirmed.verdict_id)
    removed_link = session.get(EvidenceLink, at_confirm.evidence[0].id)
    restored_link = session.scalar(
        select(EvidenceLink).where(EvidenceLink.source_fragment_id == third_fragment)
    )
    assert verdict_event is not None
    assert removed_link is not None and removed_link.deleted_at is not None
    assert restored_link is not None
    event_times = tuple(
        value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        for value in (
            detail.version.system_from,
            verdict_event.created_at,
            removed_link.deleted_at,
            restored_link.created_at,
        )
    )

    states = [
        service.get_as_of_version(
            vault_id=vault_id,
            memory_id=detail.memory_id,
            real_at=REAL_FROM,
            system_at=system_time,
        ).state
        for system_time in event_times
    ]

    assert states == [
        LifecycleState.CANDIDATE,
        LifecycleState.ACTIVE,
        LifecycleState.DISPUTED,
        LifecycleState.ACTIVE,
    ]
    assert active_again.version.state is LifecycleState.ACTIVE


def test_correct_requires_nonblank_text_before_any_append(session: Session, add_fragment) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    service = MemoryService(session, clock=lambda: T0)
    detail = service.create_claim(vault_id=vault_id, proposal=proposal(anchor(fragment_id)))

    with pytest.raises(InvalidVerdictError):
        service.record_verdict(
            vault_id=vault_id,
            memory_id=detail.memory_id,
            verdict=VerdictType.CORRECT,
            replacement=CorrectionReplacement(
                statement="   ",
                mode=CorrectionMode.INTERPRETATION_ERROR,
            ),
            expected_etag=detail.etag,
        )

    assert session.scalar(select(func.count()).select_from(UserVerdict)) == 0
    assert (
        session.scalar(
            select(func.count()).select_from(EvidenceLink).where(EvidenceLink.deleted_at.is_(None))
        )
        == 1
    )
