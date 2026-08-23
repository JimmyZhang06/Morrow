"""Consent rules for resurfacing sensitive source material.

Saving a source and proactively showing it again are deliberately separate
permissions.  This module defaults to denial and requires an exact, current
purpose-and-context grant for every resurfacing attempt.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from ._time import require_aware
from ._validation import require_bool, require_enum, require_string


class ResurfacePurpose(StrEnum):
    """Known reasons for showing source material again.

    Purposes remain strings so callers may introduce a versioned purpose
    without a domain release.  Display contexts are deliberately stricter
    because aliases could bypass protected-surface rules.
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

    source_id: str
    purpose: str
    context: ResurfaceContext
    granted_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        require_string(self.source_id, field_name="source_id")
        require_string(self.purpose, field_name="purpose")
        require_enum(self.context, enum_type=ResurfaceContext, field_name="context")
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

    def allows(self, *, purpose: str, context: ResurfaceContext, at: datetime) -> bool:
        """Check exact scope matching as well as temporal validity."""

        return self.purpose == purpose and self.context == context and self.is_active(at=at)

    def revoke(self, *, at: datetime) -> ResurfaceGrant:
        """Return an immutable copy revoked at ``at``.

        Repeating a revocation is idempotent and preserves the original time.
        """

        require_aware(at, field_name="at")
        if at < self.granted_at:
            raise ValueError("revocation must not precede the grant")
        if self.revoked_at is not None:
            return self
        return replace(self, revoked_at=at)


def evaluate_grants(
    grants: Iterable[ResurfaceGrant],
    *,
    source_id: str,
    purpose: str,
    context: ResurfaceContext,
    at: datetime,
) -> ResurfaceDecision:
    """Evaluate grants with default denial and exact source/scope matching."""

    require_aware(at, field_name="at")
    require_string(source_id, field_name="source_id")
    require_string(purpose, field_name="purpose")
    require_enum(context, enum_type=ResurfaceContext, field_name="context")
    scoped = tuple(
        grant
        for grant in grants
        if grant.source_id == source_id and grant.purpose == purpose and grant.context == context
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
    ) -> ResurfaceDecision:
        """Apply surface restrictions, feature opt-ins, and scoped grants."""

        require_aware(at, field_name="at")
        require_string(purpose, field_name="purpose")
        require_enum(context, enum_type=ResurfaceContext, field_name="context")
        if context in _FORBIDDEN_CONTEXTS:
            return ResurfaceDecision(False, ResurfaceDecisionReason.SURFACE_FORBIDDEN)
        if purpose == ResurfacePurpose.ANNIVERSARY and not self.anniversary_opt_in:
            return ResurfaceDecision(False, ResurfaceDecisionReason.FEATURE_NOT_OPTED_IN)
        if purpose == ResurfacePurpose.SUMMARY and not self.include_in_summary:
            return ResurfaceDecision(False, ResurfaceDecisionReason.FEATURE_NOT_OPTED_IN)
        if purpose == ResurfacePurpose.MEMOIR and not self.include_in_memoir:
            return ResurfaceDecision(False, ResurfaceDecisionReason.FEATURE_NOT_OPTED_IN)
        return evaluate_grants(
            self.resurface_grants,
            source_id=self.source_id,
            purpose=purpose,
            context=context,
            at=at,
        )

    def may_resurface(
        self,
        *,
        purpose: str,
        context: ResurfaceContext,
        at: datetime,
    ) -> bool:
        """Convenience predicate for policy gates."""

        return self.evaluate(purpose=purpose, context=context, at=at).allowed
