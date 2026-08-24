from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex, CreateTable

from life_coach.modules.knowledge.contracts import ClaimProposal
from life_coach.modules.knowledge.enums import (
    Attribution,
    ClaimVersionOrigin,
    ConfidenceBand,
    DataClass,
    DerivedObjectKind,
    EpistemicType,
    EvidenceExtractionReason,
    EvidenceRelation,
    EvidenceStrength,
    LifecycleState,
    MemoryClaimKind,
    TechnicalActor,
    ValidTimePrecision,
    VerdictType,
)
from life_coach.modules.knowledge.exceptions import AppendOnlyViolationError
from life_coach.modules.knowledge.models import (
    ClaimVersion,
    DerivedObject,
    EvidenceLink,
    UserVerdict,
)
from tests.knowledge.fakes import evidence_anchor
from tests.knowledge.fakes import make_memory_service as MemoryService

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


def proposal(fragment_id: uuid.UUID) -> ClaimProposal:
    return ClaimProposal(
        kind=MemoryClaimKind.EXPLICIT_FACT,
        canonical_text="I usually write in Chinese.",
        epistemic_type=EpistemicType.STATED,
        attribution=Attribution.SELF_REPORT,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_time_precision=ValidTimePrecision.DAY,
        confidence_band=ConfidenceBand.HIGH,
        evidence=(evidence_anchor(fragment_id),),
    )


def test_evidence_uses_vault_scoped_composite_foreign_keys() -> None:
    constraints = {
        (
            tuple(element.parent.name for element in constraint.elements),
            constraint.referred_table.name,
            tuple(element.column.name for element in constraint.elements),
        )
        for constraint in EvidenceLink.__table__.foreign_key_constraints
    }

    assert (
        ("vault_id", "target_derived_object_id"),
        "derived_object",
        ("vault_id", "id"),
    ) in constraints
    assert (
        ("vault_id", "source_fragment_id"),
        "source_fragment",
        ("vault_id", "id"),
    ) in constraints
    assert (
        ("vault_id", "model_run_id"),
        "model_run",
        ("vault_id", "id"),
    ) in constraints
    assert "target_type" not in EvidenceLink.__table__.columns
    assert "target_id" not in EvidenceLink.__table__.columns


def test_claim_version_uses_vault_scoped_model_run_fk_and_query_index() -> None:
    constraints = {
        (
            tuple(element.parent.name for element in constraint.elements),
            constraint.referred_table.name,
            tuple(element.column.name for element in constraint.elements),
            constraint.ondelete,
        )
        for constraint in ClaimVersion.__table__.foreign_key_constraints
    }

    assert (
        ("vault_id", "model_run_id"),
        "model_run",
        ("vault_id", "id"),
        "RESTRICT",
    ) in constraints
    assert "ix_claim_version_vault_model_run" in {
        index.name for index in ClaimVersion.__table__.indexes
    }


def test_postgresql_ddl_contains_nonoverlap_and_partial_current_guards() -> None:
    table_ddl = str(CreateTable(ClaimVersion.__table__).compile(dialect=postgresql.dialect()))
    current_index = next(
        index
        for index in ClaimVersion.__table__.indexes
        if index.name == "uq_claim_version_current"
    )
    index_ddl = str(CreateIndex(current_index).compile(dialect=postgresql.dialect()))

    assert "EXCLUDE USING gist" in table_ddl
    assert "tstzrange(system_from, system_to, '[)') WITH &&" in table_ddl
    assert "structured_payload JSONB" in table_ddl
    assert "UNIQUE INDEX uq_claim_version_current" in index_ddl
    assert "WHERE system_to IS NULL" in index_ddl


