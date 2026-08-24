"""Production mapping for one evidence-bound candidate-insight model task."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy.orm import Session

from life_coach.application.model_gateway import (
    AuthorizedSourceFragment,
    ModelInvocationDenied,
    PreparedModelInvocation,
    SourceAuthoritySnapshot,
    SourceAuthorityUnavailable,
)
from life_coach.application.model_runtime import (
    ModelResultContext,
    ModelResultRejected,
)
from life_coach.modules.identity.models import DataClass as SourceDataClass
from life_coach.modules.knowledge.contracts import (
    ClaimProposal,
    EvidenceAnchor,
    EvidenceSourceReference,
    EvidenceSourceState,
    EvidenceSourceVerifier,
    MemoryAuthorizationVerifier,
    MemoryDetail,
    MemorySafetyClassifier,
    VerifiedEvidenceAnchor,
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
from life_coach.modules.knowledge.exceptions import KnowledgeError
from life_coach.modules.knowledge.service import AsyncMemoryService
from life_coach.modules.model_runs.contracts import ModelRunArtifactSpec
from life_coach.platform.database import VaultAsyncSession

CANDIDATE_INSIGHT_TASK_TYPE = "candidate_insight"
_MAX_EVIDENCE = 4
_DATA_CLASS_RANK = {
    SourceDataClass.NORMAL: 0,
    SourceDataClass.SENSITIVE: 1,
    SourceDataClass.HIGHLY_SENSITIVE: 2,
}

_Statement = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
_Uncertainty = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class CandidateInsightKind(StrEnum):
    """The deliberately small first-release candidate taxonomy."""

    PREFERENCE = "preference"
    VALUE = "value"
    GOAL = "goal"


class CandidateInsightEvidence(BaseModel):
    """A span pointer into plaintext already authorized for this invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_fragment_id: uuid.UUID
    quote_start: int = Field(ge=0)
    quote_end: int = Field(gt=0)

    @model_validator(mode="after")
    def require_nonempty_span(self) -> CandidateInsightEvidence:
        if self.quote_end <= self.quote_start:
            raise ValueError("candidate evidence span must be non-empty")
        return self


