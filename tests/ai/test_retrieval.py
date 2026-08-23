from __future__ import annotations

from datetime import UTC, datetime

import pytest

from life_coach.ai.context import build_context_pack
from life_coach.ai.contracts import (
    ClaimLifecycleState,
    ContextPolicySnapshot,
    EvidenceRelation,
    IndexPolicy,
    RankedRetrievalItem,
    RetrievalIntent,
    RetrievalQuery,
    RetrievalRecord,
    RetrievalSignal,
    SensitivityLevel,
    SourceKind,
    SourceSpan,
    TemporalRange,
    TimePrecision,
)
from life_coach.ai.retrieval import (
    HybridRetriever,
    RRFConfig,
    cosine_similarity,
    exact_vector_rank,
    reciprocal_rank_fusion,
    tokenize_lexical,
)

VAULT = "vault-a"


def _record(
    record_id: str,
    *,
    vault_id: str = VAULT,
    text: str = "retrieval text",
    embedding: tuple[float, ...] = (),
    index_policy: IndexPolicy = IndexPolicy.BOTH,
    source_kind: SourceKind = SourceKind.SOURCE,
    deleted: bool = False,
    consent_allowed: bool = True,
    sensitivity: SensitivityLevel = SensitivityLevel.NORMAL,
    lifecycle_state: ClaimLifecycleState = ClaimLifecycleState.ACTIVE,
    valid_time: TemporalRange | None = None,
    relation: EvidenceRelation = EvidenceRelation.SUPPORTS,
    contradiction_ids: tuple[str, ...] = (),
    structured_score: float = 0.0,
) -> RetrievalRecord:
    return RetrievalRecord(
        record_id=record_id,
        vault_id=vault_id,
        text=text,
        source_span=SourceSpan(
            vault_id=vault_id,
            source_document_id=f"document-{record_id}",
            source_revision_id=f"revision-{record_id}",
            source_fragment_id=f"fragment-{record_id}",
            char_start=0,
            char_end=len(text),
            quote=text,
            source_kind=source_kind,
        ),
        embedding=embedding,
        index_policy=index_policy,
        deleted=deleted,
        consent_allowed=consent_allowed,
        sensitivity=sensitivity,
        lifecycle_state=lifecycle_state,
        valid_time=valid_time,
        relation=relation,
        contradiction_ids=contradiction_ids,
        structured_score=structured_score,
    )


def _time_range(start: datetime, end: datetime, expression: str) -> TemporalRange:
    return TemporalRange(
        precision=TimePrecision.RANGE,
        original_expression=expression,
        earliest=start,
        latest=end,
        timezone="UTC",
    )


def _zero_bonus_config(*, weights: dict[str, float] | None = None) -> RRFConfig:
    return RRFConfig(
        weights=weights
        or {
            RetrievalSignal.LEXICAL.value: 1.0,
            RetrievalSignal.VECTOR.value: 1.0,
            RetrievalSignal.STRUCTURED.value: 1.0,
        },
        confirmed_bonus=0,
        strong_evidence_bonus=0,
        source_diversity_bonus=0,
        recent_source_bonus=0,
    )


def _raw_scores(record_result: RankedRetrievalItem) -> dict[RetrievalSignal, float]:
    return {rank.signal: rank.raw_score for rank in record_result.signal_ranks}


def test_rrf_formula_uses_best_duplicate_rank() -> None:
    fused = reciprocal_rank_fusion(
        {
            "lexical": ("a", "b", "a"),
            "vector": ("b", "c"),
        },
        k=0,
    )

    assert [item.item_id for item in fused] == ["b", "a", "c"]
    assert fused[0].score == pytest.approx(1 / 2 + 1 / 1)
    assert fused[1].score == pytest.approx(1 / 1)
    assert fused[2].score == pytest.approx(1 / 2)
    assert fused[1].ranks == {"lexical": 1}


def test_rrf_has_stable_id_ties_and_configurable_weights() -> None:
    tied = reciprocal_rank_fusion(
        {"first": ("b",), "second": ("a",)},
        k=60,
    )
    assert [item.item_id for item in tied] == ["a", "b"]

    weighted = reciprocal_rank_fusion(
        {
            RetrievalSignal.LEXICAL: ("lexical-winner",),
            RetrievalSignal.VECTOR: ("vector-winner",),
        },
        weights={RetrievalSignal.LEXICAL: 2.0, RetrievalSignal.VECTOR: 1.0},
    )
    assert [item.item_id for item in weighted] == ["lexical-winner", "vector-winner"]
    assert weighted[0].score == pytest.approx(2 / 61)
    assert weighted[1].score == pytest.approx(1 / 61)


