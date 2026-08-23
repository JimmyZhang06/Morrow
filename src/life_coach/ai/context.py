"""Construction of short-lived, citation-preserving generation context."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .contracts import (
    ContextClaim,
    ContextPack,
    ContextPolicySnapshot,
    EvidenceItem,
    EvidenceRelation,
    EvidenceStrength,
    HybridRetrievalResult,
    RetrievalIntent,
    SourceSpan,
)


class ContextPackPolicyError(ValueError):
    """The retrieval result is insufficient for the requested generation purpose."""


def build_context_pack(
    retrieval: HybridRetrievalResult,
    *,
    version: str,
    policy_snapshot: ContextPolicySnapshot,
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
    if (
        retrieval.query.purpose is RetrievalIntent.PATTERN_REFLECTION
        and not retrieval.counterevidence_searched
    ):
        raise ContextPackPolicyError("pattern reflection requires a completed counterevidence pass")

    source_quotes = _unique_spans(item.record.source_span for item in retrieval.items)
    counterevidence = tuple(
        EvidenceItem(
            relation=EvidenceRelation.CONTRADICTS,
            source_span=item.record.source_span,
            strength=item.record.evidence_strength or EvidenceStrength.MODERATE,
            reason="retrieved by the independent counterevidence pass",
        )
        for item in retrieval.counterevidence
    )
    citation_map: dict[str, tuple[SourceSpan, ...]] = {
        item.record.record_id: (item.record.source_span,) for item in retrieval.items
    }
    coverage = dict(retrieval.coverage)
    coverage["source_quote_count"] = len(source_quotes)
    coverage["counterevidence_quote_count"] = len(counterevidence)

    return ContextPack(
        vault_id=retrieval.query.vault_id,
        version=version,
        purpose=retrieval.query.purpose,
        policy_snapshot=policy_snapshot,
        time_scope=retrieval.query.time_scope,
        confirmed_claims=tuple(confirmed_claims),
        candidate_claims=tuple(candidate_claims),
        source_quotes=source_quotes,
        counterevidence=counterevidence,
        uncertainties=tuple(uncertainties),
        excluded_count_by_reason=dict(retrieval.excluded_count_by_reason),
        coverage=coverage,
        citation_map=citation_map,
    )


def _unique_spans(spans: Iterable[SourceSpan]) -> tuple[SourceSpan, ...]:
    # Accepting an iterable rather than materializing retrieval text twice keeps
    # this builder useful for adapters that yield source anchors lazily.
    unique: dict[tuple[str, str, str, int, int, str, str | None, str], SourceSpan] = {}
    for span in spans:
        key = (
            span.source_document_id,
            span.source_revision_id,
            span.source_fragment_id,
            span.char_start,
            span.char_end,
            span.quote,
            span.quote_hash,
            span.source_kind.value,
        )
        unique.setdefault(key, span)
    return tuple(unique.values())


__all__ = ["ContextPackPolicyError", "build_context_pack"]
