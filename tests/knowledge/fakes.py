from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from life_coach.modules.identity import Vault, create_vault
from life_coach.modules.knowledge.contracts import (
    AuthorizationSnapshot,
    EvidenceAnchor,
    EvidenceSourceReference,
    EvidenceSourceState,
    SafetyAssessment,
    VerifiedEvidenceAnchor,
    VerifiedSubjectEntity,
)
from life_coach.modules.knowledge.enums import (
    AuthorizationPurpose,
    DataClass,
    EvidenceExtractionReason,
    EvidenceRelation,
    EvidenceStrength,
    SafetyDecision,
    SourceEvidenceStatus,
)
from life_coach.modules.knowledge.exceptions import InvalidEvidenceError
from life_coach.modules.knowledge.service import MemoryService
from life_coach.modules.sources import (
    FragmentKind,
    create_source_document,
    create_source_fragment,
)
from life_coach.shared.database import utc_now

DEFAULT_SOURCE_TEXT = "Alpha source evidence for memory tests."
_SOURCE_STATE_KEY = "knowledge_authoritative_source_state"


def record_authoritative_source(
    session: Session,
    *,
    vault_id: uuid.UUID,
    body: str | None = None,
    recorded_at: datetime = datetime(2025, 1, 1, tzinfo=UTC),
    data_class: DataClass | str = DataClass.NORMAL,
    authorization_snapshot_id: uuid.UUID | None = None,
    policy_epoch: int = 1,
    source_generation: int = 1,
    tombstoned: bool = False,
    consent_allowed: bool = True,
) -> uuid.UUID:
    """Create a real Source row and attach verifier-only authority state to the Session."""

    effective_data_class = DataClass(data_class)
    if session.get(Vault, vault_id) is None:
        create_vault(session, vault_id=vault_id)
    source = create_source_document(
        session,
        vault_id=vault_id,
        content_ciphertext=b"test-encrypted-document",
        content_hash="a" * 64,
        content_mime="text/plain",
        data_class=effective_data_class.value,
    )
    fragment = create_source_fragment(
        session,
        vault_id=vault_id,
        revision_id=source.revision.id,
        ordinal=0,
        text_ciphertext=b"test-encrypted-fragment",
        text_hash="b" * 64,
        fragment_kind=FragmentKind.PARAGRAPH,
        data_class=effective_data_class.value,
    )
    states = session.info.setdefault(_SOURCE_STATE_KEY, {})
    states[(vault_id, fragment.id)] = {
        "id": fragment.id,
        "vault_id": vault_id,
        "document_id": source.document.id,
        "revision_id": source.revision.id,
        "body": (
            f"Alpha source evidence {fragment.id} for memory tests." if body is None else body
        ),
        "recorded_at": recorded_at,
        "data_class": effective_data_class.value,
        "authorization_snapshot_id": authorization_snapshot_id or uuid.uuid4(),
        "policy_epoch": policy_epoch,
        "source_generation": source_generation,
        "tombstoned": tombstoned,
        "consent_allowed": consent_allowed,
    }
    return fragment.id


def quote_hash(text: str, start: int = 0, end: int = 5) -> str:
    return hashlib.sha256(text[start:end].encode("utf-8")).hexdigest()