def test_cosine_and_exact_vector_rank_score_every_usable_vector() -> None:
    assert cosine_similarity((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)
    assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)
    assert cosine_similarity((1.0, 0.0), (-1.0, 0.0)) == pytest.approx(-1.0)
    assert cosine_similarity((0.0, 0.0), (1.0, 0.0)) is None
    assert cosine_similarity((1.0,), (1.0, 0.0)) is None

    records = (
        _record("perfect", embedding=(1.0, 0.0), index_policy=IndexPolicy.SEMANTIC),
        _record("diagonal", embedding=(1.0, 1.0), index_policy=IndexPolicy.BOTH),
        _record("negative", embedding=(-1.0, 0.0), index_policy=IndexPolicy.SEMANTIC),
        _record("zero", embedding=(0.0, 0.0), index_policy=IndexPolicy.SEMANTIC),
        _record("mismatch", embedding=(1.0,), index_policy=IndexPolicy.SEMANTIC),
        _record("lexical-only", embedding=(1.0, 0.0), index_policy=IndexPolicy.LEXICAL),
    )

    ranked = exact_vector_rank((1.0, 0.0), records)

    assert [record_id for record_id, _ in ranked] == ["perfect", "diagonal", "negative"]
    assert ranked[0][1] == pytest.approx(1.0)
    assert ranked[1][1] == pytest.approx(2**-0.5)
    assert ranked[2][1] == pytest.approx(-1.0)


def test_unicode_cjk_lexical_search_recalls_name_and_phrase() -> None:
    tokens = tokenize_lexical("今天和小李谈团队")
    assert {"小", "李", "小李", "团队"} <= set(tokens)

    query = RetrievalQuery(
        vault_id=VAULT,
        text="小李",
        purpose=RetrievalIntent.FACT_LOOKUP,
        lifecycle_states=frozenset(),
        limit=2,
    )
    result = HybridRetriever(_zero_bonus_config()).retrieve(
        query,
        (
            _record(
                "match",
                text="今天和小李谈了团队安排",
                index_policy=IndexPolicy.LEXICAL,
            ),
            _record("unrelated", text="周末去公园", index_policy=IndexPolicy.LEXICAL),
        ),
    )

    assert [item.record.record_id for item in result.items] == ["match"]
    assert RetrievalSignal.LEXICAL in result.items[0].matched_signals


def test_fts_vector_and_structured_signals_are_fused() -> None:
    query = RetrievalQuery(
        vault_id=VAULT,
        text="focus",
        purpose=RetrievalIntent.FACT_LOOKUP,
        query_embedding=(1.0, 0.0),
        lifecycle_states=frozenset(),
    )
    retriever = HybridRetriever(
        _zero_bonus_config(
            weights={
                RetrievalSignal.LEXICAL.value: 1.0,
                RetrievalSignal.VECTOR.value: 1.0,
                RetrievalSignal.STRUCTURED.value: 2.0,
            }
        )
    )
    result = retriever.retrieve(
        query,
        (
            _record("lexical", text="focus focus planning", embedding=(0.0, 1.0)),
            _record("hybrid", text="focus", embedding=(1.0, 0.0), structured_score=5),
            _record("vector", text="unrelated", embedding=(0.9, 0.1)),
        ),
    )

    assert result.items[0].record.record_id == "hybrid"
    assert result.items[0].matched_signals == frozenset(
        {
            RetrievalSignal.LEXICAL,
            RetrievalSignal.VECTOR,
            RetrievalSignal.STRUCTURED,
        }
    )
    assert result.coverage["exact_vector_search"] is True
    assert result.coverage["signals_used"] == ["lexical", "structured", "vector"]


def test_policy_and_vault_filters_run_before_any_ranking() -> None:
    query = RetrievalQuery(
        vault_id=VAULT,
        text="needle",
        purpose=RetrievalIntent.FACT_LOOKUP,
        query_embedding=(1.0, 0.0),
        lifecycle_states=frozenset(),
        max_sensitivity=SensitivityLevel.NORMAL,
    )
    retriever = HybridRetriever(_zero_bonus_config())
    allowed = _record(
        "allowed",
        text="needle in authorized source",
        embedding=(0.8, 0.2),
        structured_score=1,
    )
    baseline = retriever.retrieve(query, (allowed,))
    forbidden = (
        _record("wrong-vault", vault_id="vault-b", text="needle", embedding=(1.0, 0.0)),
        _record("deleted", text="needle", embedding=(1.0, 0.0), deleted=True),
        _record(
            "without-consent",
            text="needle",
            embedding=(1.0, 0.0),
            consent_allowed=False,
        ),
        _record(
            "not-indexed",
            text="needle",
            embedding=(1.0, 0.0),
            index_policy=IndexPolicy.NONE,
        ),
        _record(
            "too-sensitive",
            text="needle",
            embedding=(1.0, 0.0),
            sensitivity=SensitivityLevel.HIGHLY_SENSITIVE,
        ),
        _record(
            "artifact",
            text="needle",
            embedding=(1.0, 0.0),
            source_kind=SourceKind.ARTIFACT,
        ),
    )

    result = retriever.retrieve(query, (allowed, *forbidden))

    assert [item.record.record_id for item in result.items] == ["allowed"]
    assert all(item.record.vault_id == VAULT for item in result.items)
    assert result.items[0].rrf_score == pytest.approx(baseline.items[0].rrf_score)
    assert result.items[0].signal_ranks == baseline.items[0].signal_ranks
    assert _raw_scores(result.items[0]) == pytest.approx(_raw_scores(baseline.items[0]))
    assert result.excluded_count_by_reason == {
        "artifact_source": 1,
        "consent_denied": 1,
        "deleted": 1,
        "index_policy_none": 1,
        "sensitivity_exceeded": 1,
        "vault_mismatch": 1,
    }
    assert result.coverage["eligible_count"] == 1