def test_cross_vault_evidence_is_rejected_by_database(session: Session, add_fragment) -> None:
    target_vault = uuid.uuid4()
    wrong_vault = uuid.uuid4()
    target_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=target_vault)
    session.add(
        DerivedObject(
            id=target_id,
            vault_id=target_vault,
            object_kind=DerivedObjectKind.CLAIM_VERSION,
            created_by=TechnicalActor.KNOWLEDGE_PIPELINE,
            data_class=DataClass.NORMAL,
        )
    )
    session.flush()
    session.add(
        EvidenceLink(
            id=uuid.uuid4(),
            vault_id=wrong_vault,
            target_derived_object_id=target_id,
            source_document_id=uuid.uuid4(),
            source_revision_id=uuid.uuid4(),
            source_fragment_id=fragment_id,
            relation=EvidenceRelation.SUPPORTS,
            quote_start=0,
            quote_end=5,
            quote_hash="b" * 64,
            extractor_reason=EvidenceExtractionReason.EXPLICIT_STATEMENT,
            strength_band=EvidenceStrength.STRONG,
            source_recorded_at=NOW,
            source_content_fingerprint="d" * 64,
            normalized_fingerprint="e" * 64,
            authorization_snapshot_id=uuid.uuid4(),
            source_policy_epoch=1,
            source_generation=1,
            source_verified_at=NOW,
            source_data_class=DataClass.NORMAL,
            created_by=TechnicalActor.KNOWLEDGE_PIPELINE,
            data_class=DataClass.NORMAL,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_database_rejects_half_missing_quote_offsets(session: Session, add_fragment) -> None:
    vault_id = uuid.uuid4()
    target_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    target = DerivedObject(
        id=target_id,
        vault_id=vault_id,
        object_kind=DerivedObjectKind.CLAIM_VERSION,
        created_by=TechnicalActor.KNOWLEDGE_PIPELINE,
        data_class=DataClass.NORMAL,
    )
    session.add(target)
    session.flush([target])
    session.add(
        EvidenceLink(
            id=uuid.uuid4(),
            vault_id=vault_id,
            target_derived_object_id=target_id,
            source_document_id=uuid.uuid4(),
            source_revision_id=uuid.uuid4(),
            source_fragment_id=fragment_id,
            relation=EvidenceRelation.SUPPORTS,
            quote_start=0,
            quote_end=None,
            quote_hash="c" * 64,
            extractor_reason=EvidenceExtractionReason.EXPLICIT_STATEMENT,
            strength_band=EvidenceStrength.STRONG,
            source_recorded_at=NOW,
            source_content_fingerprint="d" * 64,
            normalized_fingerprint="e" * 64,
            authorization_snapshot_id=uuid.uuid4(),
            source_policy_epoch=1,
            source_generation=1,
            source_verified_at=NOW,
            source_data_class=DataClass.NORMAL,
            created_by=TechnicalActor.KNOWLEDGE_PIPELINE,
            data_class=DataClass.NORMAL,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_database_allows_only_one_current_version_per_claim(session: Session, add_fragment) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    service = MemoryService(session, clock=lambda: NOW)
    detail = service.create_claim(vault_id=vault_id, proposal=proposal(fragment_id))
    current = session.get(ClaimVersion, detail.version.derived_object_id)
    assert current is not None
    second_derived_id = uuid.uuid4()
    second_derived = DerivedObject(
        id=second_derived_id,
        vault_id=vault_id,
        object_kind=DerivedObjectKind.CLAIM_VERSION,
        created_by=TechnicalActor.KNOWLEDGE_PIPELINE,
        data_class=DataClass.NORMAL,
    )
    session.add(second_derived)
    session.flush([second_derived])
    session.add(
        ClaimVersion(
            derived_object_id=second_derived_id,
            vault_id=vault_id,
            claim_id=detail.memory_id,
            version_no=2,
            canonical_text="Duplicate current",
            structured_payload={},
            epistemic_type=EpistemicType.STATED,
            attribution=Attribution.SELF_REPORT,
            uncertainty_text=None,
            initial_lifecycle_state=LifecycleState.CANDIDATE,
            lifecycle_state=LifecycleState.CANDIDATE,
            valid_from=NOW,
            valid_to=None,
            valid_time_precision=ValidTimePrecision.EXACT,
            valid_time_original=None,
            valid_timezone="UTC",
            system_from=NOW,
            system_to=None,
            confidence_band=ConfidenceBand.HIGH,
            pipeline_version="test",
            model_run_id=None,
            origin=ClaimVersionOrigin.PIPELINE_DERIVED,
            normalized_fingerprint="f" * 64,
            authorization_snapshot_id=current.authorization_snapshot_id,
            authorization_policy_epoch=current.authorization_policy_epoch,
            authorization_source_generation=current.authorization_source_generation,
            safety_assessment_id=current.safety_assessment_id,
            safety_allows_proactive=current.safety_allows_proactive,
            subject_verification_id=None,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_user_verdict_rows_are_orm_append_only(session: Session, add_fragment) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    service = MemoryService(session, clock=lambda: NOW)
    detail = service.create_claim(vault_id=vault_id, proposal=proposal(fragment_id))
    outcome = service.record_verdict(
        vault_id=vault_id,
        memory_id=detail.memory_id,
        verdict=VerdictType.SNOOZE,
        expected_etag=detail.etag,
    )
    verdict = session.get(UserVerdict, outcome.verdict_id)
    assert verdict is not None
    verdict.reason_optional = "mutated"

    with pytest.raises(AppendOnlyViolationError):
        session.flush()


def test_user_verdict_rows_reject_session_bulk_update(session: Session, add_fragment) -> None:
    vault_id = uuid.uuid4()
    fragment_id = add_fragment(vault_id=vault_id)
    service = MemoryService(session, clock=lambda: NOW)
    detail = service.create_claim(vault_id=vault_id, proposal=proposal(fragment_id))
    service.record_verdict(
        vault_id=vault_id,
        memory_id=detail.memory_id,
        verdict=VerdictType.SNOOZE,
        expected_etag=detail.etag,
    )

    with pytest.raises(AppendOnlyViolationError):
        session.execute(update(UserVerdict).values(reason_optional="bulk-mutated"))


def test_model_metadata_uses_the_shared_base_registry() -> None:
    inspector = inspect(ClaimVersion)
    assert inspector.local_table.metadata is DerivedObject.metadata
