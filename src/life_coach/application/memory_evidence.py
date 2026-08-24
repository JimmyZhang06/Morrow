"""Authorized, non-persistent resolution of Memory evidence excerpts."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from life_coach.application.model_gateway import (
    SourceAuthorityUnavailable,
    SourceConsentAuthority,
    SourceIntegrityViolation,
)
from life_coach.modules.consent import ConsentPurpose
from life_coach.modules.knowledge.enums import DataClass, EvidenceRelation
from life_coach.modules.knowledge.exceptions import (
    EvidenceNotFoundError,
    EvidenceSourceUnavailableError,
)
from life_coach.modules.knowledge.models import (
    ClaimVersion,
    DerivedObject,
    EvidenceLink,
    MemoryClaim,
)
from life_coach.platform.database import VaultAsyncSession


@dataclass(frozen=True, slots=True)
class MemoryEvidenceExcerpt:
    memory_id: uuid.UUID
    evidence_id: uuid.UUID
    relation: EvidenceRelation
    source_document_id: uuid.UUID
    source_revision_id: uuid.UUID
    source_fragment_id: uuid.UUID
    source_recorded_at: datetime
    source_data_class: DataClass
    excerpt: str = field(repr=False)


class MemoryEvidenceExcerptResolver:
    """Resolve one current evidence anchor through live Source and Consent authority."""

    def __init__(self, source_authority: SourceConsentAuthority) -> None:
        self._source_authority = source_authority

    def resolve(
        self,
        session: Session,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        evidence_id: uuid.UUID,
    ) -> MemoryEvidenceExcerpt:
        evidence = session.scalar(
            select(EvidenceLink)
            .join(
                ClaimVersion,
                (ClaimVersion.vault_id == EvidenceLink.vault_id)
                & (ClaimVersion.derived_object_id == EvidenceLink.target_derived_object_id),
            )
            .join(
                MemoryClaim,
                (MemoryClaim.vault_id == ClaimVersion.vault_id)
                & (MemoryClaim.id == ClaimVersion.claim_id),
            )
            .join(
                DerivedObject,
                (DerivedObject.vault_id == EvidenceLink.vault_id)
                & (DerivedObject.id == EvidenceLink.target_derived_object_id),
            )
            .where(
                EvidenceLink.vault_id == vault_id,
                EvidenceLink.id == evidence_id,
                EvidenceLink.deleted_at.is_(None),
                ClaimVersion.claim_id == memory_id,
                ClaimVersion.system_to.is_(None),
                MemoryClaim.deleted_at.is_(None),
                DerivedObject.deleted_at.is_(None),
            )
        )
        if evidence is None:
            raise EvidenceNotFoundError("Evidence is unavailable")

        try:
            snapshot = self._source_authority.prepare(
                session=session,
                vault_id=vault_id,
                fragment_ids=(evidence.source_fragment_id,),
                purpose=ConsentPurpose.PASSIVE_QA,
            )
        except (SourceAuthorityUnavailable, SourceIntegrityViolation):
            raise EvidenceSourceUnavailableError("Evidence Source is unavailable") from None
        fragment = snapshot.fragments[0]
        identity_is_current = (
            fragment.document_id == evidence.source_document_id
            and fragment.revision_id == evidence.source_revision_id
            and fragment.fragment_id == evidence.source_fragment_id
            and fragment.text_hash == evidence.source_content_fingerprint
            and snapshot.vault.policy_epoch == evidence.source_policy_epoch
            and snapshot.vault.source_generation == evidence.source_generation
        )
        if not identity_is_current:
            raise EvidenceSourceUnavailableError("Evidence Source is unavailable")
        if not 0 <= evidence.quote_start < evidence.quote_end <= len(fragment.text):
            raise EvidenceSourceUnavailableError("Evidence Source is unavailable")
        excerpt = fragment.text[evidence.quote_start : evidence.quote_end]
        excerpt_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(excerpt_hash, evidence.quote_hash):
            raise EvidenceSourceUnavailableError("Evidence Source is unavailable")
        return MemoryEvidenceExcerpt(
            memory_id=memory_id,
            evidence_id=evidence.id,
            relation=evidence.relation,
            source_document_id=fragment.document_id,
            source_revision_id=fragment.revision_id,
            source_fragment_id=fragment.fragment_id,
            source_recorded_at=fragment.recorded_at,
            source_data_class=DataClass(fragment.data_class.value),
            excerpt=excerpt,
        )


class AsyncMemoryEvidenceExcerptService:
    """Async bridge that keeps resolution inside the authorized Vault transaction."""

    def __init__(
        self,
        session: VaultAsyncSession,
        resolver: MemoryEvidenceExcerptResolver,
    ) -> None:
        self._session = session
        self._resolver = resolver

    async def get_excerpt(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        evidence_id: uuid.UUID,
    ) -> MemoryEvidenceExcerpt:
        return await self._session.run_sync(
            lambda session: self._resolver.resolve(
                session,
                vault_id=vault_id,
                memory_id=memory_id,
                evidence_id=evidence_id,
            )
        )


__all__ = [
    "AsyncMemoryEvidenceExcerptService",
    "MemoryEvidenceExcerpt",
    "MemoryEvidenceExcerptResolver",
]
