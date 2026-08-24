"""Authoritative Source/Consent model gateway integration tests."""

from __future__ import annotations

import ast
import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from life_coach.ai.contracts import RetentionPolicy, SensitivityLevel
from life_coach.ai.fakes import DeterministicFakeProvider
from life_coach.ai.provider import ModelGateway
from life_coach.application.model_gateway import (
    GovernedModelGateway,
    KnowledgeAuthorizationSnapshotAdapter,
    KnowledgeEvidenceAuthorityAdapter,
    ModelInvocationDenied,
    ModelTaskDefinition,
    SourceAuthorityUnavailable,
    SourceConsentAuthority,
    SourceIntegrityViolation,
)
from life_coach.modules.consent.models import ConsentAction, ConsentPurpose
from life_coach.modules.consent.provider_policy import ProviderPolicy
from life_coach.modules.consent.service import UserConsentCommand, grant_consent
from life_coach.modules.identity.models import CreatedBy, DataClass
from life_coach.modules.identity.service import create_vault
from life_coach.modules.knowledge.contracts import EvidenceAnchor, EvidenceSourceReference
from life_coach.modules.knowledge.enums import (
    AuthorizationPurpose,
    EvidenceExtractionReason,
    EvidenceRelation,
    EvidenceStrength,
    SourceEvidenceStatus,
)
from life_coach.modules.knowledge.enums import DataClass as KnowledgeDataClass
from life_coach.modules.sources.models import FragmentKind, ProcessingState, SourceType
from life_coach.modules.sources.service import create_source_document, create_source_fragment
from life_coach.platform.model_registry import load_model_registry
from life_coach.shared.database import Base


class EchoOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class _PlaintextReader:
    def __init__(self, values: dict[uuid.UUID, str]) -> None:
        self.values = values

    def read_text(
        self,
        *,
        vault_id: uuid.UUID,
        document_id: uuid.UUID,
        revision_id: uuid.UUID,
        revision_no: int,
        fragment_id: uuid.UUID,
        ciphertext: bytes,
    ) -> str:
        del vault_id, document_id, revision_id, revision_no, ciphertext
        return self.values[fragment_id]


@pytest.fixture
def session() -> Iterator[Session]:
    load_model_registry()
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as database_session:
        yield database_session
        database_session.rollback()
    engine.dispose()


