"""Exact, provider-neutral hybrid retrieval over authorized projections.

The safety order is part of this module's contract: vault, consent, deletion,
index, sensitivity, and structured filters run before any ranking. The same
checks run again immediately before values are returned. Text is scored as
untrusted data and this module has no tool-execution surface.
"""

from __future__ import annotations

import math
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, JsonValue, field_validator

from .contracts import (
    ClaimLifecycleState,
    ContractModel,
    EvidenceRelation,
    EvidenceStrength,
    HybridRetrievalResult,
    IndexPolicy,
    RankedRetrievalItem,
    RetrievalIntent,
    RetrievalQuery,
    RetrievalRecord,
    RetrievalSignal,
    SignalRank,
    SourceKind,
    TemporalRange,
    TimeScope,
)


class ExclusionReason(StrEnum):
    """Stable reasons reported for records removed before or after ranking."""

    VAULT_MISMATCH = "vault_mismatch"
    SOURCE_GENERATION_MISMATCH = "source_generation_mismatch"
    CONSENT_DENIED = "consent_denied"
    DELETED = "deleted"
    INDEX_POLICY_NONE = "index_policy_none"
    ARTIFACT_SOURCE = "artifact_source"
    SENSITIVITY_EXCEEDED = "sensitivity_exceeded"
    TIME_MISMATCH = "time_mismatch"
    ENTITY_MISMATCH = "entity_mismatch"
    TYPE_MISMATCH = "type_mismatch"
    STATE_MISMATCH = "state_mismatch"
    CANDIDATE_EXCLUDED = "candidate_excluded"
    DUPLICATE_RECORD_ID = "duplicate_record_id"
    UNLINKED_COUNTEREVIDENCE = "unlinked_counterevidence"
    FINAL_SAFETY_FILTER = "final_safety_filter"


class RetrievalProjectionConflictError(ValueError):
    """Raised when the same immutable retrieval identity has conflicting data."""


