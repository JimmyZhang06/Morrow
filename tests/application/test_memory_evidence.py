"""Live Source/Consent authorization tests for Memory evidence excerpts."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from life_coach.application.memory_evidence import MemoryEvidenceExcerptResolver
from life_coach.application.model_gateway import (
    KnowledgeAuthorizationSnapshotAdapter,
    KnowledgeEvidenceAuthorityAdapter,
    SourceConsentAuthority,
)
from life_coach.application.source_entries import (
    LocalAesGcmSourceContentProtector,
    ProtectedCorrectionSourceRecorder,
    ProtectedSourceFragmentPlaintextReader,
)
from life_coach.modules.consent import (
    ConsentAction,
    ConsentPurpose,
    UserConsentCommand,
    grant_consent,
    revoke_consent,
)
from life_coach.modules.identity.service import create_vault
from life_coach.modules.knowledge.contracts import ClaimProposal, EvidenceAnchor
from life_coach.modules.knowledge.enums import (
    Attribution,
    ConfidenceBand,
    DataClass,
    EpistemicType,
    EvidenceExtractionReason,
    EvidenceRelation,
    EvidenceStrength,
    MemoryClaimKind,
    ValidTimePrecision,
)
from life_coach.modules.knowledge.exceptions import (
    EvidenceNotFoundError,
    EvidenceSourceUnavailableError,
)
from life_coach.modules.knowledge.models import EvidenceLink
from life_coach.modules.knowledge.service import MemoryService
from life_coach.modules.sources.models import SourceFragment, SourceRevision
from life_coach.modules.sources.service import tombstone_source_document
from life_coach.platform.model_registry import load_model_registry
from life_coach.shared.database import Base

_CONTENT_KEY = b"memory-evidence-source-key-material-32-bytes"


@dataclass(slots=True)
class _Fixture:
    session: Session
    vault_id: uuid.UUID
    principal_id: uuid.UUID
    memory_id: uuid.UUID
    evidence_id: uuid.UUID
    source_document_id: uuid.UUID
    resolver: MemoryEvidenceExcerptResolver
    excerpt: str


def _consent(
    session: Session,
    *,
    vault_id: uuid.UUID,
    principal_id: uuid.UUID,
    purpose: ConsentPurpose,
    action: ConsentAction,
) -> None:
    now = datetime.now(UTC)
    writer = grant_consent if action is ConsentAction.GRANT else revoke_consent
    writer(
        session,
        command=UserConsentCommand(
            vault_id=vault_id,
            principal_id=principal_id,
            purpose=purpose,
            action=action,
            interaction_id=uuid.uuid4(),
            issued_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(minutes=4),
        ),
    )


@pytest.fixture
def authorized_evidence() -> Iterator[_Fixture]:
    load_model_registry()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        vault = create_vault(session)
        principal_id = uuid.uuid4()
        _consent(
            session,
            vault_id=vault.id,
            principal_id=principal_id,
            purpose=ConsentPurpose.LONG_TERM_INFERENCE,
            action=ConsentAction.GRANT,
        )
        _consent(
            session,
            vault_id=vault.id,
            principal_id=principal_id,
            purpose=ConsentPurpose.PASSIVE_QA,
            action=ConsentAction.GRANT,
        )
        protector = LocalAesGcmSourceContentProtector(_CONTENT_KEY)
        recorder = ProtectedCorrectionSourceRecorder(protector)
        source_text = "I feel calmer when I draw after work."
        excerpt = "calmer when I draw"
        source = recorder.record_correction(
            session=session,
            vault_id=vault.id,
            memory_id=uuid.uuid4(),
            correction_text=source_text,
            data_class=DataClass.SENSITIVE,
            recorded_at=datetime.now(UTC),
        )
        fragment_row = session.get(SourceFragment, source.source_fragment_id)
        assert fragment_row is not None
        revision_row = session.get(SourceRevision, fragment_row.revision_id)
        assert revision_row is not None
        # SQLite drops timezone information; production PostgreSQL preserves it.
        set_committed_value(
            revision_row,
            "created_at",
            revision_row.created_at.replace(tzinfo=UTC),
        )
        reader = ProtectedSourceFragmentPlaintextReader(protector)
        authority = SourceConsentAuthority(reader)
        evidence_authority = KnowledgeEvidenceAuthorityAdapter(authority)
        memory = MemoryService(
            session,
            evidence_source_verifier=evidence_authority,
            authorization_verifier=KnowledgeAuthorizationSnapshotAdapter(),
        )
        start = source_text.index(excerpt)
        created = memory.create_claim(
            vault_id=vault.id,
            proposal=ClaimProposal(
                kind=MemoryClaimKind.EXPLICIT_FACT,
                canonical_text="Drawing after work may help me feel calmer.",
                structured_payload={},
                epistemic_type=EpistemicType.STATED,
                attribution=Attribution.SELF_REPORT,
                uncertainty_text=None,
                valid_from=datetime.now(UTC),
                valid_to=None,
                valid_time_precision=ValidTimePrecision.DAY,
                valid_time_original="today",
                valid_timezone="UTC",
                confidence_band=ConfidenceBand.HIGH,
                pipeline_version="evidence-api-v1",
                data_class=DataClass.SENSITIVE,
                evidence=(
                    EvidenceAnchor(
                        source_fragment_id=source.source_fragment_id,
                        relation=EvidenceRelation.SUPPORTS,
                        quote_hash=hashlib.sha256(excerpt.encode()).hexdigest(),
                        extractor_reason=EvidenceExtractionReason.EXPLICIT_STATEMENT,
                        quote_start=start,
                        quote_end=start + len(excerpt),
                        strength_band=EvidenceStrength.STRONG,
                    ),
                ),
            ),
        )
        evidence = session.scalar(select(EvidenceLink).where(EvidenceLink.vault_id == vault.id))
        assert evidence is not None
        fixture = _Fixture(
            session=session,
            vault_id=vault.id,
            principal_id=principal_id,
            memory_id=created.memory_id,
            evidence_id=evidence.id,
            source_document_id=evidence.source_document_id,
            resolver=MemoryEvidenceExcerptResolver(authority),
            excerpt=excerpt,
        )
        yield fixture
        session.rollback()
    engine.dispose()


def test_resolver_returns_only_the_authoritative_quote(authorized_evidence: _Fixture) -> None:
    resolved = authorized_evidence.resolver.resolve(
        authorized_evidence.session,
        vault_id=authorized_evidence.vault_id,
        memory_id=authorized_evidence.memory_id,
        evidence_id=authorized_evidence.evidence_id,
    )

    assert resolved.excerpt == authorized_evidence.excerpt
    assert authorized_evidence.excerpt not in repr(resolved)


def test_cross_vault_excerpt_is_indistinguishable_from_absent(
    authorized_evidence: _Fixture,
) -> None:
    other_vault = create_vault(authorized_evidence.session)

    with pytest.raises(EvidenceNotFoundError):
        authorized_evidence.resolver.resolve(
            authorized_evidence.session,
            vault_id=other_vault.id,
            memory_id=authorized_evidence.memory_id,
            evidence_id=authorized_evidence.evidence_id,
        )


@pytest.mark.parametrize("invalidated_by", ["consent", "deletion"])
def test_revoked_or_deleted_source_never_returns_excerpt(
    authorized_evidence: _Fixture,
    invalidated_by: str,
) -> None:
    if invalidated_by == "consent":
        _consent(
            authorized_evidence.session,
            vault_id=authorized_evidence.vault_id,
            principal_id=authorized_evidence.principal_id,
            purpose=ConsentPurpose.PASSIVE_QA,
            action=ConsentAction.REVOKE,
        )
    else:
        tombstone_source_document(
            authorized_evidence.session,
            vault_id=authorized_evidence.vault_id,
            document_id=authorized_evidence.source_document_id,
        )

    with pytest.raises(EvidenceSourceUnavailableError):
        authorized_evidence.resolver.resolve(
            authorized_evidence.session,
            vault_id=authorized_evidence.vault_id,
            memory_id=authorized_evidence.memory_id,
            evidence_id=authorized_evidence.evidence_id,
        )


def test_hash_mismatch_never_returns_excerpt(authorized_evidence: _Fixture) -> None:
    evidence = authorized_evidence.session.get(EvidenceLink, authorized_evidence.evidence_id)
    assert evidence is not None
    evidence.quote_hash = "0" * 64
    authorized_evidence.session.flush()

    with pytest.raises(EvidenceSourceUnavailableError):
        authorized_evidence.resolver.resolve(
            authorized_evidence.session,
            vault_id=authorized_evidence.vault_id,
            memory_id=authorized_evidence.memory_id,
            evidence_id=authorized_evidence.evidence_id,
        )