class CandidateInsightOutput(BaseModel):
    """The only provider-authored fields accepted by the candidate insight task.

    Confidence, provenance, Vault identity, document ancestry, content hashes and
    model-run lineage are intentionally absent and are minted by the server.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: CandidateInsightKind
    statement: _Statement
    uncertainty: _Uncertainty | None = None
    evidence: tuple[CandidateInsightEvidence, ...] = Field(
        min_length=1,
        max_length=_MAX_EVIDENCE,
    )

    @model_validator(mode="after")
    def require_unique_spans(self) -> CandidateInsightOutput:
        identities = {
            (item.source_fragment_id, item.quote_start, item.quote_end) for item in self.evidence
        }
        if len(identities) != len(self.evidence):
            raise ValueError("candidate evidence spans must be unique")
        return self


class CandidateInsightSourceAuthority(Protocol):
    """The narrow no-decryption authority needed during finalization."""

    def assert_current(
        self,
        *,
        session: Session,
        snapshot: SourceAuthoritySnapshot,
    ) -> None: ...


class CandidateInsightMemoryWriter(Protocol):
    async def create_claim(
        self,
        *,
        vault_id: uuid.UUID,
        proposal: ClaimProposal,
    ) -> MemoryDetail: ...


type CandidateInsightMemoryFactory = Callable[
    [VaultAsyncSession, EvidenceSourceVerifier],
    CandidateInsightMemoryWriter,
]


class _PreparedEvidenceVerifier:
    """Verify proposed spans from the prepared plaintext under current fences."""

    def __init__(
        self,
        *,
        prepared: PreparedModelInvocation,
        run_id: uuid.UUID,
        source_authority: CandidateInsightSourceAuthority,
    ) -> None:
        self._prepared = prepared
        self._run_id = run_id
        self._source_authority = source_authority
        self._fragments = {
            fragment.fragment_id: fragment for fragment in prepared.snapshot.fragments
        }

    def verify(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        anchor: EvidenceAnchor,
        purpose: AuthorizationPurpose,
        at: datetime,
    ) -> VerifiedEvidenceAnchor:
        if (
            vault_id != self._prepared.snapshot.vault.vault_id
            or purpose is not AuthorizationPurpose.MEMORY_CREATE
            or anchor.model_run_id != self._run_id
            or anchor.created_by is not TechnicalActor.KNOWLEDGE_PIPELINE
        ):
            raise ModelResultRejected("candidate evidence authorization is unavailable")
        fragment = self._fragments.get(anchor.source_fragment_id)
        if fragment is None:
            raise ModelResultRejected("candidate evidence authorization is unavailable")
        self._source_authority.assert_current(
            session=session,
            snapshot=self._prepared.snapshot,
        )
        if not 0 <= anchor.quote_start < anchor.quote_end <= len(fragment.text):
            raise ModelResultRejected("candidate evidence span is unavailable")
        quote_hash = hashlib.sha256(
            fragment.text[anchor.quote_start : anchor.quote_end].encode("utf-8")
        ).hexdigest()
        if (
            anchor.quote_hash != quote_hash
            or anchor.relation is not EvidenceRelation.SUPPORTS
            or anchor.extractor_reason is not EvidenceExtractionReason.CONTEXT
            or anchor.strength_band is not EvidenceStrength.WEAK
        ):
            raise ModelResultRejected("candidate evidence span is unavailable")
        return VerifiedEvidenceAnchor(
            vault_id=vault_id,
            source_document_id=fragment.document_id,
            source_revision_id=fragment.revision_id,
            source_fragment_id=fragment.fragment_id,
            relation=EvidenceRelation.SUPPORTS,
            quote_start=anchor.quote_start,
            quote_end=anchor.quote_end,
            quote_hash=quote_hash,
            extractor_reason=EvidenceExtractionReason.CONTEXT,
            strength_band=EvidenceStrength.WEAK,
            source_recorded_at=fragment.recorded_at,
            source_data_class=DataClass(fragment.data_class.value),
            source_content_fingerprint=fragment.text_hash,
            authorization_snapshot_id=self._prepared.snapshot.consent_snapshot_uuid,
            policy_epoch=self._prepared.snapshot.vault.policy_epoch,
            source_generation=self._prepared.snapshot.vault.source_generation,
            verified_at=_aware_utc(at),
            created_by=TechnicalActor.KNOWLEDGE_PIPELINE,
            model_run_id=self._run_id,
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
        del session, vault_id, references, purpose, at
        raise ModelResultRejected("candidate evidence history is unavailable")


class CandidateInsightPersister:
    """Map the bounded model output into one evidence-backed Knowledge candidate."""

    def __init__(
        self,
        *,
        source_authority: CandidateInsightSourceAuthority,
        authorization_verifier: MemoryAuthorizationVerifier,
        safety_classifier: MemorySafetyClassifier,
        clock: Callable[[], datetime] | None = None,
        memory_factory: CandidateInsightMemoryFactory | None = None,
    ) -> None:
        if source_authority is None or authorization_verifier is None or safety_classifier is None:
            raise ValueError("candidate insight authorities must be configured")
        self._source_authority = source_authority
        self._authorization_verifier = authorization_verifier
        self._safety_classifier = safety_classifier
        self._clock = clock or (lambda: datetime.now(UTC))
        self._memory_factory = memory_factory or self._new_memory_service

    async def persist(
        self,
        session: VaultAsyncSession,
        *,
        context: ModelResultContext,
        result: BaseModel,
    ) -> ModelRunArtifactSpec:
        candidate, fragments = self._validate_boundary(context=context, result=result)
        _aware_utc(self._clock())
        anchors = tuple(
            self._anchor(
                candidate=item,
                fragment_text=fragments[item.source_fragment_id].text,
                run_id=context.run_id,
            )
            for item in candidate.evidence
        )

        # This is deliberately after every in-memory allow-list/span check. A
        # rejected model fragment therefore cannot trigger Source/Knowledge I/O.
        try:
            await session.run_sync(
                lambda sync_session: self._source_authority.assert_current(
                    session=sync_session,
                    snapshot=context.prepared.snapshot,
                )
            )
        except (ModelInvocationDenied, SourceAuthorityUnavailable):
            raise ModelResultRejected(
                "candidate insight authorization changed before persistence"
            ) from None
        evidence_verifier = _PreparedEvidenceVerifier(
            prepared=context.prepared,
            run_id=context.run_id,
            source_authority=self._source_authority,
        )
        writer = self._memory_factory(session, evidence_verifier)
        proposal = ClaimProposal(
            kind=_MEMORY_KIND[candidate.kind],
            canonical_text=candidate.statement,
            structured_payload={},
            epistemic_type=EpistemicType.INFERRED,
            attribution=Attribution.MODEL_HYPOTHESIS,
            uncertainty_text=candidate.uncertainty,
            valid_from=min(
                fragments[item.source_fragment_id].recorded_at for item in candidate.evidence
            ),
            valid_time_precision=ValidTimePrecision.UNKNOWN,
            confidence_band=ConfidenceBand.LOW,
            pipeline_version=context.prepared.task.pipeline_version,
            model_run_id=context.run_id,
            data_class=_effective_data_class(tuple(fragments.values())),
            created_by=TechnicalActor.KNOWLEDGE_PIPELINE,
            evidence=anchors,
        )
        try:
            detail = await writer.create_claim(
                vault_id=context.vault_id,
                proposal=proposal,
            )
        except KnowledgeError:
            raise ModelResultRejected("candidate insight was rejected by Knowledge") from None
        if detail.version.state is not LifecycleState.CANDIDATE:
            raise ModelResultRejected("candidate insight did not remain reviewable")
        return ModelRunArtifactSpec(
            vault_id=context.vault_id,
            derived_object_id=detail.version.derived_object_id,
            memory_claim_id=detail.memory_id,
        )

    def _new_memory_service(
        self,
        session: VaultAsyncSession,
        evidence_verifier: EvidenceSourceVerifier,
    ) -> CandidateInsightMemoryWriter:
        return AsyncMemoryService(
            session,
            evidence_source_verifier=evidence_verifier,
            safety_classifier=self._safety_classifier,
            authorization_verifier=self._authorization_verifier,
            clock=self._clock,
        )

    @staticmethod
    def _validate_boundary(
        *,
        context: ModelResultContext,
        result: BaseModel,
    ) -> tuple[CandidateInsightOutput, dict[uuid.UUID, AuthorizedSourceFragment]]:
        prepared = context.prepared
        snapshot = prepared.snapshot
        if (
            type(result) is not CandidateInsightOutput
            or prepared.task.task_type != CANDIDATE_INSIGHT_TASK_TYPE
            or prepared.task.output_type is not CandidateInsightOutput
            or snapshot.vault.vault_id != context.vault_id
        ):
            raise ModelResultRejected("candidate insight result binding is unavailable")
        candidate = result
        fragments = {fragment.fragment_id: fragment for fragment in snapshot.fragments}
        if len(fragments) != len(snapshot.fragments):
            raise ModelResultRejected("candidate insight input binding is unavailable")
        try:
            input_ids = tuple(uuid.UUID(reference.object_id) for reference in prepared.input_refs)
        except (TypeError, ValueError, AttributeError):
            raise ModelResultRejected("candidate insight input binding is unavailable") from None
        if (
            len(input_ids) != len(prepared.input_refs)
            or input_ids != tuple(fragment.fragment_id for fragment in snapshot.fragments)
            or any(reference.kind.value != "source_fragment" for reference in prepared.input_refs)
            or any(reference.vault_id != str(context.vault_id) for reference in prepared.input_refs)
        ):
            raise ModelResultRejected("candidate insight input binding is unavailable")
        for evidence in candidate.evidence:
            fragment = fragments.get(evidence.source_fragment_id)
            if fragment is None or not (
                0 <= evidence.quote_start < evidence.quote_end <= len(fragment.text)
            ):
                raise ModelResultRejected("candidate insight evidence is unavailable")
        return candidate, fragments

    @staticmethod
    def _anchor(
        *,
        candidate: CandidateInsightEvidence,
        fragment_text: str,
        run_id: uuid.UUID,
    ) -> EvidenceAnchor:
        quote = fragment_text[candidate.quote_start : candidate.quote_end]
        return EvidenceAnchor(
            source_fragment_id=candidate.source_fragment_id,
            relation=EvidenceRelation.SUPPORTS,
            quote_hash=hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            extractor_reason=EvidenceExtractionReason.CONTEXT,
            quote_start=candidate.quote_start,
            quote_end=candidate.quote_end,
            strength_band=EvidenceStrength.WEAK,
            model_run_id=run_id,
            created_by=TechnicalActor.KNOWLEDGE_PIPELINE,
        )


_MEMORY_KIND = {
    CandidateInsightKind.PREFERENCE: MemoryClaimKind.PREFERENCE,
    CandidateInsightKind.VALUE: MemoryClaimKind.VALUE,
    CandidateInsightKind.GOAL: MemoryClaimKind.GOAL,
}


def _effective_data_class(fragments: tuple[AuthorizedSourceFragment, ...]) -> DataClass:
    source_class = max(
        (fragment.data_class for fragment in fragments),
        key=_DATA_CLASS_RANK.__getitem__,
    )
    return DataClass(source_class.value)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("candidate insight clock must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "CANDIDATE_INSIGHT_TASK_TYPE",
    "CandidateInsightEvidence",
    "CandidateInsightKind",
    "CandidateInsightOutput",
    "CandidateInsightPersister",
]
