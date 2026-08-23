"""Consent rules for resurfacing sensitive source material.

Saving a source and proactively showing it again are deliberately separate
permissions.  This module defaults to denial and requires an exact, current
purpose-and-context grant for every resurfacing attempt.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from ._time import require_aware
from ._validation import require_bool, require_enum, require_string


class ResurfacePurpose(StrEnum):
    """Known reasons for showing source material again.

    Persisted grants may contain strings introduced by a newer application
    version, but policy evaluation denies them until a reviewed registry maps
    them to one of these canonical purposes.
    """

    USER_REQUESTED_REVIEW = "user_requested_review"
    REFLECTION = "reflection"
    ANNIVERSARY = "anniversary"
    SUMMARY = "summary"
    MEMOIR = "memoir"
    NOTIFICATION = "notification"


class ResurfaceContext(StrEnum):
    """Known display contexts for resurfaced material."""

    ACTIVE_SESSION = "active_session"
    NEUTRAL_PREVIEW = "neutral_preview"
    ANNIVERSARY_REVIEW = "anniversary_review"
    MEMOIR_DRAFT = "memoir_draft"
    LOCK_SCREEN_PREVIEW = "lock_screen_preview"
    PUSH_NOTIFICATION_PREVIEW = "push_notification_preview"
    SHARED_TASK_TITLE = "shared_task_title"


class ResurfaceDecisionReason(StrEnum):
    """Auditable reasons for a resurfacing policy result."""

    ALLOWED = "allowed"
    DEFAULT_DENY = "default_deny"
    NOT_YET_GRANTED = "not_yet_granted"
    EXPIRED = "expired"
    REVOKED = "revoked"
    SURFACE_FORBIDDEN = "surface_forbidden"
    FEATURE_NOT_OPTED_IN = "feature_not_opted_in"


@dataclass(frozen=True, slots=True)
class ResurfaceDecision:
    """The result of checking a sensitive-source resurfacing request."""

    allowed: bool
    reason: ResurfaceDecisionReason


@dataclass(frozen=True, slots=True)
class ResurfaceGrant:
    """A revocable, expiring permission scoped to one purpose and context."""

    grant_id: str
    version: int
    source_id: str
    purpose: str
    context: ResurfaceContext
    granted_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        require_string(self.grant_id, field_name="grant_id")
        require_string(self.source_id, field_name="source_id")
        require_string(self.purpose, field_name="purpose")
        require_enum(self.context, enum_type=ResurfaceContext, field_name="context")
        if not self.grant_id.strip():
            raise ValueError("grant_id must not be empty")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("version must be an integer")
        if self.version < 1:
            raise ValueError("version must be at least 1")
        if not self.source_id.strip():
            raise ValueError("source_id must not be empty")
        if not self.purpose.strip():
            raise ValueError("purpose must not be empty")
        require_aware(self.granted_at, field_name="granted_at")
        require_aware(self.expires_at, field_name="expires_at")
        if self.expires_at <= self.granted_at:
            raise ValueError("expires_at must be after granted_at")
        if self.revoked_at is not None:
            require_aware(self.revoked_at, field_name="revoked_at")
            if self.revoked_at < self.granted_at:
                raise ValueError("revoked_at must not be before granted_at")

    def is_active(self, *, at: datetime) -> bool:
        """Return whether this grant is usable at ``at``.

        The validity window is half-open: a grant is invalid at exactly
        ``expires_at`` or ``revoked_at``.
        """

        require_aware(at, field_name="at")
        return self.granted_at <= at < self.expires_at and (
            self.revoked_at is None or at < self.revoked_at
        )

    def allows(
        self,
        *,
        purpose: str,
        context: ResurfaceContext,
        at: datetime,
        purpose_registry: PurposeRegistry | None = None,
    ) -> bool:
        """Check reviewed scope matching as well as temporal validity."""

        require_string(purpose, field_name="purpose")
        require_enum(context, enum_type=ResurfaceContext, field_name="context")
        _validate_purpose_registry(purpose_registry)
        requested_purpose = _canonical_purpose(purpose, registry=purpose_registry)
        granted_purpose = _canonical_purpose(self.purpose, registry=purpose_registry)
        return (
            requested_purpose is not None
            and granted_purpose is requested_purpose
            and self.context is context
            and self.is_active(at=at)
        )

    def revoke(self, *, at: datetime) -> ResurfaceGrant:
        """Return an immutable copy revoked at ``at``.

        Repeating a revocation is idempotent and preserves the original time.
        """

        require_aware(at, field_name="at")
        if at < self.granted_at:
            raise ValueError("revocation must not precede the grant")
        if self.revoked_at is not None:
            return self
        return replace(self, version=self.version + 1, revoked_at=at)


PurposeRegistry = Mapping[str, ResurfacePurpose]


def _validate_purpose_registry(registry: PurposeRegistry | None) -> None:
    if registry is None:
        return
    if not isinstance(registry, Mapping):
        raise TypeError("purpose_registry must be a mapping")
    for alias, canonical in registry.items():
        require_string(alias, field_name="purpose_registry alias")
        if not alias.strip():
            raise ValueError("purpose_registry aliases must not be empty")
        try:
            ResurfacePurpose(alias)
        except ValueError:
            pass
        else:
            raise ValueError("purpose_registry must not redefine canonical purposes")
        require_enum(
            canonical,
            enum_type=ResurfacePurpose,
            field_name="purpose_registry value",
        )


def _canonical_purpose(
    purpose: str,
    *,
    registry: PurposeRegistry | None,
) -> ResurfacePurpose | None:
    """Resolve only reviewed purposes, failing closed for unknown strings."""

    try:
        return ResurfacePurpose(purpose)
    except ValueError:
        if registry is None:
            return None
        return registry.get(purpose)


def _latest_grants(
    grants: Iterable[ResurfaceGrant],
    *,
    source_id: str,
) -> tuple[tuple[ResurfaceGrant, ...], bool]:
    """Collapse immutable revisions and flag ambiguous same-version states."""

    latest_by_id: dict[str, ResurfaceGrant] = {}
    revision_by_key: dict[tuple[str, int], ResurfaceGrant] = {}
    revisions_by_id: dict[str, list[ResurfaceGrant]] = {}
    conflict_for_source = False
    for grant in grants:
        if not isinstance(grant, ResurfaceGrant):
            raise TypeError("grants must contain only ResurfaceGrant values")
        revisions_by_id.setdefault(grant.grant_id, []).append(grant)
        revision_key = (grant.grant_id, grant.version)
        existing_revision = revision_by_key.get(revision_key)
        if existing_revision is None:
            revision_by_key[revision_key] = grant
        elif grant != existing_revision and (
            grant.source_id == source_id or existing_revision.source_id == source_id
        ):
            conflict_for_source = True
        current = latest_by_id.get(grant.grant_id)
        if current is None or grant.version > current.version:
            latest_by_id[grant.grant_id] = grant
    for revisions in revisions_by_id.values():
        if not any(revision.source_id == source_id for revision in revisions):
            continue
        first = revisions[0]
        immutable_scope = (
            first.source_id,
            first.purpose,
            first.context,
            first.granted_at,
            first.expires_at,
        )
        if any(
            (
                revision.source_id,
                revision.purpose,
                revision.context,
                revision.granted_at,
                revision.expires_at,
            )
            != immutable_scope
            for revision in revisions[1:]
        ):
            conflict_for_source = True
        revoked_at: datetime | None = None
        for revision in sorted(revisions, key=lambda item: item.version):
            if revoked_at is not None and revision.revoked_at != revoked_at:
                conflict_for_source = True
            elif revision.revoked_at is not None:
                revoked_at = revision.revoked_at
    return tuple(latest_by_id.values()), conflict_for_source


def evaluate_grants(
    grants: Iterable[ResurfaceGrant],
    *,
    source_id: str,
    purpose: str,
    context: ResurfaceContext,
    at: datetime,
    purpose_registry: PurposeRegistry | None = None,
) -> ResurfaceDecision:
    """Evaluate the latest unambiguous grant states with default denial."""

    require_aware(at, field_name="at")
    require_string(source_id, field_name="source_id")
    require_string(purpose, field_name="purpose")
    require_enum(context, enum_type=ResurfaceContext, field_name="context")
    _validate_purpose_registry(purpose_registry)
    requested_purpose = _canonical_purpose(purpose, registry=purpose_registry)
    if requested_purpose is None:
        return ResurfaceDecision(False, ResurfaceDecisionReason.DEFAULT_DENY)
    latest_grants, has_conflict = _latest_grants(grants, source_id=source_id)
    if has_conflict:
        return ResurfaceDecision(False, ResurfaceDecisionReason.DEFAULT_DENY)
    scoped = tuple(
        grant
        for grant in latest_grants
        if grant.source_id == source_id
        and _canonical_purpose(grant.purpose, registry=purpose_registry) is requested_purpose
        and grant.context == context
    )
    if not scoped:
        return ResurfaceDecision(False, ResurfaceDecisionReason.DEFAULT_DENY)
    if any(grant.is_active(at=at) for grant in scoped):
        return ResurfaceDecision(True, ResurfaceDecisionReason.ALLOWED)
    if any(grant.revoked_at is not None and at >= grant.revoked_at for grant in scoped):
        return ResurfaceDecision(False, ResurfaceDecisionReason.REVOKED)
    if all(at >= grant.expires_at for grant in scoped):
        return ResurfaceDecision(False, ResurfaceDecisionReason.EXPIRED)
    return ResurfaceDecision(False, ResurfaceDecisionReason.NOT_YET_GRANTED)


class PreviewMode(StrEnum):
    """How sensitive content is introduced before the user opts to continue."""

    NEUTRAL_COLLAPSED = "neutral_collapsed"
    METADATA_ONLY = "metadata_only"


_FORBIDDEN_CONTEXTS = frozenset(
    {
        ResurfaceContext.LOCK_SCREEN_PREVIEW,
        ResurfaceContext.PUSH_NOTIFICATION_PREVIEW,
        ResurfaceContext.SHARED_TASK_TITLE,
    }
)


@dataclass(frozen=True, slots=True)
class SensitiveMemoryPolicy:
    """Per-source policy for sensitive content.

    Provisional sensitivity is only a routing hint for the current operation;
    persistence layers must not turn it into a hidden trauma label.
    """

    source_id: str
    user_marked_sensitive: bool = False
    provisional_sensitive: bool = False
    resurface_grants: tuple[ResurfaceGrant, ...] = ()
    anniversary_opt_in: bool = False
    include_in_summary: bool = False
    include_in_memoir: bool = False
    preview_mode: PreviewMode = PreviewMode.NEUTRAL_COLLAPSED

    def __post_init__(self) -> None:
        require_string(self.source_id, field_name="source_id")
        for field_name in (
            "user_marked_sensitive",
            "provisional_sensitive",
            "anniversary_opt_in",
            "include_in_summary",
            "include_in_memoir",
        ):
            require_bool(getattr(self, field_name), field_name=field_name)
        require_enum(self.preview_mode, enum_type=PreviewMode, field_name="preview_mode")
        if any(not isinstance(grant, ResurfaceGrant) for grant in self.resurface_grants):
            raise TypeError("resurface_grants must contain only ResurfaceGrant values")
        if not self.source_id.strip():
            raise ValueError("source_id must not be empty")
        if any(grant.source_id != self.source_id for grant in self.resurface_grants):
            raise ValueError("all grants must belong to this source")

    def evaluate(
        self,
        *,
        purpose: str,
        context: ResurfaceContext,
        at: datetime,
        purpose_registry: PurposeRegistry | None = None,
    ) -> ResurfaceDecision:
        """Apply surface restrictions, feature opt-ins, and scoped grants."""

        require_aware(at, field_name="at")
        require_string(purpose, field_name="purpose")
        require_enum(context, enum_type=ResurfaceContext, field_name="context")
        _validate_purpose_registry(purpose_registry)
        canonical_purpose = _canonical_purpose(purpose, registry=purpose_registry)
        if context in _FORBIDDEN_CONTEXTS:
            return ResurfaceDecision(False, ResurfaceDecisionReason.SURFACE_FORBIDDEN)
        if canonical_purpose is None:
            return ResurfaceDecision(False, ResurfaceDecisionReason.DEFAULT_DENY)
        if context is ResurfaceContext.ANNIVERSARY_REVIEW and not self.anniversary_opt_in:
            return ResurfaceDecision(False, ResurfaceDecisionReason.FEATURE_NOT_OPTED_IN)
        if context is ResurfaceContext.MEMOIR_DRAFT and not self.include_in_memoir:
            return ResurfaceDecision(False, ResurfaceDecisionReason.FEATURE_NOT_OPTED_IN)
        if canonical_purpose is ResurfacePurpose.ANNIVERSARY and not self.anniversary_opt_in:
            return ResurfaceDecision(False, ResurfaceDecisionReason.FEATURE_NOT_OPTED_IN)
        if canonical_purpose is ResurfacePurpose.SUMMARY and not self.include_in_summary:
            return ResurfaceDecision(False, ResurfaceDecisionReason.FEATURE_NOT_OPTED_IN)
        if canonical_purpose is ResurfacePurpose.MEMOIR and not self.include_in_memoir:
            return ResurfaceDecision(False, ResurfaceDecisionReason.FEATURE_NOT_OPTED_IN)
        return evaluate_grants(
            self.resurface_grants,
            source_id=self.source_id,
            purpose=purpose,
            context=context,
            at=at,
            purpose_registry=purpose_registry,
        )

    def may_resurface(
        self,
        *,
        purpose: str,
        context: ResurfaceContext,
        at: datetime,
        purpose_registry: PurposeRegistry | None = None,
    ) -> bool:
        """Convenience predicate for policy gates."""

        return self.evaluate(
            purpose=purpose,
            context=context,
            at=at,
            purpose_registry=purpose_registry,
        ).allowed