def source_fingerprint(text: str) -> str:
    normalized = " ".join(text.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def evidence_anchor(
    fragment_id: uuid.UUID,
    *,
    relation: EvidenceRelation = EvidenceRelation.SUPPORTS,
    text: str = DEFAULT_SOURCE_TEXT,
    start: int = 0,
    end: int = 5,
    quote_digest: str | None = None,
    reason: EvidenceExtractionReason | None = None,
) -> EvidenceAnchor:
    if reason is None:
        reason = {
            EvidenceRelation.SUPPORTS: EvidenceExtractionReason.EXPLICIT_STATEMENT,
            EvidenceRelation.CONTRADICTS: EvidenceExtractionReason.CONTRADICTION,
            EvidenceRelation.CONTEXTUALIZES: EvidenceExtractionReason.CONTEXT,
        }[relation]
    return EvidenceAnchor(
        source_fragment_id=fragment_id,
        relation=relation,
        quote_hash=quote_digest or quote_hash(text, start, end),
        extractor_reason=reason,
        quote_start=start,
        quote_end=end,
        strength_band=EvidenceStrength.STRONG,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class AuthoritativeEvidenceSourceVerifier:
    def _row(self, session: Session, vault_id: uuid.UUID, fragment_id: uuid.UUID):
        states = session.info.get(_SOURCE_STATE_KEY, {})
        return states.get((vault_id, fragment_id))

    def verify(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        anchor: EvidenceAnchor,
        purpose: AuthorizationPurpose,
        at: datetime,
    ) -> VerifiedEvidenceAnchor:
        del purpose
        row = self._row(session, vault_id, anchor.source_fragment_id)
        if row is None or row["tombstoned"] or not row["consent_allowed"]:
            raise InvalidEvidenceError("Source fragment is unavailable or not consented")
        body = str(row["body"])
        if anchor.quote_start < 0 or anchor.quote_end > len(body):
            raise InvalidEvidenceError("Evidence span is outside the Source fragment")
        authoritative_hash = quote_hash(body, anchor.quote_start, anchor.quote_end)
        if anchor.quote_hash != authoritative_hash:
            raise InvalidEvidenceError("Evidence hash does not match the Source span")
        return VerifiedEvidenceAnchor(
            vault_id=vault_id,
            source_document_id=row["document_id"],
            source_revision_id=row["revision_id"],
            source_fragment_id=row["id"],
            relation=anchor.relation,
            quote_start=anchor.quote_start,
            quote_end=anchor.quote_end,
            quote_hash=authoritative_hash,
            extractor_reason=anchor.extractor_reason,
            strength_band=anchor.strength_band,
            source_recorded_at=_aware(row["recorded_at"]),
            source_data_class=DataClass(row["data_class"]),
            source_content_fingerprint=source_fingerprint(body),
            authorization_snapshot_id=row["authorization_snapshot_id"],
            policy_epoch=row["policy_epoch"],
            source_generation=row["source_generation"],
            verified_at=at,
            created_by=anchor.created_by,
            model_run_id=anchor.model_run_id,
        )

    def resolve_current(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        references: tuple[EvidenceSourceReference, ...],
        purpose: AuthorizationPurpose,
        at: datetime,
    ) -> tuple[EvidenceSourceState, ...]:
        del purpose
        states: list[EvidenceSourceState] = []
        for reference in references:
            row = self._row(session, vault_id, reference.source_fragment_id)
            status = SourceEvidenceStatus.UNKNOWN
            if row is not None:
                if row["tombstoned"]:
                    status = SourceEvidenceStatus.TOMBSTONED
                elif not row["consent_allowed"]:
                    status = SourceEvidenceStatus.CONSENT_REVOKED
                elif (
                    row["document_id"] != reference.source_document_id
                    or row["revision_id"] != reference.source_revision_id
                    or reference.quote_end > len(str(row["body"]))
                    or quote_hash(str(row["body"]), reference.quote_start, reference.quote_end)
                    != reference.quote_hash
                ):
                    status = SourceEvidenceStatus.STALE
                else:
                    status = SourceEvidenceStatus.LIVE
                snapshot_id = row["authorization_snapshot_id"]
                policy_epoch = row["policy_epoch"]
                source_generation = row["source_generation"]
                data_class = DataClass(row["data_class"])
            else:
                snapshot_id = reference.authorization_snapshot_id
                policy_epoch = reference.policy_epoch
                source_generation = reference.source_generation
                data_class = DataClass.HIGHLY_SENSITIVE
            states.append(
                EvidenceSourceState(
                    evidence_id=reference.evidence_id,
                    status=status,
                    authorization_snapshot_id=snapshot_id,
                    policy_epoch=policy_epoch,
                    source_generation=source_generation,
                    data_class=data_class,
                    checked_at=at,
                )
            )
        return tuple(states)


class AllowingSafetyClassifier:
    def __init__(
        self,
        *,
        data_class: DataClass = DataClass.NORMAL,
        decision: SafetyDecision = SafetyDecision.ALLOW,
        allows_proactive: bool = True,
    ) -> None:
        self.data_class = data_class
        self.decision = decision
        self.allows_proactive = allows_proactive

    def classify(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        texts: tuple[str, ...],
        at: datetime,
    ) -> SafetyAssessment:
        del session, texts, at
        return SafetyAssessment(
            assessment_id=uuid.uuid5(vault_id, "test-safety-assessment"),
            vault_id=vault_id,
            decision=self.decision,
            data_class=self.data_class,
            allows_proactive=self.allows_proactive,
        )


class AllowingAuthorizationVerifier:
    def authorize(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        purpose: AuthorizationPurpose,
        data_class: DataClass,
        policy_epoch: int,
        source_generation: int,
        at: datetime,
    ) -> AuthorizationSnapshot:
        del session, at
        return AuthorizationSnapshot(
            snapshot_id=uuid.uuid5(
                vault_id,
                f"{purpose.value}:{data_class.value}:{policy_epoch}:{source_generation}",
            ),
            vault_id=vault_id,
            purpose=purpose,
            policy_epoch=policy_epoch,
            source_generation=source_generation,
            data_class=data_class,
            allows_read=True,
            allows_proactive=True,
        )


class AllowingSubjectEntityVerifier:
    def verify(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        entity_id: uuid.UUID,
        at: datetime,
    ) -> VerifiedSubjectEntity:
        del session, at
        return VerifiedSubjectEntity(
            verification_id=uuid.uuid5(vault_id, str(entity_id)),
            vault_id=vault_id,
            entity_id=entity_id,
            data_class=DataClass.SENSITIVE,
        )


def make_memory_service(
    session: Session,
    *,
    clock: Callable[[], datetime] = utc_now,
    **overrides: Any,
) -> MemoryService:
    dependencies: dict[str, Any] = {
        "evidence_source_verifier": AuthoritativeEvidenceSourceVerifier(),
        "safety_classifier": AllowingSafetyClassifier(),
        "authorization_verifier": AllowingAuthorizationVerifier(),
        "subject_entity_verifier": AllowingSubjectEntityVerifier(),
    }
    dependencies.update(overrides)
    return MemoryService(session, clock=clock, **dependencies)
