"""Construction of short-lived, citation-preserving generation context."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from .contracts import (
    CitationBinding,
    CitationObjectType,
    CitationTarget,
    ContextClaim,
    ContextCounterevidence,
    ContextPack,
    ContextPolicySnapshot,
    ContextSourceQuote,
    EvidenceItem,
    EvidenceRelation,
    EvidenceStrength,
    HybridRetrievalResult,
    RetrievalIntent,
)


class ContextPackPolicyError(ValueError):
    """The retrieval result is insufficient for the requested generation purpose."""


def validate_context_pack_for_use(
    context_pack: ContextPack,
    *,
    current_policy_snapshot: ContextPolicySnapshot,
    used_at: datetime,
) -> ContextPack:
    """Revalidate a short-lived pack against the server's current policy snapshot."""

    context_pack = ContextPack.model_validate_json(context_pack.model_dump_json(), strict=True)
    current_policy_snapshot = ContextPolicySnapshot.model_validate_json(
        current_policy_snapshot.model_dump_json(), strict=True
    )
    if used_at.tzinfo is None or used_at.utcoffset() is None or used_at.utcoffset() != timedelta(0):
        raise ContextPackPolicyError("used_at must be timezone-aware UTC")
    if current_policy_snapshot.policy_fingerprint != context_pack.policy_fingerprint:
        raise ContextPackPolicyError("ContextPack policy snapshot is no longer current")
    if not context_pack.created_at <= used_at < current_policy_snapshot.expires_at:
        raise ContextPackPolicyError("ContextPack is not valid at use time")
    return context_pack


def build_context_pack(
    retrieval: HybridRetrievalResult,
    *,
    version: str,
    policy_snapshot: ContextPolicySnapshot,
    created_at: datetime,
    confirmed_claims: Sequence[ContextClaim] = (),
    candidate_claims: Sequence[ContextClaim] = (),
    uncertainties: Sequence[str] = (),
) -> ContextPack:
    """Build the sole input shape accepted by downstream generation modules.

    Pattern reflection fails closed unless the independent counterevidence pass
    actually ran. Counterevidence remains separate from supporting quotations.
    """

    retrieval = HybridRetrievalResult.model_validate_json(retrieval.model_dump_json(), strict=True)
    policy_snapshot = ContextPolicySnapshot.model_validate_json(
        policy_snapshot.model_dump_json(), strict=True
    )
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ContextPackPolicyError("created_at must be timezone-aware")
    if created_at.utcoffset() != timedelta(0):
        raise ContextPackPolicyError("created_at must be stored in UTC")
    query = retrieval.query
    if policy_snapshot.vault_id != query.vault_id:
        raise ContextPackPolicyError("retrieval and policy snapshot must share a vault")
    if policy_snapshot.purpose is not query.purpose:
        raise ContextPackPolicyError("retrieval and policy snapshot must share a purpose")
    if policy_snapshot.consent_snapshot_id != query.consent_snapshot_id:
        raise ContextPackPolicyError("retrieval used a different consent snapshot")
    if policy_snapshot.policy_epoch != query.policy_epoch:
        raise ContextPackPolicyError("retrieval used a different policy epoch")
    if policy_snapshot.source_generation != query.source_generation:
        raise ContextPackPolicyError("retrieval used a different Source generation")
    if policy_snapshot.max_sensitivity is not query.max_sensitivity:
        raise ContextPackPolicyError("retrieval used a different sensitivity policy")
    if not policy_snapshot.issued_at <= created_at < policy_snapshot.expires_at:
        raise ContextPackPolicyError("context policy snapshot is not valid at creation time")
    if (
        retrieval.query.purpose is RetrievalIntent.PATTERN_REFLECTION
        and not retrieval.counterevidence_searched
    ):
        raise ContextPackPolicyError("pattern reflection requires a completed counterevidence pass")

    retrieval_fingerprint = retrieval.retrieval_fingerprint
    source_quotes = tuple(
        ContextSourceQuote(
            record_id=item.record.record_id,
            source_generation=item.record.source_generation,
            source_span=item.record.source_span,
            sensitivity=item.record.sensitivity,
            retrieval_fingerprint=retrieval_fingerprint,
        )
        for item in retrieval.items
    )
    counterevidence = tuple(
        ContextCounterevidence(
            record_id=item.record.record_id,
            source_generation=item.record.source_generation,
            evidence=EvidenceItem(
                relation=EvidenceRelation.CONTRADICTS,
                source_span=item.record.source_span,
                strength=item.record.evidence_strength or EvidenceStrength.MODERATE,
                reason="retrieved by the independent counterevidence pass",
            ),
            contradiction_ids=item.record.contradiction_ids,
            sensitivity=item.record.sensitivity,
            retrieval_fingerprint=retrieval_fingerprint,
        )
        for item in retrieval.counterevidence
    )
    record_by_span = {item.record.source_span: item.record.record_id for item in retrieval.items}
    citation_map: list[CitationBinding] = [
        CitationBinding(
            target=CitationTarget(
                object_type=CitationObjectType.RETRIEVAL_RECORD,
                object_id=item.record.record_id,
            ),
            retrieval_record_id=item.record.record_id,
            source_span=item.record.source_span,
            retrieval_fingerprint=retrieval_fingerprint,
        )
        for item in retrieval.items
    ]
    for context_claim in (*confirmed_claims, *candidate_claims):
        for evidence in context_claim.verification.evidence:
            record_id = record_by_span.get(evidence.source_span)
            if record_id is None:
                raise ContextPackPolicyError(
                    "Context claim evidence was not returned by this retrieval result"
                )
            citation_map.append(
                CitationBinding(
                    target=CitationTarget(
                        object_type=CitationObjectType.CANDIDATE_CLAIM,
                        object_id=context_claim.claim.candidate_id,
                    ),
                    retrieval_record_id=record_id,
                    source_span=evidence.source_span,
                    retrieval_fingerprint=retrieval_fingerprint,
                    verification_fingerprint=(context_claim.verification.evidence_fingerprint),
                )
            )
    coverage = dict(retrieval.coverage)
    coverage["source_quote_count"] = len(source_quotes)
    coverage["counterevidence_quote_count"] = len(counterevidence)

    return ContextPack(
        vault_id=retrieval.query.vault_id,
        version=version,
        purpose=retrieval.query.purpose,
        policy_snapshot=policy_snapshot,
        policy_fingerprint=policy_snapshot.policy_fingerprint,
        retrieval_fingerprint=retrieval_fingerprint,
        created_at=created_at,
        time_scope=retrieval.query.time_scope,
        confirmed_claims=tuple(confirmed_claims),
        candidate_claims=tuple(candidate_claims),
        source_quotes=source_quotes,
        counterevidence=counterevidence,
        uncertainties=tuple(uncertainties),
        excluded_count_by_reason=dict(retrieval.excluded_count_by_reason),
        coverage=coverage,
        citation_map=tuple(citation_map),
    )


__all__ = [
    "ContextPackPolicyError",
    "build_context_pack",
    "validate_context_pack_for_use",
]