def test_pattern_reflection_returns_counterevidence_in_a_separate_safe_pass() -> None:
    query = RetrievalQuery(
        vault_id=VAULT,
        text="management role",
        purpose=RetrievalIntent.PATTERN_REFLECTION,
        query_embedding=(1.0, 0.0),
        lifecycle_states=frozenset(),
    )
    result = HybridRetriever(_zero_bonus_config()).retrieve(
        query,
        (
            _record(
                "support",
                text="I usually want a management role",
                embedding=(1.0, 0.0),
            ),
            _record(
                "counter",
                text="I do not want a management role now",
                embedding=(0.9, 0.1),
                relation=EvidenceRelation.CONTRADICTS,
                contradiction_ids=("support",),
            ),
            _record(
                "foreign-counter",
                vault_id="vault-b",
                text="management role",
                embedding=(1.0, 0.0),
                relation=EvidenceRelation.CONTRADICTS,
                contradiction_ids=("support",),
            ),
        ),
    )

    assert result.counterevidence_searched is True
    assert [item.record.record_id for item in result.items] == ["support"]
    assert [item.record.record_id for item in result.counterevidence] == ["counter"]
    assert result.excluded_count_by_reason["vault_mismatch"] == 1
    assert all(item.record.vault_id == VAULT for item in result.items)
    assert all(item.record.vault_id == VAULT for item in result.counterevidence)
    assert result.coverage["counterevidence_returned_count"] == 1

    context = build_context_pack(
        result,
        version="context-v1",
        policy_snapshot=ContextPolicySnapshot(
            consent_snapshot_id="consent-v1",
            policy_epoch=1,
            cross_record_analysis_allowed=True,
        ),
    )
    assert [span.source_fragment_id for span in context.source_quotes] == ["fragment-support"]
    assert context.counterevidence[0].source_span.source_fragment_id == "fragment-counter"
    assert context.coverage["counterevidence_searched"] is True
    assert context.citation_map["support"] == (result.items[0].record.source_span,)


@pytest.mark.parametrize(
    "query_options",
    [
        {"search_counterevidence": False},
        {"counterevidence_limit": 0},
    ],
)
def test_pattern_reflection_cannot_disable_counterevidence_pass(
    query_options: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="cannot disable"):
        RetrievalQuery(
            vault_id=VAULT,
            text="社交模式",
            purpose=RetrievalIntent.PATTERN_REFLECTION,
            **query_options,
        )


def test_current_and_historical_queries_respect_validity_and_superseded_state() -> None:
    at_2026 = datetime(2026, 6, 1, tzinfo=UTC)
    active_current = _record(
        "active-current",
        text="manager preference",
        valid_time=_time_range(
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
            "2025 through 2026",
        ),
    )
    expired_active = _record(
        "expired-active",
        text="manager preference",
        valid_time=_time_range(
            datetime(2023, 1, 1, tzinfo=UTC),
            datetime(2025, 1, 1, tzinfo=UTC),
            "2023 through 2024",
        ),
    )
    superseded_current = _record(
        "superseded-current",
        text="manager preference",
        lifecycle_state=ClaimLifecycleState.SUPERSEDED,
        valid_time=_time_range(
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
            "2025 through 2026",
        ),
    )
    retriever = HybridRetriever(_zero_bonus_config())
    current = retriever.retrieve(
        RetrievalQuery(
            vault_id=VAULT,
            text="manager",
            purpose=RetrievalIntent.CURRENT_SELF_MODEL,
            as_of=at_2026,
        ),
        (active_current, expired_active, superseded_current),
    )

    assert [item.record.record_id for item in current.items] == ["active-current"]
    assert current.excluded_count_by_reason["time_mismatch"] == 1
    assert current.excluded_count_by_reason["state_mismatch"] == 1

    old_superseded = _record(
        "old-superseded",
        text="manager preference in the past",
        lifecycle_state=ClaimLifecycleState.SUPERSEDED,
        valid_time=_time_range(
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2025, 1, 1, tzinfo=UTC),
            "during 2024",
        ),
    )
    historical = retriever.retrieve(
        RetrievalQuery(
            vault_id=VAULT,
            text="manager",
            purpose=RetrievalIntent.HISTORICAL_SELF_MODEL,
            as_of=datetime(2024, 6, 1, tzinfo=UTC),
            lifecycle_states=frozenset({ClaimLifecycleState.SUPERSEDED}),
        ),
        (old_superseded, active_current),
    )

    assert [item.record.record_id for item in historical.items] == ["old-superseded"]
    assert historical.items[0].record.lifecycle_state is ClaimLifecycleState.SUPERSEDED
