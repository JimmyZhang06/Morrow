"""Transactional domain service for bitemporal memory and user verdicts."""

from __future__ import annotations

import base64
import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from life_coach.shared.database import utc_now

from .contracts import (
    ClaimProposal,
    ClaimVersionView,
    CorrectionSourceRecorder,
    EvidenceAnchor,
    EvidenceView,
    InboxItem,
    InboxPage,
    MemoryDetail,
    VerdictOutcome,
    VerdictView,
)
from .enums import (
    Attribution,
    DataClass,
    DerivedObjectKind,
    EpistemicType,
    EvidenceRelation,
    EvidenceStrength,
    LifecycleState,
    VerdictType,
)
from .exceptions import (
    CorrectionSourceUnavailableError,
    EvidenceNotFoundError,
    InvalidEvidenceError,
    InvalidTemporalIntervalError,
    InvalidVerdictError,
    MemoryNotFoundError,
    PolicyViolationError,
    RevisionConflictError,
)
from .models import ClaimVersion, DerivedObject, EvidenceLink, MemoryClaim, UserVerdict
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
    relation: EvidenceRelation
    source_recorded_at: datetime | None
    deleted_at: datetime | None = None


class MemoryOperations(Protocol):
    """Small application port consumed by the FastAPI router factory."""

    def list_inbox(
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
        correction_text: str | None = None,
        reason: str | None = None,
    ) -> VerdictOutcome: ...


class AsyncMemoryOperations(Protocol):
    """Async application port used by FastAPI with the project's asyncpg stack."""

    async def list_inbox(
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
        correction_text: str | None = None,
        reason: str | None = None,
    ) -> VerdictOutcome: ...


def make_etag(derived_object_id: uuid.UUID, version_no: int, review_revision: int) -> str:
    """Return an opaque aggregate token covering both version and verdict/evidence head."""

    return f'"cv:{derived_object_id}:{version_no}:{review_revision}"'


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