def _record_source(session: Session, text: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    vault = create_vault(session, data_class=DataClass.NORMAL)
    result = create_source_document(
        session,
        vault_id=vault.id,
        content_ciphertext=b"encrypted-document",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        content_mime="text/plain",
        source_type=SourceType.NOTE,
        processing_state=ProcessingState.READY,
        created_by=CreatedBy.USER,
        data_class=DataClass.NORMAL,
    )
    fragment = create_source_fragment(
        session,
        vault_id=vault.id,
        revision_id=result.revision.id,
        ordinal=0,
        text_ciphertext=b"encrypted-fragment",
        text_hash=hashlib.sha256(text.encode()).hexdigest(),
        fragment_kind=FragmentKind.PARAGRAPH,
        data_class=DataClass.NORMAL,
    )
    return vault.id, result.document.id, fragment.id


def _grant(
    session: Session,
    *,
    vault_id: uuid.UUID,
    purpose: ConsentPurpose = ConsentPurpose.LONG_TERM_INFERENCE,
) -> None:
    now = datetime.now(UTC)
    grant_consent(
        session,
        command=UserConsentCommand(
            vault_id=vault_id,
            principal_id=uuid.uuid4(),
            purpose=purpose,
            action=ConsentAction.GRANT,
            interaction_id=uuid.uuid4(),
            issued_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(minutes=3),
            provider_policy=ProviderPolicy(
                allowed_providers=("zero-retention-provider",),
                processing_regions=("eu",),
                zero_retention_required=True,
                training_use_allowed=False,
                max_retention_days=0,
                policy_version="v1",
            ),
        ),
    )


def _task() -> ModelTaskDefinition:
    return ModelTaskDefinition(
        task_type="claim_extraction",
        consent_purpose=ConsentPurpose.LONG_TERM_INFERENCE,
        provider="zero-retention-provider",
        model="structured-model",
        model_revision="revision-1",
        prompt_template_version="claim-v1",
        schema_version="1",
        pipeline_version="pipeline-v1",
        required_capabilities=frozenset({"structured_output"}),
        data_residency="eu",
        retention_policy=RetentionPolicy.ZERO_RETENTION,
        provider_retention_days=0,
        provider_training_use_enabled=False,
        max_sensitivity=SensitivityLevel.SENSITIVE,
        output_type=EchoOutput,
        latency_budget_ms=1_000,
        cost_budget=Decimal("0.01"),
    )


def _invoke_prepared(
    governed: GovernedModelGateway,
    *,
    session: Session,
    vault_id: uuid.UUID,
    fragment_id: uuid.UUID,
) -> BaseModel:
    prepared = governed.prepare(
        session=session,
        vault_id=vault_id,
        task_type="claim_extraction",
        fragment_ids=(fragment_id,),
    )
    governed.assert_current(session=session, prepared=prepared)
    return governed.invoke(prepared=prepared, run_id=uuid.uuid4())


def test_governed_gateway_mints_provider_policy_fences_and_refs_from_authority(
    session: Session,
) -> None:
    text = "I want to make more time for drawing."
    vault_id, _document_id, fragment_id = _record_source(session, text)
    _grant(session, vault_id=vault_id)
    provider = DeterministicFakeProvider(
        [{"value": "candidate"}],
        provider_id="zero-retention-provider",
        data_residencies={"eu"},
        retention_policies={RetentionPolicy.ZERO_RETENTION},
    )
    governed = GovernedModelGateway(
        gateway=ModelGateway([provider]),
        source_authority=SourceConsentAuthority(_PlaintextReader({fragment_id: text})),
        tasks=(_task(),),
    )

    result = _invoke_prepared(
        governed,
        session=session,
        vault_id=vault_id,
        fragment_id=fragment_id,
    )

    assert result == EchoOutput(value="candidate")
    call = provider.calls[0]
    assert call.run_spec.vault_id == str(vault_id)
    assert call.run_spec.provider == "zero-retention-provider"
    assert call.run_spec.data_residency == "eu"
    assert call.run_spec.retention_policy is RetentionPolicy.ZERO_RETENTION
    assert call.run_spec.consent_snapshot_id.startswith("consent:")
    assert call.run_spec.policy_epoch > 0
    assert call.run_spec.source_generation > 0
    assert call.run_spec.input_refs[0].object_id == str(fragment_id)
    assert call.source_refs == (str(fragment_id),)


def test_missing_consent_never_reaches_provider(session: Session) -> None:
    text = "private fragment"
    vault_id, _document_id, fragment_id = _record_source(session, text)
    provider = DeterministicFakeProvider(
        [{"value": "must remain queued"}],
        provider_id="zero-retention-provider",
        data_residencies={"eu"},
    )
    authority = SourceConsentAuthority(_PlaintextReader({fragment_id: text}))
    governed = GovernedModelGateway(
        gateway=ModelGateway([provider]),
        source_authority=authority,
        tasks=(_task(),),
    )

    with pytest.raises(SourceAuthorityUnavailable):
        _invoke_prepared(
            governed,
            session=session,
            vault_id=vault_id,
            fragment_id=fragment_id,
        )
    assert provider.call_count == 0


def test_bad_plaintext_integrity_never_reaches_provider(session: Session) -> None:
    text = "private fragment"
    vault_id, _document_id, fragment_id = _record_source(session, text)
    _grant(session, vault_id=vault_id)
    provider = DeterministicFakeProvider(
        [{"value": "must remain queued"}],
        provider_id="zero-retention-provider",
        data_residencies={"eu"},
    )
    governed = GovernedModelGateway(
        gateway=ModelGateway([provider]),
        source_authority=SourceConsentAuthority(_PlaintextReader({fragment_id: "tampered"})),
        tasks=(_task(),),
    )

    with pytest.raises(SourceIntegrityViolation):
        _invoke_prepared(
            governed,
            session=session,
            vault_id=vault_id,
            fragment_id=fragment_id,
        )
    assert provider.call_count == 0


def test_consent_data_handling_cannot_be_loosened_by_task_configuration(
    session: Session,
) -> None:
    text = "provider policy boundary"
    vault_id, _document_id, fragment_id = _record_source(session, text)
    _grant(session, vault_id=vault_id)
    provider = DeterministicFakeProvider(
        [{"value": "must remain queued"}],
        provider_id="zero-retention-provider",
        data_residencies={"eu"},
    )
    governed = GovernedModelGateway(
        gateway=ModelGateway([provider]),
        source_authority=SourceConsentAuthority(_PlaintextReader({fragment_id: text})),
        tasks=(replace(_task(), provider_training_use_enabled=True),),
    )

    with pytest.raises(ModelInvocationDenied, match="training"):
        _invoke_prepared(
            governed,
            session=session,
            vault_id=vault_id,
            fragment_id=fragment_id,
        )

    assert provider.call_count == 0


def test_knowledge_evidence_adapter_returns_only_source_verified_span(session: Session) -> None:
    text = "I feel calmer when I draw after work."
    quote = "calmer when I draw"
    start = text.index(quote)
    end = start + len(quote)
    vault_id, _document_id, fragment_id = _record_source(session, text)
    _grant(session, vault_id=vault_id)
    authority = SourceConsentAuthority(_PlaintextReader({fragment_id: text}))
    adapter = KnowledgeEvidenceAuthorityAdapter(authority)
    anchor = EvidenceAnchor(
        source_fragment_id=fragment_id,
        relation=EvidenceRelation.SUPPORTS,
        quote_hash=hashlib.sha256(quote.encode()).hexdigest(),
        extractor_reason=EvidenceExtractionReason.EXPLICIT_STATEMENT,
        quote_start=start,
        quote_end=end,
        strength_band=EvidenceStrength.MODERATE,
    )

    verified = adapter.verify(
        session=session,
        vault_id=vault_id,
        anchor=anchor,
        purpose=AuthorizationPurpose.MEMORY_CREATE,
        at=datetime.now(UTC),
    )

    assert verified.source_fragment_id == fragment_id
    assert verified.quote_hash == anchor.quote_hash
    assert verified.policy_epoch > 0
    assert verified.source_generation > 0
    assert verified.authorization_snapshot_id

    tampered = EvidenceAnchor(
        source_fragment_id=fragment_id,
        relation=anchor.relation,
        quote_hash="0" * 64,
        extractor_reason=anchor.extractor_reason,
        quote_start=start,
        quote_end=end,
        strength_band=anchor.strength_band,
    )
    with pytest.raises(SourceIntegrityViolation):
        adapter.verify(
            session=session,
            vault_id=vault_id,
            anchor=tampered,
            purpose=AuthorizationPurpose.MEMORY_CREATE,
            at=datetime.now(UTC),
        )


def test_evidence_review_preserves_creation_receipt_across_consent_purposes(
    session: Session,
) -> None:
    text = "I want ten quiet minutes to review one decision."
    vault_id, _document_id, fragment_id = _record_source(session, text)
    _grant(session, vault_id=vault_id, purpose=ConsentPurpose.LONG_TERM_INFERENCE)
    _grant(session, vault_id=vault_id, purpose=ConsentPurpose.PASSIVE_QA)
    adapter = KnowledgeEvidenceAuthorityAdapter(
        SourceConsentAuthority(_PlaintextReader({fragment_id: text}))
    )
    verified = adapter.verify(
        session=session,
        vault_id=vault_id,
        anchor=EvidenceAnchor(
            source_fragment_id=fragment_id,
            relation=EvidenceRelation.SUPPORTS,
            quote_hash=hashlib.sha256(text.encode()).hexdigest(),
            extractor_reason=EvidenceExtractionReason.CONTEXT,
            quote_start=0,
            quote_end=len(text),
            strength_band=EvidenceStrength.WEAK,
        ),
        purpose=AuthorizationPurpose.MEMORY_CREATE,
        at=datetime.now(UTC),
    )
    evidence_id = uuid.uuid4()
    states = adapter.resolve_current(
        session=session,
        vault_id=vault_id,
        references=(
            EvidenceSourceReference(
                evidence_id=evidence_id,
                source_document_id=verified.source_document_id,
                source_revision_id=verified.source_revision_id,
                source_fragment_id=verified.source_fragment_id,
                quote_start=verified.quote_start,
                quote_end=verified.quote_end,
                quote_hash=verified.quote_hash,
                authorization_snapshot_id=verified.authorization_snapshot_id,
                policy_epoch=verified.policy_epoch,
                source_generation=verified.source_generation,
            ),
        ),
        purpose=AuthorizationPurpose.MEMORY_REVIEW,
        at=datetime.now(UTC),
    )

    assert states[0].evidence_id == evidence_id
    assert states[0].status is SourceEvidenceStatus.LIVE
    assert states[0].authorization_snapshot_id == verified.authorization_snapshot_id


def test_knowledge_authorization_snapshot_comes_from_current_vault_consent(
    session: Session,
) -> None:
    text = "A source-backed preference"
    vault_id, _document_id, fragment_id = _record_source(session, text)
    _grant(session, vault_id=vault_id)
    source_snapshot = SourceConsentAuthority(_PlaintextReader({fragment_id: text})).prepare(
        session=session,
        vault_id=vault_id,
        fragment_ids=(fragment_id,),
        purpose=ConsentPurpose.LONG_TERM_INFERENCE,
    )
    adapter = KnowledgeAuthorizationSnapshotAdapter()

    snapshot = adapter.authorize(
        session=session,
        vault_id=vault_id,
        purpose=AuthorizationPurpose.MEMORY_CREATE,
        data_class=KnowledgeDataClass(source_snapshot.fragments[0].data_class.value),
        policy_epoch=source_snapshot.vault.policy_epoch,
        source_generation=source_snapshot.vault.source_generation,
        at=datetime.now(UTC),
    )

    assert snapshot.vault_id == vault_id
    assert snapshot.policy_epoch == source_snapshot.vault.policy_epoch
    assert snapshot.source_generation == source_snapshot.vault.source_generation
    assert snapshot.allows_read is True
    assert snapshot.allows_proactive is False

    with pytest.raises(SourceAuthorityUnavailable, match="stale"):
        adapter.authorize(
            session=session,
            vault_id=vault_id,
            purpose=AuthorizationPurpose.MEMORY_CREATE,
            data_class=snapshot.data_class,
            policy_epoch=snapshot.policy_epoch,
            source_generation=snapshot.source_generation + 1,
            at=datetime.now(UTC),
        )


def test_only_provider_kernel_may_call_provider_complete() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src" / "life_coach"
    allowed = Path("ai/provider.py")
    violations: list[str] = []
    for path in source_root.rglob("*.py"):
        relative = path.relative_to(source_root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "complete"
                and relative != allowed
            ):
                violations.append(f"{relative.as_posix()}:{node.lineno}")

    assert violations == []