class RRFConfig(ContractModel):
    """Auditable weights for RRF and its small post-fusion quality pass."""

    k: float = Field(default=60.0, ge=0)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            RetrievalSignal.LEXICAL.value: 1.0,
            RetrievalSignal.VECTOR.value: 1.0,
            RetrievalSignal.STRUCTURED.value: 1.0,
        }
    )
    confirmed_bonus: float = Field(default=0.001, ge=0)
    strong_evidence_bonus: float = Field(default=0.001, ge=0)
    source_diversity_bonus: float = Field(default=0.0005, ge=0)
    recent_source_bonus: float = Field(default=0.0005, ge=0)

    @field_validator(
        "k",
        "confirmed_bonus",
        "strong_evidence_bonus",
        "source_diversity_bonus",
        "recent_source_bonus",
    )
    @classmethod
    def finite_non_negative(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("ranking configuration values must be finite")
        return value

    @field_validator("weights")
    @classmethod
    def valid_weights(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not key.strip() for key in value):
            raise ValueError("RRF signal names must not be blank")
        if any(not math.isfinite(weight) or weight < 0 for weight in value.values()):
            raise ValueError("RRF weights must be finite and non-negative")
        return dict(value)


class FusedResult(ContractModel):
    item_id: str = Field(min_length=1)
    score: float
    ranks: dict[str, int]

    @property
    def record_id(self) -> str:
        return self.item_id


def reciprocal_rank_fusion(
    rankings: Mapping[str | RetrievalSignal, Sequence[str]],
    *,
    k: float = 60.0,
    weights: Mapping[str | RetrievalSignal, float] | None = None,
    limit: int | None = None,
) -> tuple[FusedResult, ...]:
    """Fuse 1-based rank lists with sum(weight / (k + rank)).

    Repeated IDs within a signal contribute only their best rank. Equal scores
    are ordered by ID, so map iteration and database tie order cannot cause drift.
    """

    if not math.isfinite(k) or k < 0:
        raise ValueError("RRF k must be finite and non-negative")
    if limit is not None and limit < 0:
        raise ValueError("RRF limit must be non-negative")

    normalized_weights = {
        _signal_name(signal): weight for signal, weight in (weights or {}).items()
    }
    scores: dict[str, float] = {}
    ranks_by_id: dict[str, dict[str, int]] = {}
    for signal, ranked_ids in rankings.items():
        signal_name = _signal_name(signal)
        weight = normalized_weights.get(signal_name, 1.0)
        if not math.isfinite(weight) or weight < 0:
            raise ValueError(f"RRF weight for {signal_name!r} must be finite and non-negative")
        if weight == 0:
            continue
        seen: set[str] = set()
        for rank, item_id in enumerate(ranked_ids, start=1):
            if not item_id.strip():
                raise ValueError("ranked IDs must not be blank")
            if item_id in seen:
                continue
            seen.add(item_id)
            scores[item_id] = scores.get(item_id, 0.0) + weight / (k + rank)
            ranks_by_id.setdefault(item_id, {})[signal_name] = rank

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if limit is not None:
        ordered = ordered[:limit]
    return tuple(
        FusedResult(item_id=item_id, score=score, ranks=ranks_by_id[item_id])
        for item_id, score in ordered
    )


def tokenize_lexical(text: str) -> tuple[str, ...]:
    """Normalize Unicode and add CJK unigrams/bigrams for exact lexical recall."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    word: list[str] = []
    cjk_run: list[str] = []

    def flush_word() -> None:
        if word:
            tokens.append("".join(word))
            word.clear()

    def flush_cjk() -> None:
        if cjk_run:
            tokens.extend(cjk_run)
            tokens.extend(cjk_run[index] + cjk_run[index + 1] for index in range(len(cjk_run) - 1))
            cjk_run.clear()

    for character in normalized:
        if _is_cjk(character):
            flush_word()
            cjk_run.append(character)
        elif character.isalnum():
            flush_cjk()
            word.append(character)
        else:
            flush_word()
            flush_cjk()
    flush_word()
    flush_cjk()
    return tuple(tokens)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Return exact cosine similarity, or None for zero/mismatched vectors."""

    if not left or len(left) != len(right):
        return None
    if any(not math.isfinite(value) for value in (*left, *right)):
        return None
    left_norm = sum(value * value for value in left)
    right_norm = sum(value * value for value in right)
    if left_norm == 0 or right_norm == 0:
        return None
    dot = sum(left_value * right_value for left_value, right_value in zip(left, right, strict=True))
    return dot / math.sqrt(left_norm * right_norm)


def exact_vector_rank(
    query_embedding: Sequence[float],
    records: Sequence[RetrievalRecord],
) -> tuple[tuple[str, float], ...]:
    """Score every supplied semantic projection without approximate indexing."""

    scores: list[tuple[str, float]] = []
    for record in records:
        if record.index_policy not in {IndexPolicy.SEMANTIC, IndexPolicy.BOTH}:
            continue
        similarity = cosine_similarity(query_embedding, record.embedding)
        if similarity is not None:
            scores.append((record.record_id, similarity))
    return tuple(sorted(scores, key=lambda item: (-item[1], item[0])))


class HybridRetriever:
    """Filter-first exact lexical/vector/structured retrieval with RRF."""

    def __init__(self, config: RRFConfig | None = None) -> None:
        self._config = config or RRFConfig()

    @property
    def config(self) -> RRFConfig:
        return self._config

    def retrieve(
        self,
        query: RetrievalQuery,
        candidates: Sequence[RetrievalRecord],
    ) -> HybridRetrievalResult:
        query = RetrievalQuery.model_validate_json(query.model_dump_json(), strict=True)
        reference_now = query.as_of or datetime.now(UTC)
        excluded: Counter[str] = Counter()
        vault_candidates: list[RetrievalRecord] = []
        for record in candidates:
            if not isinstance(record, RetrievalRecord):
                raise TypeError("retrieval candidates must be RetrievalRecord values")
            if record.vault_id != query.vault_id:
                excluded[ExclusionReason.VAULT_MISMATCH.value] += 1
            else:
                vault_candidates.append(
                    RetrievalRecord.model_validate_json(record.model_dump_json(), strict=True)
                )
        prepared_candidates, duplicate_count = _prepare_candidates(vault_candidates)
        if duplicate_count:
            excluded[ExclusionReason.DUPLICATE_RECORD_ID.value] = duplicate_count
        eligible: list[RetrievalRecord] = []

        for record in prepared_candidates:
            reason = _exclusion_reason(query, record, reference_now=reference_now)
            if reason is not None:
                excluded[reason.value] += 1
                continue
            eligible.append(record)

        counter_records = [
            record for record in eligible if record.relation is EvidenceRelation.CONTRADICTS
        ]
        counter_record_ids = {record.record_id for record in counter_records}
        main_records = [record for record in eligible if record.record_id not in counter_record_ids]

        main_pass = self._rank(
            query,
            main_records,
            limit=query.limit,
            reference_now=reference_now,
        )
        items = self._final_filter(
            query,
            main_pass.items,
            excluded,
            reference_now=reference_now,
        )

        counterevidence_searched = _counterevidence_required(query)
        counter_pass = _empty_pass()
        counterevidence: tuple[RankedRetrievalItem, ...] = ()
        relevant_counter: list[RetrievalRecord] = []
        if counterevidence_searched and query.counterevidence_limit:
            returned_ids = {item.record.record_id for item in items}
            relevant_counter = [
                record
                for record in counter_records
                if bool(set(record.contradiction_ids) & returned_ids)
            ]
            unlinked_count = len(counter_records) - len(relevant_counter)
            if unlinked_count:
                excluded[ExclusionReason.UNLINKED_COUNTEREVIDENCE.value] += unlinked_count
            counter_pass = self._rank(
                query,
                relevant_counter,
                limit=query.counterevidence_limit,
                reference_now=reference_now,
                linked_counter_ids=frozenset(record.record_id for record in relevant_counter),
            )
            counterevidence = self._final_filter(
                query,
                counter_pass.items,
                excluded,
                reference_now=reference_now,
            )

        coverage: dict[str, JsonValue] = {
            "input_count": len(candidates),
            "eligible_count": len(eligible),
            "main_candidate_count": len(main_records),
            "counterevidence_candidate_count": len(counter_records),
            "linked_counterevidence_candidate_count": len(relevant_counter),
            "returned_count": len(items),
            "counterevidence_returned_count": len(counterevidence),
            "lexical_evaluated_count": main_pass.lexical_evaluated,
            "lexical_matched_count": main_pass.lexical_ranked,
            "vector_evaluated_count": main_pass.vector_evaluated,
            "vector_ranked_count": main_pass.vector_ranked,
            "vector_unusable_count": main_pass.vector_unusable,
            "structured_ranked_count": main_pass.structured_ranked,
            "counter_lexical_matched_count": counter_pass.lexical_ranked,
            "counter_vector_ranked_count": counter_pass.vector_ranked,
            "counter_structured_ranked_count": counter_pass.structured_ranked,
            "counterevidence_searched": counterevidence_searched,
            "signals_used": list(main_pass.signals_used),
            "exact_vector_search": True,
        }
        return HybridRetrievalResult(
            query=query,
            items=items,
            counterevidence=counterevidence,
            counterevidence_searched=counterevidence_searched,
            excluded_count_by_reason=dict(sorted(excluded.items())),
            coverage=coverage,
        )

    def _rank(
        self,
        query: RetrievalQuery,
        records: Sequence[RetrievalRecord],
        *,
        limit: int,
        reference_now: datetime,
        linked_counter_ids: frozenset[str] = frozenset(),
    ) -> _RankPass:
        lexical_scores, lexical_evaluated = _lexical_scores(query, records)
        vector_scores, vector_evaluated, vector_unusable = _vector_scores(query, records)
        structured_scores = _structured_scores(
            query,
            records,
            linked_counter_ids=linked_counter_ids,
        )
        raw_scores = {
            RetrievalSignal.LEXICAL.value: lexical_scores,
            RetrievalSignal.VECTOR.value: vector_scores,
            RetrievalSignal.STRUCTURED.value: structured_scores,
        }
        rankings = {
            signal: tuple(
                record_id
                for record_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
            )
            for signal, scores in raw_scores.items()
            if scores
        }
        fused = reciprocal_rank_fusion(
            rankings,
            k=self._config.k,
            weights=self._config.weights,
        )
        records_by_id = {record.record_id: record for record in records}
        seen_documents: set[str] = set()
        ranked: list[RankedRetrievalItem] = []
        for fused_item in fused:
            record = records_by_id[fused_item.item_id]
            source_document_id = record.source_span.source_document_id
            diversity_bonus = (
                self._config.source_diversity_bonus
                if source_document_id not in seen_documents
                else 0.0
            )
            seen_documents.add(source_document_id)
            rerank_score = (
                fused_item.score
                + (self._config.confirmed_bonus if record.user_confirmed else 0.0)
                + (
                    self._config.strong_evidence_bonus
                    if record.evidence_strength is EvidenceStrength.STRONG
                    else 0.0
                )
                + diversity_bonus
                + _recency_bonus(
                    query,
                    record,
                    self._config.recent_source_bonus,
                    reference_now=reference_now,
                )
            )
            signal_ranks = tuple(
                SignalRank(
                    signal=RetrievalSignal(signal),
                    rank=rank,
                    raw_score=raw_scores[signal][record.record_id],
                )
                for signal, rank in sorted(fused_item.ranks.items())
            )
            ranked.append(
                RankedRetrievalItem(
                    record=record,
                    signal_ranks=signal_ranks,
                    rrf_score=fused_item.score,
                    rerank_score=rerank_score,
                    matched_signals=frozenset(rank.signal for rank in signal_ranks),
                )
            )
        ranked.sort(
            key=lambda item: (
                -(item.rerank_score or item.rrf_score),
                -item.rrf_score,
                item.record.record_id,
            )
        )
        return _RankPass(
            items=tuple(ranked[:limit]),
            lexical_evaluated=lexical_evaluated,
            lexical_ranked=len(lexical_scores),
            vector_evaluated=vector_evaluated,
            vector_ranked=len(vector_scores),
            vector_unusable=vector_unusable,
            structured_ranked=len(structured_scores),
            signals_used=tuple(sorted(rankings)),
        )

    @staticmethod
    def _final_filter(
        query: RetrievalQuery,
        items: Sequence[RankedRetrievalItem],
        excluded: Counter[str],
        *,
        reference_now: datetime,
    ) -> tuple[RankedRetrievalItem, ...]:
        safe: list[RankedRetrievalItem] = []
        for item in items:
            if _exclusion_reason(query, item.record, reference_now=reference_now) is None:
                safe.append(item)
            else:
                excluded[ExclusionReason.FINAL_SAFETY_FILTER.value] += 1
        return tuple(safe)


def hybrid_retrieve(
    query: RetrievalQuery,
    candidates: Sequence[RetrievalRecord],
    *,
    config: RRFConfig | None = None,
) -> HybridRetrievalResult:
    return HybridRetriever(config).retrieve(query, candidates)


@dataclass(frozen=True, slots=True)
class _RankPass:
    items: tuple[RankedRetrievalItem, ...]
    lexical_evaluated: int
    lexical_ranked: int
    vector_evaluated: int
    vector_ranked: int
    vector_unusable: int
    structured_ranked: int
    signals_used: tuple[str, ...]


def _empty_pass() -> _RankPass:
    return _RankPass((), 0, 0, 0, 0, 0, 0, ())


def _prepare_candidates(
    candidates: Sequence[RetrievalRecord],
) -> tuple[tuple[RetrievalRecord, ...], int]:
    """Validate immutable projections before privacy filtering and deduplicate.

    A caller must not be able to choose which projection wins by reordering the
    input. Record IDs therefore require exact equality, while a Source anchor
    requires stable source content and authorization state across every record
    that cites it. Exact duplicates are safe to collapse before ranking.
    """

    records_by_id: dict[str, RetrievalRecord] = {}
    source_projections: dict[
        tuple[str, str, str, str, int, int],
        tuple[str, str, SourceKind, bool, bool, int],
    ] = {}
    unique: list[RetrievalRecord] = []
    duplicate_count = 0

    for record in candidates:
        previous_record = records_by_id.get(record.record_id)
        if previous_record is not None:
            if previous_record != record:
                raise RetrievalProjectionConflictError(
                    f"record id {record.record_id!r} has conflicting projections"
                )
            duplicate_count += 1
            continue

        span = record.source_span
        source_anchor = (
            span.vault_id,
            span.source_document_id,
            span.source_revision_id,
            span.source_fragment_id,
            span.char_start,
            span.char_end,
        )
        source_projection = (
            span.quote,
            span.quote_hash,
            span.source_kind,
            record.consent_allowed,
            record.deleted,
            record.source_generation,
        )
        previous_projection = source_projections.setdefault(source_anchor, source_projection)
        if previous_projection != source_projection:
            raise RetrievalProjectionConflictError(
                "duplicate Source identity has conflicting content, hash, kind, "
                "consent, deletion, or generation"
            )

        records_by_id[record.record_id] = record
        unique.append(record)

    return tuple(unique), duplicate_count


def _exclusion_reason(
    query: RetrievalQuery,
    record: RetrievalRecord,
    *,
    reference_now: datetime,
) -> ExclusionReason | None:
    # These privacy checks intentionally precede every relevance calculation.
    if record.vault_id != query.vault_id:
        return ExclusionReason.VAULT_MISMATCH
    if record.source_generation != query.source_generation:
        return ExclusionReason.SOURCE_GENERATION_MISMATCH
    if not record.consent_allowed:
        return ExclusionReason.CONSENT_DENIED
    if record.deleted:
        return ExclusionReason.DELETED
    if record.index_policy is IndexPolicy.NONE:
        return ExclusionReason.INDEX_POLICY_NONE
    if record.source_span.source_kind is SourceKind.ARTIFACT:
        return ExclusionReason.ARTIFACT_SOURCE
    if record.sensitivity.rank > query.max_sensitivity.rank:
        return ExclusionReason.SENSITIVITY_EXCEEDED

    if not _time_matches(query, record, reference_now=reference_now):
        return ExclusionReason.TIME_MISMATCH
    if query.entity_ids:
        overlap = record.entity_ids & query.entity_ids
        if query.require_all_entities and overlap != query.entity_ids:
            return ExclusionReason.ENTITY_MISMATCH
        if not query.require_all_entities and not overlap:
            return ExclusionReason.ENTITY_MISMATCH
    if query.claim_kinds and record.claim_kind not in query.claim_kinds:
        return ExclusionReason.TYPE_MISMATCH
    if (
        query.lifecycle_states
        and record.lifecycle_state not in query.lifecycle_states
        and not (
            query.include_candidates and record.lifecycle_state is ClaimLifecycleState.CANDIDATE
        )
    ):
        return ExclusionReason.STATE_MISMATCH
    if (
        not query.lifecycle_states
        and query.purpose is RetrievalIntent.CURRENT_SELF_MODEL
        and record.lifecycle_state
        in {ClaimLifecycleState.SUPERSEDED, ClaimLifecycleState.RETRACTED}
    ):
        return ExclusionReason.STATE_MISMATCH
    if not query.include_candidates and record.lifecycle_state is ClaimLifecycleState.CANDIDATE:
        return ExclusionReason.CANDIDATE_EXCLUDED
    return None


def _time_matches(
    query: RetrievalQuery,
    record: RetrievalRecord,
    *,
    reference_now: datetime,
) -> bool:
    if query.as_of is not None:
        if record.valid_time is not None:
            return _temporal_contains(record.valid_time, query.as_of)
        if query.purpose is RetrievalIntent.HISTORICAL_SELF_MODEL:
            return False
        return record.recorded_at == query.as_of
    if query.time_scope is not None:
        if record.valid_time is not None:
            return _temporal_overlaps_scope(record.valid_time, query.time_scope)
        return record.recorded_at is not None and _scope_contains(
            query.time_scope, record.recorded_at
        )
    if query.purpose is RetrievalIntent.CURRENT_SELF_MODEL and record.valid_time is not None:
        return _temporal_contains(record.valid_time, reference_now)
    return True


def _temporal_contains(value: TemporalRange, point: datetime) -> bool:
    if value.earliest is None:
        return False
    return point >= value.earliest and (value.latest is None or point < value.latest)


def _scope_contains(scope: TimeScope, point: datetime) -> bool:
    return (scope.from_ is None or point >= scope.from_) and (scope.to is None or point < scope.to)


def _temporal_overlaps_scope(value: TemporalRange, scope: TimeScope) -> bool:
    if value.earliest is None:
        return False
    if value.latest is not None and scope.from_ is not None and value.latest <= scope.from_:
        return False
    return not (scope.to is not None and value.earliest >= scope.to)


def _lexical_scores(
    query: RetrievalQuery,
    records: Sequence[RetrievalRecord],
) -> tuple[dict[str, float], int]:
    query_terms = tokenize_lexical(query.text)
    lexical_records = [
        record
        for record in records
        if record.index_policy in {IndexPolicy.LEXICAL, IndexPolicy.BOTH}
    ]
    if not query_terms or not lexical_records:
        return {}, len(lexical_records)

    terms_by_id: dict[str, tuple[str, ...]] = {}
    for record in lexical_records:
        supplied = tuple(
            token
            for lexical_term in record.lexical_terms
            for token in tokenize_lexical(lexical_term)
        )
        terms_by_id[record.record_id] = tokenize_lexical(record.text) + supplied
    average_length = max(
        sum(len(terms) for terms in terms_by_id.values()) / len(terms_by_id),
        1.0,
    )
    document_frequency: Counter[str] = Counter()
    query_term_set = set(query_terms)
    for terms in terms_by_id.values():
        document_frequency.update(set(terms) & query_term_set)

    scores: dict[str, float] = {}
    query_frequency = Counter(query_terms)
    document_count = len(terms_by_id)
    for record_id, terms in terms_by_id.items():
        term_frequency = Counter(terms)
        score = 0.0
        for term, query_count in query_frequency.items():
            frequency = term_frequency[term]
            if not frequency:
                continue
            containing_documents = document_frequency[term]
            inverse_document_frequency = math.log(
                1 + (document_count - containing_documents + 0.5) / (containing_documents + 0.5)
            )
            normalization = frequency + 1.2 * (0.25 + 0.75 * len(terms) / average_length)
            score += (
                inverse_document_frequency
                * frequency
                * 2.2
                / normalization
                * (1 + math.log(query_count))
            )
        if score > 0:
            scores[record_id] = score
    return scores, len(lexical_records)


def _vector_scores(
    query: RetrievalQuery,
    records: Sequence[RetrievalRecord],
) -> tuple[dict[str, float], int, int]:
    semantic_records = [
        record
        for record in records
        if record.index_policy in {IndexPolicy.SEMANTIC, IndexPolicy.BOTH} and record.embedding
    ]
    if not query.query_embedding:
        return {}, 0, 0
    scores: dict[str, float] = {}
    unusable = 0
    for record in semantic_records:
        similarity = cosine_similarity(query.query_embedding, record.embedding)
        if similarity is None:
            unusable += 1
        else:
            scores[record.record_id] = similarity
    return scores, len(semantic_records), unusable


def _structured_scores(
    query: RetrievalQuery,
    records: Sequence[RetrievalRecord],
    *,
    linked_counter_ids: frozenset[str],
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for record in records:
        score = record.structured_score
        if query.entity_ids:
            score += 3 * len(record.entity_ids & query.entity_ids) / len(query.entity_ids)
        if query.claim_kinds and record.claim_kind in query.claim_kinds:
            score += 2
        if query.lifecycle_states and record.lifecycle_state in query.lifecycle_states:
            score += 1
        if query.time_scope is not None or query.as_of is not None:
            score += 1
        if (
            query.purpose is RetrievalIntent.CURRENT_SELF_MODEL
            and record.lifecycle_state is ClaimLifecycleState.ACTIVE
        ):
            score += 1
        if record.user_confirmed:
            score += 0.25
        score += {
            None: 0,
            EvidenceStrength.WEAK: 0.1,
            EvidenceStrength.MODERATE: 0.2,
            EvidenceStrength.STRONG: 0.25,
        }[record.evidence_strength]
        if record.record_id in linked_counter_ids:
            score += 2
        if score > 0:
            scores[record.record_id] = score
    return scores


def _recency_bonus(
    query: RetrievalQuery,
    record: RetrievalRecord,
    maximum_bonus: float,
    *,
    reference_now: datetime,
) -> float:
    if query.purpose is not RetrievalIntent.RECENT_CONTEXT or record.recorded_at is None:
        return 0.0
    age_seconds = max(0.0, (reference_now - record.recorded_at).total_seconds())
    age_days = age_seconds / 86_400
    return maximum_bonus / (1 + age_days)


def _counterevidence_required(query: RetrievalQuery) -> bool:
    if query.search_counterevidence is not None:
        return query.search_counterevidence
    return query.purpose in {
        RetrievalIntent.CURRENT_SELF_MODEL,
        RetrievalIntent.HISTORICAL_SELF_MODEL,
        RetrievalIntent.PATTERN_REFLECTION,
        RetrievalIntent.ACTION_SUPPORT,
        RetrievalIntent.NARRATIVE_RESEARCH,
    }


def _signal_name(signal: str | RetrievalSignal) -> str:
    return signal.value if isinstance(signal, RetrievalSignal) else signal


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2FA1F
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


RetrievalPurpose = RetrievalIntent
RetrievalDocument = RetrievalRecord
RetrievalResult = HybridRetrievalResult

__all__ = [
    "ExclusionReason",
    "FusedResult",
    "HybridRetrievalResult",
    "HybridRetriever",
    "RRFConfig",
    "RankedRetrievalItem",
    "RetrievalDocument",
    "RetrievalIntent",
    "RetrievalProjectionConflictError",
    "RetrievalPurpose",
    "RetrievalQuery",
    "RetrievalRecord",
    "RetrievalResult",
    "RetrievalSignal",
    "cosine_similarity",
    "exact_vector_rank",
    "hybrid_retrieve",
    "reciprocal_rank_fusion",
    "tokenize_lexical",
]