def _validate_anchor(anchor: EvidenceAnchor) -> None:
    if not anchor.quote_hash.strip():
        raise InvalidEvidenceError("Evidence quote_hash is required")
    if not anchor.extractor_reason.strip():
        raise InvalidEvidenceError("Evidence extractor_reason is required")
    paired_offsets = anchor.quote_start is None and anchor.quote_end is None
    valid_offsets = (
        anchor.quote_start is not None
        and anchor.quote_end is not None
        and anchor.quote_start >= 0
        and anchor.quote_end > anchor.quote_start
    )
    if not (paired_offsets or valid_offsets):
        raise InvalidEvidenceError("Evidence quote offsets must form a non-empty pair")
    _require_aware("source_recorded_at", anchor.source_recorded_at)


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
        correction_source_recorder: CorrectionSourceRecorder | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._correction_source_recorder = correction_source_recorder
        self._clock = clock

    def create_claim(self, *, vault_id: uuid.UUID, proposal: ClaimProposal) -> MemoryDetail:
        """Persist a proposal after provenance, temporal, and safety policy checks."""

        if not proposal.canonical_text.strip():
            raise PolicyViolationError("A memory statement cannot be blank")
        _validate_interval("valid", proposal.valid_from, proposal.valid_to)
        validate_persistable_memory(
            canonical_text=proposal.canonical_text,
            structured_payload=proposal.structured_payload,
            epistemic_type=proposal.epistemic_type,
            attribution=proposal.attribution,
            data_class=proposal.data_class,
        )
        for anchor in proposal.evidence:
            _validate_anchor(anchor)
        if not any(anchor.relation is EvidenceRelation.SUPPORTS for anchor in proposal.evidence):
            raise InvalidEvidenceError(
                "A memory claim requires at least one supporting Source fragment"
            )

        now = self._now()
        memory_id = uuid.uuid4()
        derived_id = uuid.uuid4()
        with self._session.begin_nested():
            claim = MemoryClaim(
                id=memory_id,
                vault_id=vault_id,
                kind=proposal.kind,
                subject_entity_id=proposal.subject_entity_id,
                created_by=proposal.created_by,
                data_class=proposal.data_class,
                created_at=now,
                updated_at=now,
            )
            derived = DerivedObject(
                id=derived_id,
                vault_id=vault_id,
                object_kind=DerivedObjectKind.CLAIM_VERSION,
                created_by=proposal.created_by,
                data_class=proposal.data_class,
                review_revision=0,
                created_at=now,
                updated_at=now,
            )
            evidence = [
                self._new_evidence(
                    vault_id=vault_id,
                    target_derived_object_id=derived_id,
                    anchor=anchor,
                    data_class=proposal.data_class,
                    created_by=proposal.created_by,
                    created_at=now,
                )
                for anchor in proposal.evidence
            ]
            initial_state = policy_lifecycle(
                reduced_state=LifecycleState.CANDIDATE,
                activation_is_allowed=activation_allowed(
                    kind=proposal.kind,
                    data_class=proposal.data_class,
                    epistemic_type=proposal.epistemic_type,
                    attribution=proposal.attribution,
                    evidence=evidence,
                    last_decisive_verdict=None,
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
            history_views.append(self._version_view(history_version, history_state))

        all_verdicts = self._verdicts_for_targets(
            vault_id=vault_id, target_ids=all_target_ids, system_at=cutoff
        )
        evidence_views = tuple(self._evidence_view(item) for item in selected_evidence)
        return MemoryDetail(
            memory_id=claim.id,
            kind=claim.kind,
            subject_entity_id=claim.subject_entity_id,
            version=self._version_view(version, selected_state),
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
            current_verdict=selected_reduction.current_verdict,
            etag=make_etag(derived.id, version.version_no, derived.review_revision),
            allowed_uses=allowed_uses(
                state=selected_state,
                kind=claim.kind,
                data_class=derived.data_class,
                current_verdict=selected_reduction.current_verdict,
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
                ClaimVersion.lifecycle_state.in_(
                    [LifecycleState.CANDIDATE, LifecycleState.DISPUTED]
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
                    version=self._version_view(version, state),
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

    def record_verdict(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        verdict: VerdictType,
        expected_etag: str,
        correction_text: str | None = None,
        reason: str | None = None,
    ) -> VerdictOutcome:
        correction_text = correction_text.strip() if correction_text is not None else None
        if verdict is VerdictType.CORRECT and not correction_text:
            raise InvalidVerdictError("A correct verdict requires non-blank correction_text")
        if verdict is not VerdictType.CORRECT and correction_text is not None:
            raise InvalidVerdictError("correction_text is only valid for a correct verdict")

        with self._session.begin_nested():
            claim, version, derived = self._locked_current(vault_id=vault_id, memory_id=memory_id)
            self._assert_etag(expected_etag, version=version, derived=derived)
            evidence = self._evidence_for_target(
                vault_id=vault_id, target_id=version.derived_object_id
            )
            existing_events = self._verdicts_for_target(
                vault_id=vault_id, target_id=version.derived_object_id
            )
            next_sequence = max((event.sequence_no for event in existing_events), default=0) + 1
            now = self._next_aggregate_time(version=version, derived=derived)
            verdict_data_class = (
                DataClass.HIGHLY_SENSITIVE
                if correction_text and contains_clinical_language(correction_text)
                else derived.data_class
            )
            event = UserVerdict(
                id=uuid.uuid4(),
                vault_id=vault_id,
                target_derived_object_id=version.derived_object_id,
                sequence_no=next_sequence,
                verdict=verdict,
                correction_text=correction_text,
                reason_optional=reason,
                created_at=now,
                created_by="user",
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

            if verdict is VerdictType.CORRECT:
                assert correction_text is not None
                current_id, current_version_no, current_state = self._correct_version(
                    vault_id=vault_id,
                    claim=claim,
                    version=version,
                    derived=derived,
                    event=event,
                    correction_text=correction_text,
                    changed_at=now,
                )
                current_etag = make_etag(current_id, current_version_no, 0)
            else:
                self._set_projection_state(version, resulting_state)
                self._session.flush()
                current_id = version.derived_object_id
                current_version_no = version.version_no
                current_state = resulting_state
                current_etag = make_etag(current_id, current_version_no, old_review_revision + 1)

            outcome = VerdictOutcome(
                verdict_id=event.id,
                memory_id=memory_id,
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
        created_by: str = "knowledge-pipeline",
    ) -> MemoryDetail:
        _validate_anchor(anchor)
        with self._session.begin_nested():
            claim, version, derived = self._version_target(
                vault_id=vault_id, target_id=target_derived_object_id, for_update=True
            )
            self._assert_etag(expected_etag, version=version, derived=derived)
            now = self._next_aggregate_time(version=version, derived=derived)
            new_link = self._new_evidence(
                vault_id=vault_id,
                target_derived_object_id=target_derived_object_id,
                anchor=anchor,
                data_class=derived.data_class,
                created_by=created_by,
                created_at=now,
            )
            old_review_revision = derived.review_revision
            self._bump_review_revision(
                derived=derived, expected_revision=old_review_revision, changed_at=now
            )
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

    def _now(self) -> datetime:
        value = self._clock()
        _require_aware("clock", value)
        return _utc(value)

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
        return list(
            self._session.scalars(statement.order_by(EvidenceLink.created_at, EvidenceLink.id))
        )

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
            data_class=derived.data_class,
            epistemic_type=version.epistemic_type,
            attribution=version.attribution,
            evidence=evidence,
            last_decisive_verdict=preliminary.last_decisive_verdict,
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

    def _correct_version(
        self,
        *,
        vault_id: uuid.UUID,
        claim: MemoryClaim,
        version: ClaimVersion,
        derived: DerivedObject,
        event: UserVerdict,
        correction_text: str,
        changed_at: datetime,
    ) -> tuple[uuid.UUID, int, LifecycleState]:
        if self._correction_source_recorder is None:
            raise CorrectionSourceUnavailableError(
                "Correction requires an injected Source correction recorder"
            )
        new_data_class = (
            DataClass.HIGHLY_SENSITIVE
            if contains_clinical_language(correction_text)
            else derived.data_class
        )
        anchor = self._correction_source_recorder.record_correction(
            session=self._session,
            vault_id=vault_id,
            memory_id=claim.id,
            correction_text=correction_text,
            data_class=new_data_class,
            recorded_at=changed_at,
        )
        _require_aware("correction_source.recorded_at", anchor.recorded_at)

        version.lifecycle_state = replacement_transition(version.lifecycle_state)
        version.system_to = changed_at
        self._session.flush([version, event])

        new_derived_id = uuid.uuid4()
        new_version_no = version.version_no + 1
        if new_data_class is DataClass.HIGHLY_SENSITIVE:
            claim.data_class = DataClass.HIGHLY_SENSITIVE
            claim.updated_at = changed_at
        new_derived = DerivedObject(
            id=new_derived_id,
            vault_id=vault_id,
            object_kind=DerivedObjectKind.CLAIM_VERSION,
            created_by="user",
            data_class=new_data_class,
            review_revision=0,
            created_at=changed_at,
            updated_at=changed_at,
        )
        correction_evidence = EvidenceLink(
            id=uuid.uuid4(),
            vault_id=vault_id,
            target_derived_object_id=new_derived_id,
            source_fragment_id=anchor.source_fragment_id,
            relation=EvidenceRelation.SUPPORTS,
            quote_start=0,
            quote_end=len(correction_text),
            quote_hash=hashlib.sha256(correction_text.encode("utf-8")).hexdigest(),
            extractor_reason="User-authored correction",
            strength_band=EvidenceStrength.STRONG,
            model_run_id=None,
            source_recorded_at=_utc(anchor.recorded_at),
            created_by="user",
            data_class=new_data_class,
            created_at=changed_at,
            updated_at=changed_at,
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
            canonical_text=correction_text,
            structured_payload={"corrected_from": str(version.derived_object_id)},
            epistemic_type=EpistemicType.USER_AUTHORED,
            attribution=Attribution.SELF_REPORT,
            uncertainty_text=version.uncertainty_text,
            initial_lifecycle_state=new_state,
            lifecycle_state=new_state,
            valid_from=version.valid_from,
            valid_to=version.valid_to,
            valid_time_precision=version.valid_time_precision,
            valid_time_original=version.valid_time_original,
            valid_timezone=version.valid_timezone,
            system_from=changed_at,
            system_to=None,
            confidence_band=version.confidence_band,
            pipeline_version="user-correction-v1",
            model_run_id=None,
        )
        self._session.add(new_derived)
        self._session.flush([new_derived])
        self._session.add_all([new_version, correction_evidence])
        self._session.flush()
        return new_derived_id, new_version_no, new_state

    @staticmethod
    def _new_evidence(
        *,
        vault_id: uuid.UUID,
        target_derived_object_id: uuid.UUID,
        anchor: EvidenceAnchor,
        data_class: DataClass,
        created_by: str,
        created_at: datetime,
    ) -> EvidenceLink:
        return EvidenceLink(
            id=uuid.uuid4(),
            vault_id=vault_id,
            target_derived_object_id=target_derived_object_id,
            source_fragment_id=anchor.source_fragment_id,
            relation=anchor.relation,
            quote_start=anchor.quote_start,
            quote_end=anchor.quote_end,
            quote_hash=anchor.quote_hash,
            extractor_reason=anchor.extractor_reason,
            strength_band=anchor.strength_band,
            model_run_id=anchor.model_run_id,
            source_recorded_at=(
                _utc(anchor.source_recorded_at) if anchor.source_recorded_at else None
            ),
            created_by=created_by,
            data_class=data_class,
            created_at=created_at,
            updated_at=created_at,
        )

    @staticmethod
    def _version_view(version: ClaimVersion, state: LifecycleState) -> ClaimVersionView:
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
        )

    @staticmethod
    def _evidence_view(evidence: EvidenceLink) -> EvidenceView:
        return EvidenceView(
            id=evidence.id,
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
        )

    @staticmethod
    def _verdict_view(verdict: UserVerdict) -> VerdictView:
        return VerdictView(
            id=verdict.id,
            target_derived_object_id=verdict.target_derived_object_id,
            sequence_no=verdict.sequence_no,
            verdict=verdict.verdict,
            correction_text=verdict.correction_text,
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
        correction_source_recorder: CorrectionSourceRecorder | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._correction_source_recorder = correction_source_recorder
        self._clock = clock

    def _service(self, session: Session) -> MemoryService:
        return MemoryService(
            session,
            correction_source_recorder=self._correction_source_recorder,
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

    async def record_verdict(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        verdict: VerdictType,
        expected_etag: str,
        correction_text: str | None = None,
        reason: str | None = None,
    ) -> VerdictOutcome:
        return await self._session.run_sync(
            lambda session: self._service(session).record_verdict(
                vault_id=vault_id,
                memory_id=memory_id,
                verdict=verdict,
                expected_etag=expected_etag,
                correction_text=correction_text,
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
        created_by: str = "knowledge-pipeline",
    ) -> MemoryDetail:
        return await self._session.run_sync(
            lambda session: self._service(session).add_evidence(
                vault_id=vault_id,
                target_derived_object_id=target_derived_object_id,
                anchor=anchor,
                expected_etag=expected_etag,
                created_by=created_by,
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
