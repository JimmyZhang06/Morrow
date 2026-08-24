"""Candidate-insight output and authoritative persistence-boundary tests."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from life_coach.ai.contracts import (
    ModelInputKind,
    ModelInputRef,
    RetentionPolicy,
    SensitivityLevel,
)
from life_coach.ai.provider import UntrustedModelInput
from life_coach.application.candidate_insight import (
    CANDIDATE_INSIGHT_TASK_TYPE,
    CandidateInsightEvidence,
    CandidateInsightKind,
    CandidateInsightOutput,
    CandidateInsightPersister,
)
from life_coach.application.model_gateway import (
    AuthorizedSourceFragment,
    ModelTaskDefinition,
    PreparedModelInvocation,
    SourceAuthoritySnapshot,
    SourceAuthorityUnavailable,
)
from life_coach.application.model_runtime import (
    ModelResultContext,
    ModelResultRejected,
)
from life_coach.modules.consent.models import ConsentPurpose
from life_coach.modules.consent.provider_policy import ProviderPolicy
from life_coach.modules.identity.models import DataClass as SourceDataClass
from life_coach.modules.identity.service import VaultSnapshot
from life_coach.modules.knowledge.contracts import (
    ClaimProposal,
    EvidenceSourceVerifier,
    MemoryDetail,
)
from life_coach.modules.knowledge.enums import (
    Attribution,
    AuthorizationPurpose,
    ConfidenceBand,
    DataClass,
    EpistemicType,
    EvidenceExtractionReason,
    EvidenceRelation,
    EvidenceStrength,
    LifecycleState,
    MemoryClaimKind,
    TechnicalActor,
    ValidTimePrecision,
)
from life_coach.modules.knowledge.exceptions import PolicyViolationError
from life_coach.modules.model_runs.contracts import ModelRunArtifactSpec

_NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
_TEXT = "I keep making time to draw because it helps me recover after work."


class _Session:
    def __init__(self) -> None:
        self.sync_session = cast(Any, object())
        self.run_sync_calls = 0

    async def run_sync(self, operation: Any) -> Any:
        self.run_sync_calls += 1
        return operation(self.sync_session)


class _Authority:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.calls: list[tuple[object, SourceAuthoritySnapshot]] = []

    def assert_current(self, *, session: object, snapshot: SourceAuthoritySnapshot) -> None:
        self.calls.append((session, snapshot))
        if self.unavailable:
            raise SourceAuthorityUnavailable("revoked")


class _Writer:
    def __init__(
        self,
        *,
        detail: MemoryDetail,
        error: Exception | None = None,
    ) -> None:
        self.detail = detail
        self.error = error
        self.calls: list[tuple[uuid.UUID, ClaimProposal]] = []

    async def create_claim(
        self,
        *,
        vault_id: uuid.UUID,
        proposal: ClaimProposal,
    ) -> MemoryDetail:
        self.calls.append((vault_id, proposal))
        if self.error is not None:
            raise self.error
        return self.detail


class _MemoryFactory:
    def __init__(self, writer: _Writer) -> None:
        self.writer = writer
        self.calls: list[tuple[object, EvidenceSourceVerifier]] = []

    def __call__(
        self,
        session: object,
        verifier: EvidenceSourceVerifier,
    ) -> _Writer:
        self.calls.append((session, verifier))
        return self.writer


def _prepared(
    *,
    vault_id: uuid.UUID,
    fragments: tuple[AuthorizedSourceFragment, ...],
    input_ids: tuple[uuid.UUID, ...] | None = None,
) -> PreparedModelInvocation:
    task = ModelTaskDefinition(
        task_type=CANDIDATE_INSIGHT_TASK_TYPE,
        consent_purpose=ConsentPurpose.LONG_TERM_INFERENCE,
        provider="zero-retention-provider",
        model="structured-model",
        model_revision="revision-1",
        prompt_template_version="candidate-insight-v1",
        schema_version="1",
        pipeline_version="candidate-insight-v1",
        required_capabilities=frozenset({"structured_output"}),
        data_residency="eu",
        retention_policy=RetentionPolicy.ZERO_RETENTION,
        provider_retention_days=0,
        provider_training_use_enabled=False,
        max_sensitivity=SensitivityLevel.SENSITIVE,
        output_type=CandidateInsightOutput,
        latency_budget_ms=1_000,
        cost_budget=Decimal("0.01"),
    )
    snapshot = SourceAuthoritySnapshot(
        vault=VaultSnapshot(vault_id=vault_id, policy_epoch=3, source_generation=5),
        purpose=ConsentPurpose.LONG_TERM_INFERENCE,
        consent_snapshot_id=f"consent:{'c' * 64}",
        consent_snapshot_uuid=uuid.uuid4(),
        consent_record_ids=(uuid.uuid4(),),
        provider_policy=ProviderPolicy(
            allowed_providers=("zero-retention-provider",),
            processing_regions=("eu",),
            max_retention_days=0,
        ),
        fragments=fragments,
        actual_sensitivity=SensitivityLevel.SENSITIVE,
    )
    selected_ids = input_ids or tuple(fragment.fragment_id for fragment in fragments)
    return PreparedModelInvocation(
        task=task,
        snapshot=snapshot,
        input_refs=tuple(
            ModelInputRef(
                vault_id=str(vault_id),
                kind=ModelInputKind.SOURCE_FRAGMENT,
                object_id=str(fragment_id),
            )
            for fragment_id in selected_ids
        ),
        model_input=UntrustedModelInput(
            data={"fragments": []},
            source_refs=tuple(str(fragment_id) for fragment_id in selected_ids),
        ),
    )


def _fragment(
    fragment_id: uuid.UUID,
    *,
    text: str = _TEXT,
    data_class: SourceDataClass = SourceDataClass.SENSITIVE,
) -> AuthorizedSourceFragment:
    return AuthorizedSourceFragment(
        document_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        fragment_id=fragment_id,
        recorded_at=datetime(2026, 8, 20, tzinfo=UTC),
        data_class=data_class,
        text=text,
        text_hash=hashlib.sha256(text.encode()).hexdigest(),
    )


def _context(prepared: PreparedModelInvocation, *, run_id: uuid.UUID) -> ModelResultContext:
    return ModelResultContext(
        run_id=run_id,
        vault_id=prepared.snapshot.vault.vault_id,
        principal_id=uuid.uuid4(),
        membership_generation=2,
        prepared=prepared,
    )


def _candidate(fragment_id: uuid.UUID) -> CandidateInsightOutput:
    quote = "making time to draw"
    start = _TEXT.index(quote)
    return CandidateInsightOutput(
        kind=CandidateInsightKind.PREFERENCE,
        statement="Drawing may be a recurring restorative preference.",
        uncertainty="This is a tentative interpretation.",
        evidence=(
            CandidateInsightEvidence(
                source_fragment_id=fragment_id,
                quote_start=start,
                quote_end=start + len(quote),
            ),
        ),
    )


def _detail() -> MemoryDetail:
    return cast(
        MemoryDetail,
        SimpleNamespace(
            memory_id=uuid.uuid4(),
            version=SimpleNamespace(
                derived_object_id=uuid.uuid4(),
                state=LifecycleState.CANDIDATE,
            ),
        ),
    )


def _persister(
    authority: _Authority,
    factory: _MemoryFactory,
) -> CandidateInsightPersister:
    return CandidateInsightPersister(
        source_authority=authority,
        authorization_verifier=cast(Any, object()),
        safety_classifier=cast(Any, object()),
        clock=lambda: _NOW,
        memory_factory=cast(Any, factory),
    )


def test_output_schema_exposes_only_bounded_candidate_fields() -> None:
    schema = CandidateInsightOutput.model_json_schema()

    assert set(schema["properties"]) == {"kind", "statement", "uncertainty", "evidence"}
    evidence_schema = schema["$defs"]["CandidateInsightEvidence"]
    assert set(evidence_schema["properties"]) == {
        "source_fragment_id",
        "quote_start",
        "quote_end",
    }
    forbidden = {
        "confidence",
        "quote_hash",
        "model_run_id",
        "vault_id",
        "document_id",
        "revision_id",
    }
    assert forbidden.isdisjoint(schema["properties"])
    assert forbidden.isdisjoint(evidence_schema["properties"])

    with pytest.raises(ValidationError):
        CandidateInsightOutput.model_validate(
            {
                "kind": "self_description",
                "statement": "not an allowed first-release kind",
                "evidence": [],
            }
        )
    with pytest.raises(ValidationError):
        CandidateInsightEvidence.model_validate(
            {
                "source_fragment_id": str(uuid.uuid4()),
                "quote_start": 0,
                "quote_end": 1,
                "quote_hash": "0" * 64,
            }
        )


@pytest.mark.asyncio
async def test_unprepared_fragment_is_rejected_before_authority_or_memory_io() -> None:
    vault_id, allowed_id, unprepared_id, run_id = (uuid.uuid4() for _ in range(4))
    prepared = _prepared(vault_id=vault_id, fragments=(_fragment(allowed_id),))
    authority = _Authority()
    factory = _MemoryFactory(_Writer(detail=_detail()))
    session = _Session()
    candidate = _candidate(unprepared_id)

    with pytest.raises(ModelResultRejected, match="evidence is unavailable"):
        await _persister(authority, factory).persist(
            cast(Any, session),
            context=_context(prepared, run_id=run_id),
            result=candidate,
        )

    assert session.run_sync_calls == 0
    assert authority.calls == []
    assert factory.calls == []


@pytest.mark.asyncio
async def test_invalid_span_is_rejected_before_authority_or_memory_io() -> None:
    vault_id, fragment_id, run_id = (uuid.uuid4() for _ in range(3))
    prepared = _prepared(vault_id=vault_id, fragments=(_fragment(fragment_id),))
    authority = _Authority()
    factory = _MemoryFactory(_Writer(detail=_detail()))
    session = _Session()
    candidate = _candidate(fragment_id).model_copy(
        update={
            "evidence": (
                CandidateInsightEvidence(
                    source_fragment_id=fragment_id,
                    quote_start=0,
                    quote_end=len(_TEXT) + 1,
                ),
            )
        }
    )

    with pytest.raises(ModelResultRejected, match="evidence is unavailable"):
        await _persister(authority, factory).persist(
            cast(Any, session),
            context=_context(prepared, run_id=run_id),
            result=candidate,
        )

    assert session.run_sync_calls == 0
    assert authority.calls == []
    assert factory.calls == []


@pytest.mark.asyncio
async def test_prepared_input_binding_mismatch_fails_before_io() -> None:
    vault_id, fragment_id, other_id, run_id = (uuid.uuid4() for _ in range(4))
    prepared = _prepared(
        vault_id=vault_id,
        fragments=(_fragment(fragment_id),),
        input_ids=(other_id,),
    )
    authority = _Authority()
    factory = _MemoryFactory(_Writer(detail=_detail()))

    with pytest.raises(ModelResultRejected, match="input binding"):
        await _persister(authority, factory).persist(
            cast(Any, _Session()),
            context=_context(prepared, run_id=run_id),
            result=_candidate(fragment_id),
        )

    assert authority.calls == []
    assert factory.calls == []


@pytest.mark.asyncio
async def test_persister_mints_server_owned_claim_lineage_and_artifact() -> None:
    vault_id, fragment_id, run_id = (uuid.uuid4() for _ in range(3))
    fragment = _fragment(fragment_id, data_class=SourceDataClass.HIGHLY_SENSITIVE)
    prepared = _prepared(vault_id=vault_id, fragments=(fragment,))
    authority = _Authority()
    detail = _detail()
    writer = _Writer(detail=detail)
    factory = _MemoryFactory(writer)
    session = _Session()

    artifact = await _persister(authority, factory).persist(
        cast(Any, session),
        context=_context(prepared, run_id=run_id),
        result=_candidate(fragment_id),
    )

    assert artifact == ModelRunArtifactSpec(
        vault_id=vault_id,
        derived_object_id=detail.version.derived_object_id,
        memory_claim_id=detail.memory_id,
    )
    assert len(authority.calls) == 1
    assert len(factory.calls) == 1
    proposal = writer.calls[0][1]
    assert proposal.kind is MemoryClaimKind.PREFERENCE
    assert proposal.epistemic_type is EpistemicType.INFERRED
    assert proposal.attribution is Attribution.MODEL_HYPOTHESIS
    assert proposal.confidence_band is ConfidenceBand.LOW
    assert proposal.valid_time_precision is ValidTimePrecision.UNKNOWN
    assert proposal.valid_from == fragment.recorded_at
    assert proposal.structured_payload == {}
    assert proposal.pipeline_version == prepared.task.pipeline_version
    assert proposal.model_run_id == run_id
    assert proposal.created_by is TechnicalActor.KNOWLEDGE_PIPELINE
    assert proposal.data_class is DataClass.HIGHLY_SENSITIVE
    anchor = proposal.evidence[0]
    expected = _candidate(fragment_id).evidence[0]
    quote = _TEXT[expected.quote_start : expected.quote_end]
    assert anchor.quote_hash == hashlib.sha256(quote.encode()).hexdigest()
    assert anchor.model_run_id == run_id
    assert anchor.relation is EvidenceRelation.SUPPORTS
    assert anchor.extractor_reason is EvidenceExtractionReason.CONTEXT
    assert anchor.strength_band is EvidenceStrength.WEAK
    assert anchor.created_by is TechnicalActor.KNOWLEDGE_PIPELINE

    verifier = factory.calls[0][1]
    verified = verifier.verify(
        session=cast(Any, session.sync_session),
        vault_id=vault_id,
        anchor=anchor,
        purpose=AuthorizationPurpose.MEMORY_CREATE,
        at=_NOW,
    )
    assert verified.model_run_id == run_id
    assert verified.source_fragment_id == fragment_id
    assert len(authority.calls) == 2


@pytest.mark.asyncio
async def test_authority_change_is_a_deterministic_rejection_without_memory_io() -> None:
    vault_id, fragment_id, run_id = (uuid.uuid4() for _ in range(3))
    prepared = _prepared(vault_id=vault_id, fragments=(_fragment(fragment_id),))
    authority = _Authority(unavailable=True)
    factory = _MemoryFactory(_Writer(detail=_detail()))

    with pytest.raises(ModelResultRejected, match="authorization changed"):
        await _persister(authority, factory).persist(
            cast(Any, _Session()),
            context=_context(prepared, run_id=run_id),
            result=_candidate(fragment_id),
        )

    assert len(authority.calls) == 1
    assert factory.calls == []


@pytest.mark.asyncio
async def test_knowledge_rejection_is_stable_and_does_not_reflect_private_text() -> None:
    vault_id, fragment_id, run_id = (uuid.uuid4() for _ in range(3))
    prepared = _prepared(vault_id=vault_id, fragments=(_fragment(fragment_id),))
    authority = _Authority()
    writer = _Writer(
        detail=_detail(),
        error=PolicyViolationError(_TEXT),
    )

    with pytest.raises(ModelResultRejected) as exc_info:
        await _persister(authority, _MemoryFactory(writer)).persist(
            cast(Any, _Session()),
            context=_context(prepared, run_id=run_id),
            result=_candidate(fragment_id),
        )

    assert _TEXT not in str(exc_info.value)


def test_safety_classifier_is_a_required_fail_closed_dependency() -> None:
    with pytest.raises(ValueError, match="authorities must be configured"):
        CandidateInsightPersister(
            source_authority=cast(Any, object()),
            authorization_verifier=cast(Any, object()),
            safety_classifier=cast(Any, None),
        )
