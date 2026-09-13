"""Bounded, source-scoped review context. Rejected text is never a prompt fact."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import cast

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from life_coach.ai.contracts import ModelInputKind, ModelInputRef
from life_coach.application.model_gateway import (
    SourceAuthorityUnavailable,
    SourceConsentAuthority,
)
from life_coach.application.source_entries import (
    ProtectedSourceFragmentPlaintextReader,
    SourceContentProtector,
)
from life_coach.modules.consent import ConsentPurpose
from life_coach.modules.consent.exceptions import ConsentDenied
from life_coach.modules.consent.service import require_consent
from life_coach.modules.knowledge.enums import (
    ClaimVersionOrigin,
    EvidenceRelation,
    LifecycleState,
    VerdictType,
)
from life_coach.modules.knowledge.exceptions import KnowledgeError
from life_coach.modules.knowledge.models import (
    ClaimVersion,
    DerivedObject,
    EvidenceLink,
    MemoryClaim,
    UserVerdict,
)
from life_coach.modules.knowledge.policy import activation_allowed, allowed_uses, policy_lifecycle
from life_coach.modules.knowledge.reducer import reduce_verdicts
from life_coach.modules.sources.models import (
    SourceDocument,
    SourceType,
)


@dataclass(frozen=True)
class ReviewedContext:
    items: list[dict[str, JsonValue]]
    fragment_ids: tuple[uuid.UUID, ...]
    input_refs: tuple[ModelInputRef, ...]
    fingerprint: str
    examined: int


def reviewed_context(
    session: Session,
    *,
    vault_id: uuid.UUID,
    material_ids: list[uuid.UUID],
    protector: SourceContentProtector,
) -> ReviewedContext:
    authority = SourceConsentAuthority(ProtectedSourceFragmentPlaintextReader(protector))
    candidates = list(
        session.scalars(
            select(ClaimVersion.claim_id)
            .join(
                EvidenceLink,
                (EvidenceLink.vault_id == ClaimVersion.vault_id)
                & (EvidenceLink.target_derived_object_id == ClaimVersion.derived_object_id),
            )
            .where(
                ClaimVersion.vault_id == vault_id,
                EvidenceLink.source_fragment_id.in_(material_ids),
            )
            .distinct()
            .order_by(ClaimVersion.claim_id)
            .limit(12)
        )
    )
    items: list[dict[str, JsonValue]] = []
    fragments: list[uuid.UUID] = []
    refs: list[ModelInputRef] = []
    manifests: list[object] = []
    for memory_id in candidates:
        try:
            row = session.execute(
                select(ClaimVersion, MemoryClaim, DerivedObject)
                .join(
                    MemoryClaim,
                    (MemoryClaim.vault_id == ClaimVersion.vault_id)
                    & (MemoryClaim.id == ClaimVersion.claim_id),
                )
                .join(
                    DerivedObject,
                    (DerivedObject.vault_id == ClaimVersion.vault_id)
                    & (DerivedObject.id == ClaimVersion.derived_object_id),
                )
                .where(
                    ClaimVersion.vault_id == vault_id,
                    ClaimVersion.claim_id == memory_id,
                    ClaimVersion.system_to.is_(None),
                    MemoryClaim.deleted_at.is_(None),
                    DerivedObject.deleted_at.is_(None),
                )
            ).one_or_none()
            if row is None:
                continue
            version, claim, derived = row
            if any(
                value.value == "highly_sensitive"
                for value in (claim.data_class, derived.data_class)
            ):
                continue
            events = list(
                session.scalars(
                    select(UserVerdict).where(
                        UserVerdict.vault_id == vault_id,
                        UserVerdict.target_derived_object_id == version.derived_object_id,
                    )
                )
            )
            governance = session.scalar(
                select(UserVerdict)
                .join(
                    ClaimVersion,
                    (ClaimVersion.vault_id == UserVerdict.vault_id)
                    & (ClaimVersion.derived_object_id == UserVerdict.target_derived_object_id),
                )
                .where(
                    UserVerdict.vault_id == vault_id,
                    ClaimVersion.claim_id == memory_id,
                )
                .order_by(UserVerdict.created_at.desc(), UserVerdict.id.desc())
                .limit(1)
            )
            manifests.append(
                (
                    str(memory_id),
                    str(version.derived_object_id),
                    derived.review_revision,
                    version.lifecycle_state.value,
                    str(governance.id) if governance else None,
                )
            )
            reduced = reduce_verdicts(version.initial_lifecycle_state, events, can_activate=False)
            corrected = (
                version.origin is ClaimVersionOrigin.USER_CORRECTION
                and version.origin_verdict_id is not None
            )
            review = VerdictType.CORRECT if corrected else reduced.current_verdict
            if review not in (
                VerdictType.CONFIRM,
                VerdictType.CORRECT,
            ) or version.lifecycle_state in (
                LifecycleState.RETRACTED,
                LifecycleState.SUPERSEDED,
            ):
                continue
            # Do not silently drop a deleted source/counterexample from an interpretation.
            evidence = list(
                session.scalars(
                    select(EvidenceLink).where(
                        EvidenceLink.vault_id == vault_id,
                        EvidenceLink.target_derived_object_id == version.derived_object_id,
                        EvidenceLink.deleted_at.is_(None),
                        EvidenceLink.invalidated_reason.is_(None),
                    )
                )
            )
            if not evidence or len(evidence) > 6:
                continue
            selected = {link.source_fragment_id for link in evidence}
            if len(set(fragments) | selected) > 12 or len(items) >= 6:
                continue
            grants = []
            for link in evidence:
                document = session.scalar(
                    select(SourceDocument).where(
                        SourceDocument.vault_id == vault_id,
                        SourceDocument.id == link.source_document_id,
                        SourceDocument.deleted_at.is_(None),
                    )
                )
                if document is None or (
                    link.source_fragment_id not in material_ids
                    and not (document.source_type is SourceType.CORRECTION and corrected)
                ):
                    raise SourceAuthorityUnavailable(
                        "memory requires unavailable or unselected source"
                    )
                for purpose in (ConsentPurpose.PASSIVE_QA, ConsentPurpose.CROSS_RECORD_ANALYSIS):
                    grant = require_consent(
                        session, vault_id=vault_id, purpose=purpose, source_document_id=document.id
                    )
                    grants.append(str(grant.record_id))
            # Fresh chat-specific proof: exact source identity and quote, both live purposes.
            # Historical knowledge receipts are not rewritten and their legacy fences stay intact.
            for purpose in (ConsentPurpose.PASSIVE_QA, ConsentPurpose.CROSS_RECORD_ANALYSIS):
                checked = authority.prepare(
                    session=session,
                    vault_id=vault_id,
                    fragment_ids=sorted(selected, key=str),
                    purpose=purpose,
                )
                if any(f.data_class.value == "highly_sensitive" for f in checked.fragments):
                    raise SourceAuthorityUnavailable("sensitive memory unavailable")
                by_id = {f.fragment_id: f for f in checked.fragments}
                for link in evidence:
                    source = by_id[link.source_fragment_id]
                    start, end = link.quote_start, link.quote_end
                    if (
                        source.document_id != link.source_document_id
                        or source.revision_id != link.source_revision_id
                        or start is None
                        or end is None
                        or not 0 <= start < end <= len(source.text)
                        or hashlib.sha256(source.text[start:end].encode()).hexdigest()
                        != link.quote_hash
                    ):
                        raise SourceAuthorityUnavailable("memory quote verification failed")
            can_activate = activation_allowed(
                kind=claim.kind,
                data_class=claim.data_class,
                epistemic_type=version.epistemic_type,
                attribution=version.attribution,
                evidence=evidence,
                last_decisive_verdict=reduced.last_decisive_verdict,
                requires_explicit_confirmation=version.suppressed_by_derived_object_id is not None
                and version.suppression_override_verdict_id is None,
            )
            reduced = reduce_verdicts(
                version.initial_lifecycle_state, events, can_activate=can_activate
            )
            state = policy_lifecycle(
                reduced_state=reduced.lifecycle_state,
                activation_is_allowed=can_activate,
                evidence=evidence,
            )
            uses = allowed_uses(
                state=state,
                kind=claim.kind,
                data_class=claim.data_class,
                current_verdict=reduced.current_verdict,
                governance_verdict=governance.verdict if governance else None,
                governance_applies=governance is not None
                and version.suppression_override_verdict_id != governance.id,
                is_historical=False,
                source_authority_current=any(
                    e.relation is EvidenceRelation.SUPPORTS for e in evidence
                ),
                authorization_allows_read=True,
                authorization_allows_proactive=False,
                safety_allows_proactive=False,
                suppression_pending=version.suppressed_by_derived_object_id is not None
                and version.suppression_override_verdict_id is None
                and reduced.last_decisive_verdict is not VerdictType.CONFIRM,
            )
            if "answer_when_asked" not in uses:
                continue
            manifests.append((str(memory_id), sorted(grants), sorted(str(e.id) for e in evidence)))
            current_ids = selected
            item = {
                "memory_id": str(memory_id),
                "version_id": str(version.derived_object_id),
                "version": version.version_no,
                "review": review.value,
                "state": state.value,
                "statement": version.canonical_text,
                "uncertainty": version.uncertainty_text,
                "valid_from": version.valid_from.isoformat(),
                "valid_to": version.valid_to.isoformat() if version.valid_to else None,
                "source_fragment_ids": [str(i) for i in sorted(current_ids, key=str)],
            }
            items.append(cast(dict[str, JsonValue], item))
            fragments.extend(sorted(current_ids, key=str))
            refs.append(
                ModelInputRef(
                    vault_id=str(vault_id),
                    kind=ModelInputKind.DERIVED_OBJECT,
                    object_id=str(version.derived_object_id),
                )
            )
        except (KnowledgeError, ConsentDenied, SourceAuthorityUnavailable):
            continue
    canonical = json.dumps(
        {"policy": "conversation-review-source-v1", "items": items, "reviews": manifests},
        sort_keys=True,
        ensure_ascii=False,
    )
    return ReviewedContext(
        items,
        tuple(dict.fromkeys(fragments)),
        tuple(refs),
        hashlib.sha256(canonical.encode()).hexdigest(),
        len(candidates),
    )
