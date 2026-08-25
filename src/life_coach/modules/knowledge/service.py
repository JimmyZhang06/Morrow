"""Transactional domain service for bitemporal memory and user verdicts."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from life_coach.shared.database import utc_now

from .contracts import (
    AuthorizationSnapshot,
    ClaimProposal,
    ClaimVersionView,
    CorrectionReplacement,
    CorrectionSourceRecorder,
    EvidenceAnchor,
    EvidenceSourceReference,
    EvidenceSourceState,
    EvidenceSourceVerifier,
    EvidenceView,
    InboxItem,
    InboxPage,
    MemoryAuthorizationVerifier,
    MemoryDetail,
    MemorySafetyClassifier,
    ReplacementValidTime,
    SafetyAssessment,
    SourceStateChange,
    SubjectEntityVerifier,
    VerdictOutcome,
    VerdictView,
    VerifiedEvidenceAnchor,
    VerifiedSubjectEntity,
)
from .enums import (
    Attribution,
    AuthorizationPurpose,
    ClaimVersionOrigin,
    ConfidenceBand,
    CorrectionMode,
    DataClass,
    DerivedObjectKind,
    EpistemicType,
    EvidenceExtractionReason,
    EvidenceRelation,
    EvidenceStrength,
    LifecycleState,
    SafetyDecision,
    SourceEvidenceStatus,
    TechnicalActor,
    ValidTimePrecision,
    VerdictType,
)
from .exceptions import (
    AuthorizationUnavailableError,
    CorrectionSourceUnavailableError,
    EvidenceNotFoundError,
    EvidenceSourceUnavailableError,
    InvalidEvidenceError,
    InvalidTemporalIntervalError,
    InvalidVerdictError,
    MemoryNotFoundError,
    PolicyViolationError,
    RevisionConflictError,
)
from .models import (
    ClaimVersion,
    DerivedObject,
    EvidenceLink,
    MemoryClaim,
    MemorySuppression,
    UserVerdict,
)
from .policy import (
    EvidenceForPolicy,
    activation_allowed,
    allowed_uses,
    contains_clinical_language,
    policy_lifecycle,
    validate_persistable_memory,
)
from .reducer import ReducedVerdict, reduce_verdicts, replacement_transition, transition_lifecycle


@dataclass(frozen=True, slots=True)
class _HistoricalEvidence:
    """Evidence projection with deletion already evaluated at an as-of instant."""

    source_fragment_id: uuid.UUID
    source_revision_id: uuid.UUID
    source_content_fingerprint: str
    relation: EvidenceRelation
    source_recorded_at: datetime | None
    deleted_at: datetime | None = None


class MemoryOperations(Protocol):
    """Small application port consumed by the FastAPI router factory."""

    def list_inbox(
        self, *, vault_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> InboxPage: ...

    def list_memories(
        self, *, vault_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> InboxPage: ...

    def get_detail(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        real_at: datetime | None = None,
        system_at: datetime | None = None,
    ) -> MemoryDetail: ...

    def record_verdict(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        verdict: VerdictType,
        expected_etag: str,
        replacement: CorrectionReplacement | None = None,
        reason: str | None = None,
    ) -> VerdictOutcome: ...


class AsyncMemoryOperations(Protocol):
    """Async application port used by FastAPI with the project's asyncpg stack."""

    async def list_inbox(
        self, *, vault_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> InboxPage: ...

    async def list_memories(
        self, *, vault_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> InboxPage: ...

    async def get_detail(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        real_at: datetime | None = None,
        system_at: datetime | None = None,
    ) -> MemoryDetail: ...

    async def record_verdict(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        verdict: VerdictType,
        expected_etag: str,
        replacement: CorrectionReplacement | None = None,
        reason: str | None = None,
    ) -> VerdictOutcome: ...


def make_etag(derived_object_id: uuid.UUID, version_no: int, review_revision: int) -> str:
    """Return an opaque aggregate token covering both version and verdict/evidence head."""

    return f'"cv:{derived_object_id}:{version_no}:{review_revision}"'


def make_snapshot_token(
    *, memory_id: uuid.UUID, derived_object_id: uuid.UUID, real_at: datetime, system_at: datetime
) -> str:
    material = (
        f"memory-snapshot-v1\0{memory_id}\0{derived_object_id}\0"
        f"{_utc(real_at).isoformat()}\0{_utc(system_at).isoformat()}"
    )
    return f'"snapshot:{hashlib.sha256(material.encode("utf-8")).hexdigest()}"'


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_aware(name: str, value: datetime | None) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise InvalidTemporalIntervalError(f"{name} must include a timezone")


def _validate_interval(name: str, start: datetime, end: datetime | None) -> None:
    _require_aware(f"{name}_from", start)
    _require_aware(f"{name}_to", end)
    if end is not None and _utc(start) >= _utc(end):
        raise InvalidTemporalIntervalError(f"{name} interval must be non-empty and half-open")


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TECHNICAL_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_DATA_CLASS_RANK = {
    DataClass.NORMAL: 0,
    DataClass.SENSITIVE: 1,
    DataClass.HIGHLY_SENSITIVE: 2,
}


def _validate_anchor(anchor: EvidenceAnchor) -> None:
    if _SHA256_RE.fullmatch(anchor.quote_hash) is None:
        raise InvalidEvidenceError("Evidence quote_hash must be lowercase SHA-256 hex")
    if not isinstance(anchor.extractor_reason, EvidenceExtractionReason):
        raise InvalidEvidenceError("Evidence extractor_reason must be a technical enum")
    if not isinstance(anchor.created_by, TechnicalActor):
        raise InvalidEvidenceError("Evidence created_by must be a technical actor")
    if anchor.quote_start < 0 or anchor.quote_end <= anchor.quote_start:
        raise InvalidEvidenceError("Evidence quote offsets must form a non-empty pair")


def _max_data_class(*values: DataClass) -> DataClass:
    return max(values, key=_DATA_CLASS_RANK.__getitem__)


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _claim_fingerprint(*, vault_id: uuid.UUID, proposal: ClaimProposal) -> str:
    payload = json.dumps(
        proposal.structured_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    material = "\0".join(
        (
            "claim-v1",
            str(vault_id),
            proposal.kind.value,
            str(proposal.subject_entity_id or ""),
            _normalized_text(proposal.canonical_text),
            _normalized_text(payload),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _replacement_fingerprint(
    *, vault_id: uuid.UUID, claim: MemoryClaim, replacement: CorrectionReplacement
) -> str:
    proposal = ClaimProposal(
        kind=claim.kind,
        canonical_text=replacement.statement,
        structured_payload={},
        epistemic_type=EpistemicType.USER_AUTHORED,
        attribution=Attribution.SELF_REPORT,
        valid_from=replacement.valid_time.valid_from
        if replacement.valid_time
        else datetime(1970, 1, 1, tzinfo=UTC),
        subject_entity_id=claim.subject_entity_id,
    )
    return _claim_fingerprint(vault_id=vault_id, proposal=proposal)


def _evidence_fingerprint(anchor: VerifiedEvidenceAnchor) -> str:
    material = "\0".join(
        (
            "evidence-v1",
            anchor.source_content_fingerprint,
            anchor.relation.value,
            str(anchor.quote_start),
            str(anchor.quote_end),
            anchor.quote_hash,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _user_texts(*values: Any) -> tuple[str, ...]:
    texts: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            texts.append(value)
        elif isinstance(value, Mapping):
            for key, child in value.items():
                collect(str(key))
                collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    for value in values:
        collect(value)
    return tuple(texts)


class MemoryService:
    """Knowledge aggregate service.

    The caller owns the outer SQLAlchemy transaction. Each mutating command uses a
    savepoint so a caught domain error cannot leave a Source correction, verdict, or
    replacement version partially pending in that transaction.
    """

    def __init__(
        self,
        session: Session,
        *,
        evidence_source_verifier: EvidenceSourceVerifier | None = None,
        correction_source_recorder: CorrectionSourceRecorder | None = None,
        safety_classifier: MemorySafetyClassifier | None = None,
        authorization_verifier: MemoryAuthorizationVerifier | None = None,
        subject_entity_verifier: SubjectEntityVerifier | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._evidence_source_verifier = evidence_source_verifier
        self._correction_source_recorder = correction_source_recorder
        self._safety_classifier = safety_classifier
        self._authorization_verifier = authorization_verifier
        self._subject_entity_verifier = subject_entity_verifier
        self._clock = clock

    def create_claim(self, *, vault_id: uuid.UUID, proposal: ClaimProposal) -> MemoryDetail:
        """Persist a proposal after provenance, temporal, and safety policy checks."""

        if not proposal.canonical_text.strip():
            raise PolicyViolationError("A memory statement cannot be blank")
        if not isinstance(proposal.created_by, TechnicalActor):
            raise PolicyViolationError("created_by must be a technical actor")
        if _TECHNICAL_ID_RE.fullmatch(proposal.pipeline_version) is None:
            raise PolicyViolationError("pipeline_version must be a restricted technical ID")
        _validate_interval("valid", proposal.valid_from, proposal.valid_to)
        for anchor in proposal.evidence:
            _validate_anchor(anchor)
        if any(anchor.model_run_id != proposal.model_run_id for anchor in proposal.evidence):
            raise InvalidEvidenceError(
                "Claim and evidence model-run lineage must be all absent or exactly equal"
            )
        now = self._now()
        safety = self._classify_texts(
            vault_id=vault_id,
            texts=_user_texts(
                proposal.canonical_text,
                proposal.structured_payload,
                proposal.uncertainty_text,
                proposal.valid_time_original,
            ),
            model_origin=(
                proposal.epistemic_type is EpistemicType.INFERRED
                or proposal.attribution is Attribution.MODEL_HYPOTHESIS
            ),
            at=now,
        )
        subject = self._verify_subject(
            vault_id=vault_id, subject_entity_id=proposal.subject_entity_id, at=now
        )
        model_origin = (
            proposal.epistemic_type is EpistemicType.INFERRED
            or proposal.attribution is Attribution.MODEL_HYPOTHESIS
        )
        if model_origin:
            validate_persistable_memory(
                canonical_text=proposal.canonical_text,
                structured_payload=proposal.structured_payload,
                epistemic_type=proposal.epistemic_type,
                attribution=proposal.attribution,
                data_class=_max_data_class(
                    proposal.data_class,
                    safety.data_class if safety is not None else DataClass.SENSITIVE,
                ),
            )
        verified_anchors = [
            self._verify_evidence_anchor(vault_id=vault_id, anchor=anchor, at=now)
            for anchor in proposal.evidence
        ]
        if not any(anchor.relation is EvidenceRelation.SUPPORTS for anchor in verified_anchors):
            raise InvalidEvidenceError(
                "A memory claim requires at least one supporting Source fragment"
            )
        effective_data_class = _max_data_class(
            proposal.data_class,
            *(anchor.source_data_class for anchor in verified_anchors),
            *([safety.data_class] if safety is not None else [DataClass.SENSITIVE]),
            *([subject.data_class] if subject is not None else []),
        )
        validate_persistable_memory(
            canonical_text=proposal.canonical_text,
            structured_payload=proposal.structured_payload,
            epistemic_type=proposal.epistemic_type,
            attribution=proposal.attribution,
            data_class=effective_data_class,
        )
        policy_epoch = max(anchor.policy_epoch for anchor in verified_anchors)
        source_generation = max(anchor.source_generation for anchor in verified_anchors)
        authorization = self._require_authorization(
            vault_id=vault_id,
            purpose=AuthorizationPurpose.MEMORY_CREATE,
            data_class=effective_data_class,
            policy_epoch=policy_epoch,
            source_generation=source_generation,
            at=now,
        )
        normalized_fingerprint = _claim_fingerprint(vault_id=vault_id, proposal=proposal)
        suppression = self._suppression_for_fingerprint(
            vault_id=vault_id, normalized_fingerprint=normalized_fingerprint
        )

        memory_id = uuid.uuid4()
        derived_id = uuid.uuid4()
        suppression_lineage_id = (
            suppression.suppression_lineage_id if suppression is not None else uuid.uuid4()
        )
        with self._session.begin_nested():
            claim = MemoryClaim(
                id=memory_id,
                vault_id=vault_id,
                kind=proposal.kind,
                subject_entity_id=proposal.subject_entity_id,
                suppression_lineage_id=suppression_lineage_id,
                created_by=proposal.created_by,
                data_class=effective_data_class,
                created_at=now,
                updated_at=now,
            )
            derived = DerivedObject(
                id=derived_id,
                vault_id=vault_id,
                object_kind=DerivedObjectKind.CLAIM_VERSION,
                created_by=proposal.created_by,
                data_class=effective_data_class,
                review_revision=0,
                created_at=now,
                updated_at=now,
            )
            evidence = [
                self._new_evidence(
                    vault_id=vault_id,
                    target_derived_object_id=derived_id,
                    anchor=anchor,
                    data_class=effective_data_class,
                    created_at=now,
                )
                for anchor in verified_anchors
            ]
            initial_state = policy_lifecycle(
                reduced_state=LifecycleState.CANDIDATE,
                activation_is_allowed=activation_allowed(
                    kind=proposal.kind,
                    data_class=effective_data_class,
                    epistemic_type=proposal.epistemic_type,
                    attribution=proposal.attribution,
                    evidence=evidence,
                    last_decisive_verdict=None,
                    requires_explicit_confirmation=suppression is not None,
                ),
                evidence=evidence,
            )
            version = ClaimVersion(
                derived_object_id=derived_id,
                vault_id=vault_id,
                claim_id=memory_id,
                version_no=1,
                canonical_text=proposal.canonical_text,
                structured_payload=dict(proposal.structured_payload),
                epistemic_type=proposal.epistemic_type,
                attribution=proposal.attribution,
                uncertainty_text=proposal.uncertainty_text,
                initial_lifecycle_state=initial_state,
                lifecycle_state=initial_state,
                valid_from=_utc(proposal.valid_from),
                valid_to=_utc(proposal.valid_to) if proposal.valid_to else None,
                valid_time_precision=proposal.valid_time_precision,
                valid_time_original=proposal.valid_time_original,
                valid_timezone=proposal.valid_timezone,
                system_from=now,
                system_to=None,
                confidence_band=proposal.confidence_band,
                pipeline_version=proposal.pipeline_version,
                model_run_id=proposal.model_run_id,
                origin=ClaimVersionOrigin.PIPELINE_DERIVED,
                correction_mode=None,
                supersedes_derived_object_id=None,
                origin_verdict_id=None,
                suppressed_by_derived_object_id=(
                    suppression.source_derived_object_id if suppression is not None else None
                ),
                suppression_override_verdict_id=None,
                normalized_fingerprint=normalized_fingerprint,
                authorization_snapshot_id=authorization.snapshot_id,
                authorization_policy_epoch=authorization.policy_epoch,
                authorization_source_generation=authorization.source_generation,
                safety_assessment_id=safety.assessment_id if safety is not None else None,
                safety_allows_proactive=(safety.allows_proactive if safety is not None else False),
                subject_verification_id=(subject.verification_id if subject is not None else None),
            )
            # There are intentionally no ORM relationships to the not-yet-owned Source
            # mapper. Flush super/stable parents explicitly before their FK extensions.
            self._session.add_all([claim, derived])
            self._session.flush([claim, derived])
            self._session.add_all([version, *evidence])
            self._session.flush()
        return self.get_detail(vault_id=vault_id, memory_id=memory_id)

    def get_as_of_version(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        real_at: datetime,
        system_at: datetime,
    ) -> ClaimVersionView:
        """Resolve both real and system half-open axes; neither axis is optional."""

        detail = self.get_detail(
            vault_id=vault_id,
            memory_id=memory_id,
            real_at=real_at,
            system_at=system_at,
        )
        return detail.version

    def get_detail(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        real_at: datetime | None = None,
        system_at: datetime | None = None,
    ) -> MemoryDetail:
        if (real_at is None) is not (system_at is None):
            raise InvalidTemporalIntervalError(
                "A bitemporal query must provide both real_at and system_at"
            )
        _require_aware("real_at", real_at)
        _require_aware("system_at", system_at)

        claim = self._claim(vault_id=vault_id, memory_id=memory_id)
        if real_at is None:
            version, derived = self._current_version(vault_id=vault_id, memory_id=memory_id)
            cutoff = None
        else:
            assert system_at is not None
            version, derived = self._as_of_version(
                vault_id=vault_id,
                memory_id=memory_id,
                real_at=_utc(real_at),
                system_at=_utc(system_at),
            )
            cutoff = _utc(system_at)

        selected_evidence = self._evidence_for_target(
            vault_id=vault_id,
            target_id=version.derived_object_id,
            system_at=cutoff,
        )
        selected_verdicts = self._verdicts_for_target(
            vault_id=vault_id,
            target_id=version.derived_object_id,
            system_at=cutoff,
        )
        if cutoff is None:
            selected_state, selected_reduction = self._effective_state(
                claim=claim,
                version=version,
                derived=derived,
                evidence=selected_evidence,
                verdicts=selected_verdicts,
            )
        else:
            selected_state, selected_reduction = self._historical_effective_state(
                claim=claim,
                version=version,
                derived=derived,
                system_at=cutoff,
            )

        history_rows = self._history(vault_id=vault_id, memory_id=memory_id, system_at=cutoff)
        history_views: list[ClaimVersionView] = []
        all_target_ids: list[uuid.UUID] = []
        for history_version, history_derived in history_rows:
            all_target_ids.append(history_version.derived_object_id)
            history_evidence = self._evidence_for_target(
                vault_id=vault_id,
                target_id=history_version.derived_object_id,
                system_at=cutoff,
            )
            history_verdicts = self._verdicts_for_target(
                vault_id=vault_id,
                target_id=history_version.derived_object_id,
                system_at=cutoff,
            )
            if cutoff is None:
                history_state, _ = self._effective_state(
                    claim=claim,
                    version=history_version,
                    derived=history_derived,
                    evidence=history_evidence,
                    verdicts=history_verdicts,
                )
            else:
                history_state, _ = self._historical_effective_state(
                    claim=claim,
                    version=history_version,
                    derived=history_derived,
                    system_at=cutoff,
                )
            history_views.append(
                self._version_view(history_version, history_state, claim.data_class)
            )

        all_verdicts = self._verdicts_for_targets(
            vault_id=vault_id, target_ids=all_target_ids, system_at=cutoff
        )
        governance_event = self._claim_governance_event(vault_id=vault_id, claim_id=claim.id)
        governance_verdict = governance_event.verdict if governance_event is not None else None
        governance_applies = governance_event is not None and (
            version.suppression_override_verdict_id != governance_event.id
        )
        current_version, _ = self._current_version(vault_id=vault_id, memory_id=memory_id)
        authorization = self._representation_authorization(
            vault_id=vault_id,
            purpose=(
                AuthorizationPurpose.MEMORY_REVIEW
                if cutoff is None
                else AuthorizationPurpose.MEMORY_HISTORY
            ),
            data_class=claim.data_class,
            policy_epoch=current_version.authorization_policy_epoch,
            source_generation=current_version.authorization_source_generation,
            at=self._now(),
        )
        current_source_evidence = (
            selected_evidence
            if cutoff is None
            else self._currently_authoritative_evidence(
                vault_id=vault_id,
                evidence=[item for item in selected_evidence if item.deleted_at is None],
            )
        )
        source_authority_current = any(
            item.relation is EvidenceRelation.SUPPORTS for item in current_source_evidence
        )
        suppression_pending = (
            version.suppressed_by_derived_object_id is not None
            and version.suppression_override_verdict_id is None
            and selected_reduction.last_decisive_verdict is not VerdictType.CONFIRM
        )
        evidence_views = tuple(self._evidence_view(item) for item in selected_evidence)
        is_current = cutoff is None
        snapshot_token = None
        if not is_current:
            assert real_at is not None and system_at is not None
            snapshot_token = make_snapshot_token(
                memory_id=claim.id,
                derived_object_id=version.derived_object_id,
                real_at=real_at,
                system_at=system_at,
            )
        return MemoryDetail(
            memory_id=claim.id,
            kind=claim.kind,
            subject_entity_id=claim.subject_entity_id,
            version=self._version_view(version, selected_state, claim.data_class),
            history=tuple(history_views),
            evidence=tuple(
                item for item in evidence_views if item.relation is EvidenceRelation.SUPPORTS
            ),
            counterevidence=tuple(
                item for item in evidence_views if item.relation is EvidenceRelation.CONTRADICTS
            ),
            contextual_evidence=tuple(
                item for item in evidence_views if item.relation is EvidenceRelation.CONTEXTUALIZES
            ),
            verdicts=tuple(self._verdict_view(item) for item in all_verdicts),
            current_verdict=self._surface_verdict(version, selected_reduction),
            governance_verdict=governance_verdict,
            data_class=claim.data_class,
            authorization_snapshot=authorization,
            is_current=is_current,
            etag=(
                make_etag(derived.id, version.version_no, derived.review_revision)
                if is_current
                else None
            ),
            snapshot_token=snapshot_token,
            allowed_uses=allowed_uses(
                state=selected_state,
                kind=claim.kind,
                data_class=claim.data_class,
                current_verdict=selected_reduction.current_verdict,
                governance_verdict=governance_verdict,
                governance_applies=governance_applies,
                is_historical=not is_current,
                source_authority_current=source_authority_current,
                authorization_allows_read=(
                    authorization.allows_read if authorization is not None else False
                ),
                authorization_allows_proactive=(
                    authorization.allows_proactive if authorization is not None else False
                ),
                safety_allows_proactive=version.safety_allows_proactive,
                suppression_pending=suppression_pending,
            ),
        )

    def list_inbox(
        self, *, vault_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> InboxPage:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        offset = self._decode_cursor(cursor)
        rows = self._session.execute(
            select(ClaimVersion, MemoryClaim, DerivedObject)
            .join(
                MemoryClaim,
                and_(
                    ClaimVersion.vault_id == MemoryClaim.vault_id,
                    ClaimVersion.claim_id == MemoryClaim.id,
                ),
            )
            .join(
                DerivedObject,
                and_(
                    ClaimVersion.vault_id == DerivedObject.vault_id,
                    ClaimVersion.derived_object_id == DerivedObject.id,
                ),
            )
            .where(
                ClaimVersion.vault_id == vault_id,
                ClaimVersion.system_to.is_(None),
                MemoryClaim.deleted_at.is_(None),
                DerivedObject.deleted_at.is_(None),
                ClaimVersion.lifecycle_state.notin_(
                    [LifecycleState.SUPERSEDED, LifecycleState.RETRACTED]
                ),
            )
            .order_by(DerivedObject.created_at.desc(), DerivedObject.id.desc())
        ).all()

        reviewable: list[InboxItem] = []
        for version, claim, derived in rows:
            evidence = self._evidence_for_target(
                vault_id=vault_id, target_id=version.derived_object_id
            )
            verdicts = self._verdicts_for_target(
                vault_id=vault_id, target_id=version.derived_object_id
            )
            state, reduced = self._effective_state(
                claim=claim,
                version=version,
                derived=derived,
                evidence=evidence,
                verdicts=verdicts,
            )
            if state not in {LifecycleState.CANDIDATE, LifecycleState.DISPUTED}:
                continue
            if reduced.current_verdict in {VerdictType.SNOOZE, VerdictType.REJECT}:
                continue
            reviewable.append(
                InboxItem(
                    memory_id=claim.id,
                    kind=claim.kind,
                    version=self._version_view(version, state, claim.data_class),
                    support_count=sum(
                        item.relation is EvidenceRelation.SUPPORTS for item in evidence
                    ),
                    counterevidence_count=sum(
                        item.relation is EvidenceRelation.CONTRADICTS for item in evidence
                    ),
                    current_verdict=reduced.current_verdict,
                    etag=make_etag(derived.id, version.version_no, derived.review_revision),
                )
            )

        page_items = reviewable[offset : offset + limit]
        next_offset = offset + len(page_items)
        next_cursor = self._encode_cursor(next_offset) if next_offset < len(reviewable) else None
        return InboxPage(items=tuple(page_items), next_cursor=next_cursor)

    def list_memories(
        self, *, vault_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> InboxPage:
        """List current memories across verdict states for durable user history."""

        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        offset = self._decode_cursor(cursor)
        rows = self._session.execute(
            select(ClaimVersion, MemoryClaim, DerivedObject)
            .join(
                MemoryClaim,
                and_(
                    ClaimVersion.vault_id == MemoryClaim.vault_id,
                    ClaimVersion.claim_id == MemoryClaim.id,
                ),
            )
            .join(
                DerivedObject,
                and_(
                    ClaimVersion.vault_id == DerivedObject.vault_id,
                    ClaimVersion.derived_object_id == DerivedObject.id,
                ),
            )
            .where(
                ClaimVersion.vault_id == vault_id,
                ClaimVersion.system_to.is_(None),
                MemoryClaim.deleted_at.is_(None),
                DerivedObject.deleted_at.is_(None),
                ClaimVersion.lifecycle_state != LifecycleState.SUPERSEDED,
            )
            .order_by(DerivedObject.created_at.desc(), DerivedObject.id.desc())
        ).all()

        items: list[InboxItem] = []
        for version, claim, derived in rows:
            evidence = self._evidence_for_target(
                vault_id=vault_id, target_id=version.derived_object_id
            )
            verdicts = self._verdicts_for_target(
                vault_id=vault_id, target_id=version.derived_object_id
            )
            state, reduced = self._effective_state(
                claim=claim,
                version=version,
                derived=derived,
                evidence=evidence,
                verdicts=verdicts,
            )
            items.append(
                InboxItem(
                    memory_id=claim.id,
                    kind=claim.kind,
                    version=self._version_view(version, state, claim.data_class),
                    support_count=sum(
                        item.relation is EvidenceRelation.SUPPORTS for item in evidence
                    ),
                    counterevidence_count=sum(
                        item.relation is EvidenceRelation.CONTRADICTS for item in evidence
                    ),
                    current_verdict=self._surface_verdict(version, reduced),
                    etag=make_etag(derived.id, version.version_no, derived.review_revision),
                )
            )

        page_items = items[offset : offset + limit]
        next_offset = offset + len(page_items)
        next_cursor = self._encode_cursor(next_offset) if next_offset < len(items) else None
        return InboxPage(items=tuple(page_items), next_cursor=next_cursor)

    @staticmethod
    def _surface_verdict(
        version: ClaimVersion, reduced: ReducedVerdict
    ) -> VerdictType | None:
        if (
            version.origin is ClaimVersionOrigin.USER_CORRECTION
            and version.origin_verdict_id is not None
        ):
            return VerdictType.CORRECT
        return reduced.current_verdict

    def record_verdict(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        verdict: VerdictType,
        expected_etag: str,
        replacement: CorrectionReplacement | None = None,
        reason: str | None = None,
    ) -> VerdictOutcome:
        reason = reason.strip() if reason is not None else None
        if verdict is VerdictType.CORRECT and replacement is None:
            raise InvalidVerdictError("A correct verdict requires a structured replacement")
        if verdict is not VerdictType.CORRECT and replacement is not None:
            raise InvalidVerdictError("replacement is only valid for a correct verdict")
        if replacement is not None:
            self._validate_replacement(replacement)

        with self._session.begin_nested():
            claim, version, derived = self._locked_current(vault_id=vault_id, memory_id=memory_id)
            self._assert_etag(expected_etag, version=version, derived=derived)
            if replacement is not None and replacement.mode.value == "life_stage_change":
                assert replacement.valid_time is not None
                transition = _utc(replacement.valid_time.valid_from)
                if transition <= _utc(version.valid_from) or (
                    version.valid_to is not None and transition >= _utc(version.valid_to)
                ):
                    raise InvalidTemporalIntervalError(
                        "A life-stage transition must fall inside the current reality interval"
                    )
            evidence = self._evidence_for_target(
                vault_id=vault_id, target_id=version.derived_object_id
            )
            existing_events = self._verdicts_for_target(
                vault_id=vault_id, target_id=version.derived_object_id
            )
            next_sequence = max((event.sequence_no for event in existing_events), default=0) + 1
            now = self._next_aggregate_time(version=version, derived=derived)
            safety = self._classify_texts(
                vault_id=vault_id,
                texts=_user_texts(
                    replacement.statement if replacement is not None else None,
                    replacement.uncertainty_text if replacement is not None else None,
                    (
                        replacement.valid_time.original_expression
                        if replacement is not None and replacement.valid_time is not None
                        else None
                    ),
                    reason,
                ),
                model_origin=False,
                at=now,
            )
            text_data_class = (
                safety.data_class
                if safety is not None
                else (
                    DataClass.SENSITIVE if replacement is not None or reason else claim.data_class
                )
            )
            verdict_data_class = _max_data_class(claim.data_class, text_data_class)
            if _DATA_CLASS_RANK[verdict_data_class] > _DATA_CLASS_RANK[claim.data_class]:
                claim.data_class = verdict_data_class
                claim.updated_at = now
                derived.data_class = verdict_data_class
            event = UserVerdict(
                id=uuid.uuid4(),
                vault_id=vault_id,
                target_derived_object_id=version.derived_object_id,
                sequence_no=next_sequence,
                verdict=verdict,
                correction_text=replacement.statement if replacement is not None else None,
                replacement_payload=(
                    self._replacement_payload(replacement) if replacement is not None else None
                ),
                reason_optional=reason,
                created_at=now,
                created_by=TechnicalActor.USER,
                data_class=verdict_data_class,
            )

            preliminary = reduce_verdicts(
                version.initial_lifecycle_state,
                [*existing_events, event],
                can_activate=False,
            )
            can_activate = activation_allowed(
                kind=claim.kind,
                data_class=derived.data_class,
                epistemic_type=version.epistemic_type,
                attribution=version.attribution,
                evidence=evidence,
                last_decisive_verdict=preliminary.last_decisive_verdict,
                requires_explicit_confirmation=(
                    version.suppressed_by_derived_object_id is not None
                    and version.suppression_override_verdict_id is None
                ),
            )
            reduced = reduce_verdicts(
                version.initial_lifecycle_state,
                [*existing_events, event],
                can_activate=can_activate,
            )
            resulting_state = policy_lifecycle(
                reduced_state=reduced.lifecycle_state,
                activation_is_allowed=can_activate,
                evidence=evidence,
            )

            old_review_revision = derived.review_revision
            self._bump_review_revision(
                derived=derived, expected_revision=old_review_revision, changed_at=now
            )
            self._session.add(event)
            self._session.flush([event])
            if verdict in {VerdictType.REJECT, VerdictType.CORRECT, VerdictType.RETRACT}:
                self._append_suppression(
                    claim=claim,
                    version=version,
                    event=event,
                    created_at=now,
                )

            if verdict is VerdictType.CORRECT:
                assert replacement is not None
                (
                    outcome_memory_id,
                    current_id,
                    current_version_no,
                    current_state,
                ) = self._correct_version(
                    vault_id=vault_id,
                    claim=claim,
                    version=version,
                    derived=derived,
                    event=event,
                    replacement=replacement,
                    safety=safety,
                    changed_at=now,
                    existing_evidence=evidence,
                )
                current_etag = make_etag(current_id, current_version_no, 0)
            else:
                self._set_projection_state(version, resulting_state)
                self._session.flush()
                current_id = version.derived_object_id
                current_version_no = version.version_no
                current_state = resulting_state
                current_etag = make_etag(current_id, current_version_no, old_review_revision + 1)
                outcome_memory_id = memory_id

            outcome = VerdictOutcome(
                verdict_id=event.id,
                memory_id=outcome_memory_id,
                target_derived_object_id=event.target_derived_object_id,
                current_derived_object_id=current_id,
                state=current_state,
                version_no=current_version_no,
                etag=current_etag,
            )
        return outcome

    def add_evidence(
        self,
        *,
        vault_id: uuid.UUID,
        target_derived_object_id: uuid.UUID,
        anchor: EvidenceAnchor,
        expected_etag: str,
    ) -> MemoryDetail:
        _validate_anchor(anchor)
        with self._session.begin_nested():
            claim, version, derived = self._version_target(
                vault_id=vault_id, target_id=target_derived_object_id, for_update=True
            )
            self._assert_etag(expected_etag, version=version, derived=derived)
            now = self._next_aggregate_time(version=version, derived=derived)
            verified = self._verify_evidence_anchor(vault_id=vault_id, anchor=anchor, at=now)
            effective_data_class = _max_data_class(
                claim.data_class, derived.data_class, verified.source_data_class
            )
            authorization = self._require_authorization(
                vault_id=vault_id,
                purpose=AuthorizationPurpose.MEMORY_CREATE,
                data_class=effective_data_class,
                policy_epoch=max(version.authorization_policy_epoch, verified.policy_epoch),
                source_generation=max(
                    version.authorization_source_generation, verified.source_generation
                ),
                at=now,
            )
            new_link = self._new_evidence(
                vault_id=vault_id,
                target_derived_object_id=target_derived_object_id,
                anchor=verified,
                data_class=effective_data_class,
                created_at=now,
            )
            old_review_revision = derived.review_revision
            self._bump_review_revision(
                derived=derived, expected_revision=old_review_revision, changed_at=now
            )
            claim.data_class = effective_data_class
            claim.updated_at = now
            derived.data_class = effective_data_class
            version.authorization_snapshot_id = authorization.snapshot_id
            version.authorization_policy_epoch = authorization.policy_epoch
            version.authorization_source_generation = authorization.source_generation
            self._session.add(new_link)
            self._session.flush()
            evidence = self._evidence_for_target(
                vault_id=vault_id, target_id=target_derived_object_id
            )
            verdicts = self._verdicts_for_target(
                vault_id=vault_id, target_id=target_derived_object_id
            )
            state, _ = self._effective_state(
                claim=claim,
                version=version,
                derived=derived,
                evidence=evidence,
                verdicts=verdicts,
            )
            self._set_projection_state(version, state)
            self._session.flush()
        return self.get_detail(vault_id=vault_id, memory_id=claim.id)

    def remove_evidence(
        self,
        *,
        vault_id: uuid.UUID,
        evidence_id: uuid.UUID,
        expected_etag: str,
    ) -> MemoryDetail:
        """Immediately tombstone an evidence link and deterministically re-evaluate."""

        with self._session.begin_nested():
            link = self._session.scalar(
                select(EvidenceLink)
                .where(
                    EvidenceLink.vault_id == vault_id,
                    EvidenceLink.id == evidence_id,
                    EvidenceLink.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if link is None:
                raise EvidenceNotFoundError("Evidence was not found")
            claim, version, derived = self._version_target(
                vault_id=vault_id,
                target_id=link.target_derived_object_id,
                for_update=True,
            )
            self._assert_etag(expected_etag, version=version, derived=derived)
            now = self._next_aggregate_time(version=version, derived=derived)
            old_review_revision = derived.review_revision
            self._bump_review_revision(
                derived=derived, expected_revision=old_review_revision, changed_at=now
            )
            link.deleted_at = now
            self._session.flush()
            evidence = self._evidence_for_target(
                vault_id=vault_id, target_id=version.derived_object_id
            )
            verdicts = self._verdicts_for_target(
                vault_id=vault_id, target_id=version.derived_object_id
            )
            state, _ = self._effective_state(
                claim=claim,
                version=version,
                derived=derived,
                evidence=evidence,
                verdicts=verdicts,
            )
            self._set_projection_state(version, state)
            self._session.flush()
        return self.get_detail(vault_id=vault_id, memory_id=claim.id)

    def invalidate_source_evidence(
        self, *, vault_id: uuid.UUID, change: SourceStateChange
    ) -> tuple[MemoryDetail, ...]:
        """Idempotently close evidence invalidated by an authoritative Source event."""

        if change.status is SourceEvidenceStatus.LIVE:
            raise InvalidEvidenceError("A live Source status cannot invalidate evidence")
        if not any(
            (
                change.source_document_id,
                change.source_revision_id,
                change.source_fragment_id,
            )
        ):
            raise InvalidEvidenceError(
                "Source invalidation requires a document, revision, or fragment"
            )
        _require_aware("source_change.occurred_at", change.occurred_at)
        command_time = self._now()
        if _utc(change.occurred_at) > command_time:
            raise InvalidEvidenceError("Source invalidation cannot occur in the future")

        filters = [
            EvidenceLink.vault_id == vault_id,
            EvidenceLink.deleted_at.is_(None),
        ]
        if change.source_document_id is not None:
            filters.append(EvidenceLink.source_document_id == change.source_document_id)
        if change.source_revision_id is not None:
            filters.append(EvidenceLink.source_revision_id == change.source_revision_id)
        if change.source_fragment_id is not None:
            filters.append(EvidenceLink.source_fragment_id == change.source_fragment_id)

        memory_ids: list[uuid.UUID] = []
        with self._session.begin_nested():
            links = list(
                self._session.scalars(select(EvidenceLink).where(*filters).with_for_update())
            )
            links_by_target: dict[uuid.UUID, list[EvidenceLink]] = {}
            for link in links:
                links_by_target.setdefault(link.target_derived_object_id, []).append(link)
            for target_id, target_links in links_by_target.items():
                claim, version, derived = self._version_target(
                    vault_id=vault_id, target_id=target_id, for_update=True
                )
                changed_at = max(
                    self._next_aggregate_time(version=version, derived=derived),
                    _utc(change.occurred_at),
                )
                self._bump_review_revision(
                    derived=derived,
                    expected_revision=derived.review_revision,
                    changed_at=changed_at,
                )
                for link in target_links:
                    link.deleted_at = changed_at
                    link.invalidated_reason = change.status
                self._session.flush()
                evidence = self._evidence_for_target(
                    vault_id=vault_id, target_id=version.derived_object_id
                )
                verdicts = self._verdicts_for_target(
                    vault_id=vault_id, target_id=version.derived_object_id
                )
                state, _ = self._effective_state(
                    claim=claim,
                    version=version,
                    derived=derived,
                    evidence=evidence,
                    verdicts=verdicts,
                )
                self._set_projection_state(version, state)
                memory_ids.append(claim.id)
            self._session.flush()
        return tuple(
            self.get_detail(vault_id=vault_id, memory_id=memory_id) for memory_id in memory_ids
        )

    def _now(self) -> datetime:
        value = self._clock()
        _require_aware("clock", value)
        return _utc(value)

    def _classify_texts(
        self,
        *,
        vault_id: uuid.UUID,
        texts: tuple[str, ...],
        model_origin: bool,
        at: datetime,
    ) -> SafetyAssessment | None:
        high_risk = any(contains_clinical_language(text) for text in texts)
        if self._safety_classifier is None:
            if model_origin or high_risk:
                raise PolicyViolationError(
                    "Model-origin and high-risk memory text requires independent Safety review"
                )
            return None
        try:
            assessment = self._safety_classifier.classify(
                session=self._session,
                vault_id=vault_id,
                texts=texts,
                at=at,
            )
        except Exception as exc:
            if model_origin or high_risk:
                raise PolicyViolationError("Independent Safety review is unavailable") from exc
            return None
        if assessment.vault_id != vault_id or assessment.decision is not SafetyDecision.ALLOW:
            raise PolicyViolationError("Independent Safety review did not authorize this memory")
        return assessment

    def _verify_subject(
        self,
        *,
        vault_id: uuid.UUID,
        subject_entity_id: uuid.UUID | None,
        at: datetime,
    ) -> VerifiedSubjectEntity | None:
        if subject_entity_id is None:
            return None
        if self._subject_entity_verifier is None:
            raise PolicyViolationError("subject_entity_id requires a vault-bound Entity verifier")
        try:
            verified = self._subject_entity_verifier.verify(
                session=self._session,
                vault_id=vault_id,
                entity_id=subject_entity_id,
                at=at,
            )
        except Exception as exc:
            raise PolicyViolationError("Subject entity could not be verified") from exc
        if verified.vault_id != vault_id or verified.entity_id != subject_entity_id:
            raise PolicyViolationError("Subject entity is not valid in this vault")
        return verified

    def _verify_evidence_anchor(
        self, *, vault_id: uuid.UUID, anchor: EvidenceAnchor, at: datetime
    ) -> VerifiedEvidenceAnchor:
        if self._evidence_source_verifier is None:
            raise EvidenceSourceUnavailableError(
                "Evidence requires an authoritative Source verifier"
            )
        try:
            verified = self._evidence_source_verifier.verify(
                session=self._session,
                vault_id=vault_id,
                anchor=anchor,
                purpose=AuthorizationPurpose.MEMORY_CREATE,
                at=at,
            )
        except (InvalidEvidenceError, EvidenceSourceUnavailableError):
            raise
        except Exception as exc:
            raise EvidenceSourceUnavailableError(
                "Authoritative Source verification failed"
            ) from exc
        if (
            verified.vault_id != vault_id
            or verified.source_fragment_id != anchor.source_fragment_id
            or verified.relation is not anchor.relation
            or verified.quote_start != anchor.quote_start
            or verified.quote_end != anchor.quote_end
            or verified.quote_hash != anchor.quote_hash
            or verified.extractor_reason is not anchor.extractor_reason
            or verified.created_by is not anchor.created_by
        ):
            raise InvalidEvidenceError("Source verification did not match the proposed anchor")
        if (
            _SHA256_RE.fullmatch(verified.quote_hash) is None
            or _SHA256_RE.fullmatch(verified.source_content_fingerprint) is None
        ):
            raise InvalidEvidenceError("Source returned a non-SHA-256 evidence fingerprint")
        if verified.quote_start < 0 or verified.quote_end <= verified.quote_start:
            raise InvalidEvidenceError("Source returned an invalid evidence span")
        _require_aware("verified.source_recorded_at", verified.source_recorded_at)
        _require_aware("verified.verified_at", verified.verified_at)
        if _utc(verified.source_recorded_at) > at or _utc(verified.verified_at) > at:
            raise InvalidEvidenceError(
                "Source evidence cannot be recorded or verified in the future"
            )
        if verified.policy_epoch < 0 or verified.source_generation < 0:
            raise InvalidEvidenceError("Source authorization fences must be non-negative")
        return verified

    def _require_authorization(
        self,
        *,
        vault_id: uuid.UUID,
        purpose: AuthorizationPurpose,
        data_class: DataClass,
        policy_epoch: int,
        source_generation: int,
        at: datetime,
    ) -> AuthorizationSnapshot:
        if self._authorization_verifier is None:
            raise AuthorizationUnavailableError(
                "Memory creation requires a vault/purpose authorization snapshot"
            )
        try:
            snapshot = self._authorization_verifier.authorize(
                session=self._session,
                vault_id=vault_id,
                purpose=purpose,
                data_class=data_class,
                policy_epoch=policy_epoch,
                source_generation=source_generation,
                at=at,
            )
        except Exception as exc:
            raise AuthorizationUnavailableError("Memory authorization is unavailable") from exc
        if (
            snapshot.vault_id != vault_id
            or snapshot.purpose is not purpose
            or snapshot.data_class is not data_class
            or snapshot.policy_epoch != policy_epoch
            or snapshot.source_generation != source_generation
            or not snapshot.allows_read
        ):
            raise AuthorizationUnavailableError("Memory authorization snapshot is invalid")
        return snapshot

    def _representation_authorization(
        self,
        *,
        vault_id: uuid.UUID,
        purpose: AuthorizationPurpose,
        data_class: DataClass,
        policy_epoch: int,
        source_generation: int,
        at: datetime,
    ) -> AuthorizationSnapshot | None:
        if self._authorization_verifier is None:
            return None
        try:
            snapshot = self._authorization_verifier.authorize(
                session=self._session,
                vault_id=vault_id,
                purpose=purpose,
                data_class=data_class,
                policy_epoch=policy_epoch,
                source_generation=source_generation,
                at=at,
            )
        except Exception:
            return None
        if (
            snapshot.vault_id != vault_id
            or snapshot.purpose is not purpose
            or snapshot.data_class is not data_class
            or snapshot.policy_epoch != policy_epoch
            or snapshot.source_generation != source_generation
            or not snapshot.allows_read
        ):
            return None
        return snapshot

    def _suppression_for_fingerprint(
        self, *, vault_id: uuid.UUID, normalized_fingerprint: str
    ) -> MemorySuppression | None:
        return self._session.scalar(
            select(MemorySuppression)
            .where(
                MemorySuppression.vault_id == vault_id,
                MemorySuppression.normalized_fingerprint == normalized_fingerprint,
            )
            .order_by(MemorySuppression.created_at.desc(), MemorySuppression.id.desc())
            .limit(1)
        )

    def _next_system_time(self, *lower_bounds: datetime) -> datetime:
        now = self._now()
        lower = max(_utc(value) for value in lower_bounds)
        return now if now > lower else lower + timedelta(microseconds=1)

    def _next_aggregate_time(self, *, version: ClaimVersion, derived: DerivedObject) -> datetime:
        bounds = [version.system_from, derived.updated_at]
        last_verdict = self._session.scalar(
            select(func.max(UserVerdict.created_at)).where(
                UserVerdict.vault_id == version.vault_id,
                UserVerdict.target_derived_object_id == version.derived_object_id,
            )
        )
        last_evidence_created = self._session.scalar(
            select(func.max(EvidenceLink.created_at)).where(
                EvidenceLink.vault_id == version.vault_id,
                EvidenceLink.target_derived_object_id == version.derived_object_id,
            )
        )
        last_evidence_deleted = self._session.scalar(
            select(func.max(EvidenceLink.deleted_at)).where(
                EvidenceLink.vault_id == version.vault_id,
                EvidenceLink.target_derived_object_id == version.derived_object_id,
            )
        )
        bounds.extend(
            value
            for value in (last_verdict, last_evidence_created, last_evidence_deleted)
            if value is not None
        )
        return self._next_system_time(*bounds)

    def _claim(self, *, vault_id: uuid.UUID, memory_id: uuid.UUID) -> MemoryClaim:
        claim = self._session.scalar(
            select(MemoryClaim).where(
                MemoryClaim.vault_id == vault_id,
                MemoryClaim.id == memory_id,
                MemoryClaim.deleted_at.is_(None),
            )
        )
        if claim is None:
            raise MemoryNotFoundError("Memory was not found")
        return claim

    def _current_version(
        self, *, vault_id: uuid.UUID, memory_id: uuid.UUID
    ) -> tuple[ClaimVersion, DerivedObject]:
        row = self._session.execute(
            select(ClaimVersion, DerivedObject)
            .join(
                DerivedObject,
                and_(
                    ClaimVersion.vault_id == DerivedObject.vault_id,
                    ClaimVersion.derived_object_id == DerivedObject.id,
                ),
            )
            .where(
                ClaimVersion.vault_id == vault_id,
                ClaimVersion.claim_id == memory_id,
                ClaimVersion.system_to.is_(None),
                DerivedObject.deleted_at.is_(None),
            )
        ).one_or_none()
        if row is None:
            raise MemoryNotFoundError("Memory was not found")
        return row[0], row[1]

    def _locked_current(
        self, *, vault_id: uuid.UUID, memory_id: uuid.UUID
    ) -> tuple[MemoryClaim, ClaimVersion, DerivedObject]:
        claim = self._session.scalar(
            select(MemoryClaim)
            .where(
                MemoryClaim.vault_id == vault_id,
                MemoryClaim.id == memory_id,
                MemoryClaim.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if claim is None:
            raise MemoryNotFoundError("Memory was not found")
        version, derived = self._current_version(vault_id=vault_id, memory_id=memory_id)
        return claim, version, derived

    def _as_of_version(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        real_at: datetime,
        system_at: datetime,
    ) -> tuple[ClaimVersion, DerivedObject]:
        row = self._session.execute(
            select(ClaimVersion, DerivedObject)
            .join(
                DerivedObject,
                and_(
                    ClaimVersion.vault_id == DerivedObject.vault_id,
                    ClaimVersion.derived_object_id == DerivedObject.id,
                ),
            )
            .where(
                ClaimVersion.vault_id == vault_id,
                ClaimVersion.claim_id == memory_id,
                ClaimVersion.valid_from <= real_at,
                or_(ClaimVersion.valid_to.is_(None), real_at < ClaimVersion.valid_to),
                ClaimVersion.system_from <= system_at,
                or_(ClaimVersion.system_to.is_(None), system_at < ClaimVersion.system_to),
                DerivedObject.deleted_at.is_(None),
            )
            .order_by(ClaimVersion.version_no.desc())
            .limit(1)
        ).one_or_none()
        if row is None:
            raise MemoryNotFoundError("No memory version exists at both requested times")
        return row[0], row[1]

    def _history(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        system_at: datetime | None,
    ) -> list[tuple[ClaimVersion, DerivedObject]]:
        statement = (
            select(ClaimVersion, DerivedObject)
            .join(
                DerivedObject,
                and_(
                    ClaimVersion.vault_id == DerivedObject.vault_id,
                    ClaimVersion.derived_object_id == DerivedObject.id,
                ),
            )
            .where(
                ClaimVersion.vault_id == vault_id,
                ClaimVersion.claim_id == memory_id,
                DerivedObject.deleted_at.is_(None),
            )
            .order_by(ClaimVersion.version_no)
        )
        if system_at is not None:
            statement = statement.where(ClaimVersion.system_from <= system_at)
        return [(row[0], row[1]) for row in self._session.execute(statement).all()]

    def _version_target(
        self,
        *,
        vault_id: uuid.UUID,
        target_id: uuid.UUID,
        for_update: bool,
    ) -> tuple[MemoryClaim, ClaimVersion, DerivedObject]:
        statement = (
            select(MemoryClaim, ClaimVersion, DerivedObject)
            .join(
                ClaimVersion,
                and_(
                    MemoryClaim.vault_id == ClaimVersion.vault_id,
                    MemoryClaim.id == ClaimVersion.claim_id,
                ),
            )
            .join(
                DerivedObject,
                and_(
                    ClaimVersion.vault_id == DerivedObject.vault_id,
                    ClaimVersion.derived_object_id == DerivedObject.id,
                ),
            )
            .where(
                MemoryClaim.vault_id == vault_id,
                ClaimVersion.derived_object_id == target_id,
                MemoryClaim.deleted_at.is_(None),
                DerivedObject.deleted_at.is_(None),
            )
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.execute(statement).one_or_none()
        if row is None:
            raise MemoryNotFoundError("Memory version was not found")
        return row[0], row[1], row[2]

    def _evidence_for_target(
        self,
        *,
        vault_id: uuid.UUID,
        target_id: uuid.UUID,
        system_at: datetime | None = None,
    ) -> list[EvidenceLink]:
        statement = select(EvidenceLink).where(
            EvidenceLink.vault_id == vault_id,
            EvidenceLink.target_derived_object_id == target_id,
        )
        if system_at is None:
            statement = statement.where(EvidenceLink.deleted_at.is_(None))
        else:
            statement = statement.where(
                EvidenceLink.created_at <= system_at,
                or_(EvidenceLink.deleted_at.is_(None), system_at < EvidenceLink.deleted_at),
            )
        evidence = list(
            self._session.scalars(statement.order_by(EvidenceLink.created_at, EvidenceLink.id))
        )
        if system_at is not None:
            return evidence
        return self._currently_authoritative_evidence(vault_id=vault_id, evidence=evidence)

    def _currently_authoritative_evidence(
        self, *, vault_id: uuid.UUID, evidence: Sequence[EvidenceLink]
    ) -> list[EvidenceLink]:
        if not evidence or self._evidence_source_verifier is None:
            return []
        references = tuple(
            EvidenceSourceReference(
                evidence_id=item.id,
                source_document_id=item.source_document_id,
                source_revision_id=item.source_revision_id,
                source_fragment_id=item.source_fragment_id,
                quote_start=item.quote_start,
                quote_end=item.quote_end,
                quote_hash=item.quote_hash,
                authorization_snapshot_id=item.authorization_snapshot_id,
                policy_epoch=item.source_policy_epoch,
                source_generation=item.source_generation,
            )
            for item in evidence
        )
        checked_at = self._now()
        try:
            states = self._evidence_source_verifier.resolve_current(
                session=self._session,
                vault_id=vault_id,
                references=references,
                purpose=AuthorizationPurpose.MEMORY_REVIEW,
                at=checked_at,
            )
        except Exception:
            return []
        by_id: dict[uuid.UUID, EvidenceSourceState] = {}
        for resolved_state in states:
            if resolved_state.evidence_id in by_id:
                return []
            by_id[resolved_state.evidence_id] = resolved_state
        result: list[EvidenceLink] = []
        for item in evidence:
            current_state = by_id.get(item.id)
            if current_state is None or current_state.status is not SourceEvidenceStatus.LIVE:
                continue
            try:
                _require_aware("source_state.checked_at", current_state.checked_at)
            except InvalidTemporalIntervalError:
                continue
            if (
                _utc(current_state.checked_at) > checked_at
                or current_state.authorization_snapshot_id != item.authorization_snapshot_id
                or current_state.policy_epoch != item.source_policy_epoch
                or current_state.source_generation != item.source_generation
                or _DATA_CLASS_RANK[current_state.data_class]
                > _DATA_CLASS_RANK[item.source_data_class]
            ):
                continue
            result.append(item)
        return result

    def _verdicts_for_target(
        self,
        *,
        vault_id: uuid.UUID,
        target_id: uuid.UUID,
        system_at: datetime | None = None,
    ) -> list[UserVerdict]:
        statement = select(UserVerdict).where(
            UserVerdict.vault_id == vault_id,
            UserVerdict.target_derived_object_id == target_id,
        )
        if system_at is not None:
            statement = statement.where(UserVerdict.created_at <= system_at)
        return list(self._session.scalars(statement.order_by(UserVerdict.sequence_no)))

    def _verdicts_for_targets(
        self,
        *,
        vault_id: uuid.UUID,
        target_ids: Sequence[uuid.UUID],
        system_at: datetime | None,
    ) -> list[UserVerdict]:
        if not target_ids:
            return []
        statement = select(UserVerdict).where(
            UserVerdict.vault_id == vault_id,
            UserVerdict.target_derived_object_id.in_(target_ids),
        )
        if system_at is not None:
            statement = statement.where(UserVerdict.created_at <= system_at)
        return list(
            self._session.scalars(
                statement.order_by(
                    UserVerdict.created_at,
                    UserVerdict.target_derived_object_id,
                    UserVerdict.sequence_no,
                )
            )
        )

    def _claim_governance_event(
        self, *, vault_id: uuid.UUID, claim_id: uuid.UUID
    ) -> UserVerdict | None:
        return self._session.scalar(
            select(UserVerdict)
            .join(
                ClaimVersion,
                and_(
                    UserVerdict.vault_id == ClaimVersion.vault_id,
                    UserVerdict.target_derived_object_id == ClaimVersion.derived_object_id,
                ),
            )
            .where(
                UserVerdict.vault_id == vault_id,
                ClaimVersion.claim_id == claim_id,
            )
            .order_by(UserVerdict.created_at.desc(), UserVerdict.id.desc())
            .limit(1)
        )

    def _historical_effective_state(
        self,
        *,
        claim: MemoryClaim,
        version: ClaimVersion,
        derived: DerivedObject,
        system_at: datetime,
    ) -> tuple[LifecycleState, ReducedVerdict]:
        all_evidence = list(
            self._session.scalars(
                select(EvidenceLink)
                .where(
                    EvidenceLink.vault_id == version.vault_id,
                    EvidenceLink.target_derived_object_id == version.derived_object_id,
                    EvidenceLink.created_at <= system_at,
                )
                .order_by(EvidenceLink.created_at, EvidenceLink.id)
            )
        )
        verdicts = self._verdicts_for_target(
            vault_id=version.vault_id,
            target_id=version.derived_object_id,
            system_at=system_at,
        )
        event_times = {
            _utc(item.created_at) for item in all_evidence if _utc(item.created_at) <= system_at
        }
        event_times.update(
            _utc(item.deleted_at)
            for item in all_evidence
            if item.deleted_at is not None and _utc(item.deleted_at) <= system_at
        )
        event_times.update(_utc(item.created_at) for item in verdicts)
        state = version.initial_lifecycle_state
        reduction = reduce_verdicts(state, (), can_activate=False)

        for event_time in sorted(event_times):
            effective_evidence = [
                _HistoricalEvidence(
                    source_fragment_id=item.source_fragment_id,
                    source_revision_id=item.source_revision_id,
                    source_content_fingerprint=item.source_content_fingerprint,
                    relation=item.relation,
                    source_recorded_at=item.source_recorded_at,
                )
                for item in all_evidence
                if _utc(item.created_at) <= event_time
                and (item.deleted_at is None or event_time < _utc(item.deleted_at))
            ]
            effective_verdicts = [item for item in verdicts if _utc(item.created_at) <= event_time]
            computed, reduction = self._effective_state(
                claim=claim,
                version=version,
                derived=derived,
                evidence=effective_evidence,
                verdicts=effective_verdicts,
                respect_current_projection=False,
            )
            if computed is LifecycleState.CANDIDATE and state in {
                LifecycleState.ACTIVE,
                LifecycleState.DISPUTED,
            }:
                state = LifecycleState.DISPUTED
            else:
                state = computed
        return state, reduction

    def _effective_state(
        self,
        *,
        claim: MemoryClaim,
        version: ClaimVersion,
        derived: DerivedObject,
        evidence: Sequence[EvidenceForPolicy],
        verdicts: Sequence[UserVerdict],
        respect_current_projection: bool = True,
    ) -> tuple[LifecycleState, ReducedVerdict]:
        preliminary = reduce_verdicts(version.initial_lifecycle_state, verdicts, can_activate=False)
        can_activate = activation_allowed(
            kind=claim.kind,
            data_class=claim.data_class,
            epistemic_type=version.epistemic_type,
            attribution=version.attribution,
            evidence=evidence,
            last_decisive_verdict=preliminary.last_decisive_verdict,
            requires_explicit_confirmation=(
                version.suppressed_by_derived_object_id is not None
                and version.suppression_override_verdict_id is None
            ),
        )
        reduced = reduce_verdicts(
            version.initial_lifecycle_state, verdicts, can_activate=can_activate
        )
        state = policy_lifecycle(
            reduced_state=reduced.lifecycle_state,
            activation_is_allowed=can_activate,
            evidence=evidence,
        )
        if (
            respect_current_projection
            and state is LifecycleState.CANDIDATE
            and version.lifecycle_state in {LifecycleState.ACTIVE, LifecycleState.DISPUTED}
        ):
            state = LifecycleState.DISPUTED
        return state, reduced

    def _assert_etag(
        self,
        expected_etag: str,
        *,
        version: ClaimVersion,
        derived: DerivedObject,
    ) -> None:
        if not expected_etag.startswith('"cv:'):
            raise RevisionConflictError("Historical snapshot tokens are read-only")
        actual = make_etag(derived.id, version.version_no, derived.review_revision)
        if expected_etag != actual:
            raise RevisionConflictError("The memory changed; refresh before applying a verdict")

    def _bump_review_revision(
        self,
        *,
        derived: DerivedObject,
        expected_revision: int,
        changed_at: datetime,
    ) -> None:
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(DerivedObject)
                .where(
                    DerivedObject.vault_id == derived.vault_id,
                    DerivedObject.id == derived.id,
                    DerivedObject.review_revision == expected_revision,
                    DerivedObject.deleted_at.is_(None),
                )
                .values(
                    review_revision=DerivedObject.review_revision + 1,
                    updated_at=changed_at,
                )
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount != 1:
            raise RevisionConflictError("The memory changed concurrently")
        self._session.expire(derived, ["review_revision", "updated_at"])

    def _set_projection_state(self, version: ClaimVersion, state: LifecycleState) -> None:
        current = version.lifecycle_state
        if current is state:
            return
        if state is LifecycleState.CANDIDATE and current in {
            LifecycleState.ACTIVE,
            LifecycleState.DISPUTED,
        }:
            if current is LifecycleState.ACTIVE:
                version.lifecycle_state = transition_lifecycle(current, LifecycleState.DISPUTED)
            return
        if state is LifecycleState.SUPERSEDED:
            version.lifecycle_state = replacement_transition(current)
            return
        version.lifecycle_state = transition_lifecycle(current, state)

    @staticmethod
    def _validate_replacement(replacement: CorrectionReplacement) -> None:
        if not replacement.statement.strip():
            raise InvalidVerdictError("A correction replacement statement cannot be blank")
        if not isinstance(replacement.mode, CorrectionMode):
            raise InvalidVerdictError("Correction mode must be a closed technical enum")
        if replacement.mode is CorrectionMode.LIFE_STAGE_CHANGE and replacement.valid_time is None:
            raise InvalidTemporalIntervalError(
                "A life-stage change requires an explicit replacement valid interval"
            )
        if replacement.valid_time is None:
            return
        valid_time = replacement.valid_time
        _validate_interval("replacement_valid", valid_time.valid_from, valid_time.valid_to)
        if valid_time.timezone is not None and (
            not valid_time.timezone.strip() or len(valid_time.timezone) > 128
        ):
            raise InvalidTemporalIntervalError("Replacement timezone is invalid")
        if (
            valid_time.precision in {ValidTimePrecision.RANGE, ValidTimePrecision.UNKNOWN}
            and not valid_time.original_expression
        ):
            raise InvalidTemporalIntervalError(
                "Fuzzy replacement time requires the original user expression"
            )

    @staticmethod
    def _replacement_payload(replacement: CorrectionReplacement) -> dict[str, Any]:
        valid_time = replacement.valid_time
        return {
            "statement": replacement.statement.strip(),
            "mode": replacement.mode.value,
            "valid_time": (
                {
                    "from": _utc(valid_time.valid_from).isoformat(),
                    "to": (
                        _utc(valid_time.valid_to).isoformat()
                        if valid_time.valid_to is not None
                        else None
                    ),
                    "precision": valid_time.precision.value,
                    "original_expression": valid_time.original_expression,
                    "timezone": valid_time.timezone,
                }
                if valid_time is not None
                else None
            ),
            "uncertainty_text": replacement.uncertainty_text,
            "confidence_band": replacement.confidence_band.value,
        }

    @staticmethod
    def _replacement_from_payload(
        payload: Mapping[str, Any] | None,
    ) -> CorrectionReplacement | None:
        if payload is None:
            return None
        valid_payload = payload.get("valid_time")
        valid_time = None
        if isinstance(valid_payload, Mapping):
            raw_from = valid_payload.get("from")
            raw_to = valid_payload.get("to")
            if not isinstance(raw_from, str):
                return None
            valid_time = ReplacementValidTime(
                valid_from=datetime.fromisoformat(raw_from),
                valid_to=(datetime.fromisoformat(raw_to) if isinstance(raw_to, str) else None),
                precision=ValidTimePrecision(str(valid_payload.get("precision"))),
                original_expression=(
                    str(valid_payload["original_expression"])
                    if valid_payload.get("original_expression") is not None
                    else None
                ),
                timezone=(
                    str(valid_payload["timezone"])
                    if valid_payload.get("timezone") is not None
                    else None
                ),
            )
        return CorrectionReplacement(
            statement=str(payload["statement"]),
            mode=CorrectionMode(str(payload["mode"])),
            valid_time=valid_time,
            uncertainty_text=(
                str(payload["uncertainty_text"])
                if payload.get("uncertainty_text") is not None
                else None
            ),
            confidence_band=ConfidenceBand(str(payload["confidence_band"])),
        )

    def _append_suppression(
        self,
        *,
        claim: MemoryClaim,
        version: ClaimVersion,
        event: UserVerdict,
        created_at: datetime,
    ) -> None:
        suppression = MemorySuppression(
            id=uuid.uuid4(),
            vault_id=claim.vault_id,
            normalized_fingerprint=version.normalized_fingerprint,
            suppression_lineage_id=claim.suppression_lineage_id,
            source_derived_object_id=version.derived_object_id,
            source_verdict_id=event.id,
            verdict=event.verdict,
            created_at=created_at,
            created_by=TechnicalActor.USER,
            data_class=claim.data_class,
        )
        self._session.add(suppression)
        self._session.flush([suppression])

    def _correct_version(
        self,
        *,
        vault_id: uuid.UUID,
        claim: MemoryClaim,
        version: ClaimVersion,
        derived: DerivedObject,
        event: UserVerdict,
        replacement: CorrectionReplacement,
        safety: SafetyAssessment | None,
        changed_at: datetime,
        existing_evidence: Sequence[EvidenceLink],
    ) -> tuple[uuid.UUID, uuid.UUID, int, LifecycleState]:
        if self._correction_source_recorder is None:
            raise CorrectionSourceUnavailableError(
                "Correction requires an injected Source correction recorder"
            )
        statement = replacement.statement.strip()
        fallback_class = DataClass.SENSITIVE
        requested_data_class = _max_data_class(
            claim.data_class,
            safety.data_class if safety is not None else fallback_class,
        )
        source_anchor = self._correction_source_recorder.record_correction(
            session=self._session,
            vault_id=vault_id,
            memory_id=claim.id,
            correction_text=statement,
            data_class=requested_data_class,
            recorded_at=changed_at,
        )
        raw_anchor = EvidenceAnchor(
            source_fragment_id=source_anchor.source_fragment_id,
            relation=EvidenceRelation.SUPPORTS,
            quote_hash=hashlib.sha256(statement.encode("utf-8")).hexdigest(),
            extractor_reason=EvidenceExtractionReason.USER_CORRECTION,
            quote_start=0,
            quote_end=len(statement),
            strength_band=EvidenceStrength.STRONG,
            created_by=TechnicalActor.USER,
        )
        # The correction Source is inserted before it is verified. PostgreSQL may
        # assign its persisted ``created_at`` a few milliseconds after the command
        # timestamp captured above, so verify against a fresh boundary time rather
        # than incorrectly classifying the new Source as future evidence.
        verification_at = max(changed_at, self._now())
        verified_anchor = self._verify_evidence_anchor(
            vault_id=vault_id, anchor=raw_anchor, at=verification_at
        )
        new_data_class = _max_data_class(requested_data_class, verified_anchor.source_data_class)
        validate_persistable_memory(
            canonical_text=statement,
            structured_payload={},
            epistemic_type=EpistemicType.USER_AUTHORED,
            attribution=Attribution.SELF_REPORT,
            data_class=new_data_class,
        )
        authorization = self._require_authorization(
            vault_id=vault_id,
            purpose=AuthorizationPurpose.MEMORY_CREATE,
            data_class=new_data_class,
            policy_epoch=max(version.authorization_policy_epoch, verified_anchor.policy_epoch),
            source_generation=max(
                version.authorization_source_generation, verified_anchor.source_generation
            ),
            at=verification_at,
        )

        version.lifecycle_state = replacement_transition(version.lifecycle_state)
        version.system_to = changed_at
        self._session.flush([version, event])
        claim.data_class = new_data_class
        claim.updated_at = changed_at

        if replacement.mode is CorrectionMode.LIFE_STAGE_CHANGE:
            assert replacement.valid_time is not None
            return self._create_life_stage_successor(
                vault_id=vault_id,
                claim=claim,
                version=version,
                event=event,
                replacement=replacement,
                verified_anchor=verified_anchor,
                authorization=authorization,
                safety=safety,
                data_class=new_data_class,
                changed_at=changed_at,
                existing_evidence=existing_evidence,
            )

        valid_time = replacement.valid_time
        valid_from = _utc(valid_time.valid_from) if valid_time is not None else version.valid_from
        valid_to = (
            _utc(valid_time.valid_to)
            if valid_time is not None and valid_time.valid_to is not None
            else (None if valid_time is not None else version.valid_to)
        )
        precision = valid_time.precision if valid_time is not None else version.valid_time_precision
        original = (
            valid_time.original_expression
            if valid_time is not None
            else version.valid_time_original
        )
        timezone = valid_time.timezone if valid_time is not None else version.valid_timezone
        new_derived_id = uuid.uuid4()
        new_version_no = version.version_no + 1
        new_derived = DerivedObject(
            id=new_derived_id,
            vault_id=vault_id,
            object_kind=DerivedObjectKind.CLAIM_VERSION,
            created_by=TechnicalActor.USER,
            data_class=new_data_class,
            review_revision=0,
            created_at=changed_at,
            updated_at=changed_at,
        )
        correction_evidence = self._new_evidence(
            vault_id=vault_id,
            target_derived_object_id=new_derived_id,
            anchor=verified_anchor,
            data_class=new_data_class,
            created_at=changed_at,
        )
        new_state = policy_lifecycle(
            reduced_state=LifecycleState.CANDIDATE,
            activation_is_allowed=activation_allowed(
                kind=claim.kind,
                data_class=new_data_class,
                epistemic_type=EpistemicType.USER_AUTHORED,
                attribution=Attribution.SELF_REPORT,
                evidence=[correction_evidence],
                last_decisive_verdict=None,
            ),
            evidence=[correction_evidence],
        )
        new_version = ClaimVersion(
            derived_object_id=new_derived_id,
            vault_id=vault_id,
            claim_id=claim.id,
            version_no=new_version_no,
            canonical_text=statement,
            structured_payload={},
            epistemic_type=EpistemicType.USER_AUTHORED,
            attribution=Attribution.SELF_REPORT,
            uncertainty_text=replacement.uncertainty_text,
            initial_lifecycle_state=new_state,
            lifecycle_state=new_state,
            valid_from=valid_from,
            valid_to=valid_to,
            valid_time_precision=precision,
            valid_time_original=original,
            valid_timezone=timezone,
            system_from=changed_at,
            system_to=None,
            confidence_band=replacement.confidence_band,
            pipeline_version="user-correction-v2",
            model_run_id=None,
            origin=ClaimVersionOrigin.USER_CORRECTION,
            correction_mode=replacement.mode,
            supersedes_derived_object_id=version.derived_object_id,
            origin_verdict_id=event.id,
            suppressed_by_derived_object_id=version.derived_object_id,
            suppression_override_verdict_id=event.id,
            normalized_fingerprint=_replacement_fingerprint(
                vault_id=vault_id, claim=claim, replacement=replacement
            ),
            authorization_snapshot_id=authorization.snapshot_id,
            authorization_policy_epoch=authorization.policy_epoch,
            authorization_source_generation=authorization.source_generation,
            safety_assessment_id=safety.assessment_id if safety is not None else None,
            safety_allows_proactive=safety.allows_proactive if safety is not None else False,
            subject_verification_id=version.subject_verification_id,
        )
        self._session.add(new_derived)
        self._session.flush([new_derived])
        self._session.add_all([new_version, correction_evidence])
        self._session.flush()
        return claim.id, new_derived_id, new_version_no, new_state

    def _create_life_stage_successor(
        self,
        *,
        vault_id: uuid.UUID,
        claim: MemoryClaim,
        version: ClaimVersion,
        event: UserVerdict,
        replacement: CorrectionReplacement,
        verified_anchor: VerifiedEvidenceAnchor,
        authorization: AuthorizationSnapshot,
        safety: SafetyAssessment | None,
        data_class: DataClass,
        changed_at: datetime,
        existing_evidence: Sequence[EvidenceLink],
    ) -> tuple[uuid.UUID, uuid.UUID, int, LifecycleState]:
        assert replacement.valid_time is not None
        transition = _utc(replacement.valid_time.valid_from)
        bounded_derived_id = uuid.uuid4()
        bounded_derived = DerivedObject(
            id=bounded_derived_id,
            vault_id=vault_id,
            object_kind=DerivedObjectKind.CLAIM_VERSION,
            created_by=TechnicalActor.USER,
            data_class=data_class,
            review_revision=0,
            created_at=changed_at,
            updated_at=changed_at,
        )
        bounded_evidence = [
            self._clone_evidence(
                item,
                target_derived_object_id=bounded_derived_id,
                data_class=data_class,
                created_at=changed_at,
            )
            for item in existing_evidence
        ]
        bounded_state = policy_lifecycle(
            reduced_state=LifecycleState.CANDIDATE,
            activation_is_allowed=activation_allowed(
                kind=claim.kind,
                data_class=data_class,
                epistemic_type=version.epistemic_type,
                attribution=version.attribution,
                evidence=bounded_evidence,
                last_decisive_verdict=None,
                requires_explicit_confirmation=True,
            ),
            evidence=bounded_evidence,
        )
        bounded_version = ClaimVersion(
            derived_object_id=bounded_derived_id,
            vault_id=vault_id,
            claim_id=claim.id,
            version_no=version.version_no + 1,
            canonical_text=version.canonical_text,
            structured_payload=dict(version.structured_payload),
            epistemic_type=version.epistemic_type,
            attribution=version.attribution,
            uncertainty_text=version.uncertainty_text,
            initial_lifecycle_state=bounded_state,
            lifecycle_state=bounded_state,
            valid_from=version.valid_from,
            valid_to=transition,
            valid_time_precision=version.valid_time_precision,
            valid_time_original=version.valid_time_original,
            valid_timezone=version.valid_timezone,
            system_from=changed_at,
            system_to=None,
            confidence_band=version.confidence_band,
            pipeline_version="user-stage-boundary-v1",
            model_run_id=None,
            origin=ClaimVersionOrigin.USER_CORRECTION,
            correction_mode=CorrectionMode.LIFE_STAGE_CHANGE,
            supersedes_derived_object_id=version.derived_object_id,
            origin_verdict_id=event.id,
            suppressed_by_derived_object_id=version.derived_object_id,
            suppression_override_verdict_id=None,
            normalized_fingerprint=version.normalized_fingerprint,
            authorization_snapshot_id=authorization.snapshot_id,
            authorization_policy_epoch=authorization.policy_epoch,
            authorization_source_generation=authorization.source_generation,
            safety_assessment_id=version.safety_assessment_id,
            safety_allows_proactive=False,
            subject_verification_id=version.subject_verification_id,
        )

        successor_claim_id = uuid.uuid4()
        successor_derived_id = uuid.uuid4()
        successor_claim = MemoryClaim(
            id=successor_claim_id,
            vault_id=vault_id,
            kind=claim.kind,
            subject_entity_id=claim.subject_entity_id,
            suppression_lineage_id=claim.suppression_lineage_id,
            created_by=TechnicalActor.USER,
            data_class=data_class,
            created_at=changed_at,
            updated_at=changed_at,
        )
        successor_derived = DerivedObject(
            id=successor_derived_id,
            vault_id=vault_id,
            object_kind=DerivedObjectKind.CLAIM_VERSION,
            created_by=TechnicalActor.USER,
            data_class=data_class,
            review_revision=0,
            created_at=changed_at,
            updated_at=changed_at,
        )
        successor_evidence = self._new_evidence(
            vault_id=vault_id,
            target_derived_object_id=successor_derived_id,
            anchor=verified_anchor,
            data_class=data_class,
            created_at=changed_at,
        )
        successor_state = policy_lifecycle(
            reduced_state=LifecycleState.CANDIDATE,
            activation_is_allowed=activation_allowed(
                kind=claim.kind,
                data_class=data_class,
                epistemic_type=EpistemicType.USER_AUTHORED,
                attribution=Attribution.SELF_REPORT,
                evidence=[successor_evidence],
                last_decisive_verdict=None,
            ),
            evidence=[successor_evidence],
        )
        successor_version = ClaimVersion(
            derived_object_id=successor_derived_id,
            vault_id=vault_id,
            claim_id=successor_claim_id,
            version_no=1,
            canonical_text=replacement.statement.strip(),
            structured_payload={},
            epistemic_type=EpistemicType.USER_AUTHORED,
            attribution=Attribution.SELF_REPORT,
            uncertainty_text=replacement.uncertainty_text,
            initial_lifecycle_state=successor_state,
            lifecycle_state=successor_state,
            valid_from=transition,
            valid_to=(
                _utc(replacement.valid_time.valid_to)
                if replacement.valid_time.valid_to is not None
                else None
            ),
            valid_time_precision=replacement.valid_time.precision,
            valid_time_original=replacement.valid_time.original_expression,
            valid_timezone=replacement.valid_time.timezone,
            system_from=changed_at,
            system_to=None,
            confidence_band=replacement.confidence_band,
            pipeline_version="user-stage-successor-v1",
            model_run_id=None,
            origin=ClaimVersionOrigin.USER_CORRECTION,
            correction_mode=CorrectionMode.LIFE_STAGE_CHANGE,
            supersedes_derived_object_id=version.derived_object_id,
            origin_verdict_id=event.id,
            suppressed_by_derived_object_id=version.derived_object_id,
            suppression_override_verdict_id=event.id,
            normalized_fingerprint=_replacement_fingerprint(
                vault_id=vault_id, claim=claim, replacement=replacement
            ),
            authorization_snapshot_id=authorization.snapshot_id,
            authorization_policy_epoch=authorization.policy_epoch,
            authorization_source_generation=authorization.source_generation,
            safety_assessment_id=safety.assessment_id if safety is not None else None,
            safety_allows_proactive=safety.allows_proactive if safety is not None else False,
            subject_verification_id=version.subject_verification_id,
        )

        self._session.add_all([bounded_derived, successor_claim, successor_derived])
        self._session.flush([bounded_derived, successor_claim, successor_derived])
        self._session.add_all(
            [bounded_version, *bounded_evidence, successor_version, successor_evidence]
        )
        self._session.flush()
        return successor_claim_id, successor_derived_id, 1, successor_state

    @staticmethod
    def _clone_evidence(
        evidence: EvidenceLink,
        *,
        target_derived_object_id: uuid.UUID,
        data_class: DataClass,
        created_at: datetime,
    ) -> EvidenceLink:
        return EvidenceLink(
            id=uuid.uuid4(),
            vault_id=evidence.vault_id,
            target_derived_object_id=target_derived_object_id,
            source_document_id=evidence.source_document_id,
            source_revision_id=evidence.source_revision_id,
            source_fragment_id=evidence.source_fragment_id,
            relation=evidence.relation,
            quote_start=evidence.quote_start,
            quote_end=evidence.quote_end,
            quote_hash=evidence.quote_hash,
            extractor_reason=evidence.extractor_reason,
            strength_band=evidence.strength_band,
            model_run_id=evidence.model_run_id,
            source_recorded_at=evidence.source_recorded_at,
            source_content_fingerprint=evidence.source_content_fingerprint,
            normalized_fingerprint=evidence.normalized_fingerprint,
            authorization_snapshot_id=evidence.authorization_snapshot_id,
            source_policy_epoch=evidence.source_policy_epoch,
            source_generation=evidence.source_generation,
            source_verified_at=evidence.source_verified_at,
            source_data_class=evidence.source_data_class,
            created_by=TechnicalActor.SOURCE_RECONCILER,
            data_class=data_class,
            created_at=created_at,
            updated_at=created_at,
        )

    @staticmethod
    def _new_evidence(
        *,
        vault_id: uuid.UUID,
        target_derived_object_id: uuid.UUID,
        anchor: VerifiedEvidenceAnchor,
        data_class: DataClass,
        created_at: datetime,
    ) -> EvidenceLink:
        return EvidenceLink(
            id=uuid.uuid4(),
            vault_id=vault_id,
            target_derived_object_id=target_derived_object_id,
            source_document_id=anchor.source_document_id,
            source_revision_id=anchor.source_revision_id,
            source_fragment_id=anchor.source_fragment_id,
            relation=anchor.relation,
            quote_start=anchor.quote_start,
            quote_end=anchor.quote_end,
            quote_hash=anchor.quote_hash,
            extractor_reason=anchor.extractor_reason,
            strength_band=anchor.strength_band,
            model_run_id=anchor.model_run_id,
            source_recorded_at=_utc(anchor.source_recorded_at),
            source_content_fingerprint=anchor.source_content_fingerprint,
            normalized_fingerprint=_evidence_fingerprint(anchor),
            authorization_snapshot_id=anchor.authorization_snapshot_id,
            source_policy_epoch=anchor.policy_epoch,
            source_generation=anchor.source_generation,
            source_verified_at=_utc(anchor.verified_at),
            source_data_class=anchor.source_data_class,
            created_by=anchor.created_by,
            data_class=data_class,
            created_at=created_at,
            updated_at=created_at,
        )

    @staticmethod
    def _version_view(
        version: ClaimVersion, state: LifecycleState, data_class: DataClass
    ) -> ClaimVersionView:
        return ClaimVersionView(
            derived_object_id=version.derived_object_id,
            version_no=version.version_no,
            statement=version.canonical_text,
            structured_payload=dict(version.structured_payload),
            epistemic_type=version.epistemic_type,
            attribution=version.attribution,
            uncertainty=version.uncertainty_text,
            state=state,
            valid_from=_utc(version.valid_from),
            valid_to=_utc(version.valid_to) if version.valid_to else None,
            valid_time_precision=version.valid_time_precision,
            valid_time_original=version.valid_time_original,
            valid_timezone=version.valid_timezone,
            system_from=_utc(version.system_from),
            system_to=_utc(version.system_to) if version.system_to else None,
            confidence_band=version.confidence_band,
            pipeline_version=version.pipeline_version,
            data_class=data_class,
            origin=version.origin,
            correction_mode=version.correction_mode,
            supersedes_derived_object_id=version.supersedes_derived_object_id,
            origin_verdict_id=version.origin_verdict_id,
            normalized_fingerprint=version.normalized_fingerprint,
        )

    @staticmethod
    def _evidence_view(evidence: EvidenceLink) -> EvidenceView:
        return EvidenceView(
            id=evidence.id,
            source_document_id=evidence.source_document_id,
            source_revision_id=evidence.source_revision_id,
            source_fragment_id=evidence.source_fragment_id,
            relation=evidence.relation,
            quote_start=evidence.quote_start,
            quote_end=evidence.quote_end,
            quote_hash=evidence.quote_hash,
            extractor_reason=evidence.extractor_reason,
            strength_band=evidence.strength_band,
            source_recorded_at=(
                _utc(evidence.source_recorded_at) if evidence.source_recorded_at else None
            ),
            source_data_class=evidence.source_data_class,
            normalized_fingerprint=evidence.normalized_fingerprint,
            authorization_snapshot_id=evidence.authorization_snapshot_id,
            policy_epoch=evidence.source_policy_epoch,
            source_generation=evidence.source_generation,
        )

    @staticmethod
    def _verdict_view(verdict: UserVerdict) -> VerdictView:
        return VerdictView(
            id=verdict.id,
            target_derived_object_id=verdict.target_derived_object_id,
            sequence_no=verdict.sequence_no,
            verdict=verdict.verdict,
            correction_text=verdict.correction_text,
            replacement=MemoryService._replacement_from_payload(verdict.replacement_payload),
            reason=verdict.reason_optional,
            created_at=_utc(verdict.created_at),
        )

    @staticmethod
    def _encode_cursor(offset: int) -> str:
        return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii")

    @staticmethod
    def _decode_cursor(cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            value = int(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("Invalid memory inbox cursor") from exc
        if value < 0:
            raise ValueError("Invalid memory inbox cursor")
        return value


class AsyncMemoryService:
    """AsyncSession adapter; domain logic still executes once on SQLAlchemy's sync bridge."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        evidence_source_verifier: EvidenceSourceVerifier | None = None,
        correction_source_recorder: CorrectionSourceRecorder | None = None,
        safety_classifier: MemorySafetyClassifier | None = None,
        authorization_verifier: MemoryAuthorizationVerifier | None = None,
        subject_entity_verifier: SubjectEntityVerifier | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._evidence_source_verifier = evidence_source_verifier
        self._correction_source_recorder = correction_source_recorder
        self._safety_classifier = safety_classifier
        self._authorization_verifier = authorization_verifier
        self._subject_entity_verifier = subject_entity_verifier
        self._clock = clock

    def _service(self, session: Session) -> MemoryService:
        return MemoryService(
            session,
            evidence_source_verifier=self._evidence_source_verifier,
            correction_source_recorder=self._correction_source_recorder,
            safety_classifier=self._safety_classifier,
            authorization_verifier=self._authorization_verifier,
            subject_entity_verifier=self._subject_entity_verifier,
            clock=self._clock,
        )

    async def create_claim(self, *, vault_id: uuid.UUID, proposal: ClaimProposal) -> MemoryDetail:
        return await self._session.run_sync(
            lambda session: self._service(session).create_claim(
                vault_id=vault_id, proposal=proposal
            )
        )

    async def get_as_of_version(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        real_at: datetime,
        system_at: datetime,
    ) -> ClaimVersionView:
        return await self._session.run_sync(
            lambda session: self._service(session).get_as_of_version(
                vault_id=vault_id,
                memory_id=memory_id,
                real_at=real_at,
                system_at=system_at,
            )
        )

    async def get_detail(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        real_at: datetime | None = None,
        system_at: datetime | None = None,
    ) -> MemoryDetail:
        return await self._session.run_sync(
            lambda session: self._service(session).get_detail(
                vault_id=vault_id,
                memory_id=memory_id,
                real_at=real_at,
                system_at=system_at,
            )
        )

    async def list_inbox(
        self, *, vault_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> InboxPage:
        return await self._session.run_sync(
            lambda session: self._service(session).list_inbox(
                vault_id=vault_id, limit=limit, cursor=cursor
            )
        )

    async def list_memories(
        self, *, vault_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> InboxPage:
        return await self._session.run_sync(
            lambda session: self._service(session).list_memories(
                vault_id=vault_id, limit=limit, cursor=cursor
            )
        )

    async def record_verdict(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        verdict: VerdictType,
        expected_etag: str,
        replacement: CorrectionReplacement | None = None,
        reason: str | None = None,
    ) -> VerdictOutcome:
        return await self._session.run_sync(
            lambda session: self._service(session).record_verdict(
                vault_id=vault_id,
                memory_id=memory_id,
                verdict=verdict,
                expected_etag=expected_etag,
                replacement=replacement,
                reason=reason,
            )
        )

    async def add_evidence(
        self,
        *,
        vault_id: uuid.UUID,
        target_derived_object_id: uuid.UUID,
        anchor: EvidenceAnchor,
        expected_etag: str,
    ) -> MemoryDetail:
        return await self._session.run_sync(
            lambda session: self._service(session).add_evidence(
                vault_id=vault_id,
                target_derived_object_id=target_derived_object_id,
                anchor=anchor,
                expected_etag=expected_etag,
            )
        )

    async def remove_evidence(
        self,
        *,
        vault_id: uuid.UUID,
        evidence_id: uuid.UUID,
        expected_etag: str,
    ) -> MemoryDetail:
        return await self._session.run_sync(
            lambda session: self._service(session).remove_evidence(
                vault_id=vault_id,
                evidence_id=evidence_id,
                expected_etag=expected_etag,
            )
        )

    async def invalidate_source_evidence(
        self, *, vault_id: uuid.UUID, change: SourceStateChange
    ) -> tuple[MemoryDetail, ...]:
        return await self._session.run_sync(
            lambda session: self._service(session).invalidate_source_evidence(
                vault_id=vault_id, change=change
            )
        )
