from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from life_coach.modules.safety import (
    ResurfaceContext,
    ResurfaceDecisionReason,
    ResurfaceGrant,
    ResurfacePurpose,
    SensitiveMemoryPolicy,
)

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)


def grant(**overrides: object) -> ResurfaceGrant:
    values: dict[str, object] = {
        "source_id": "sensitive-source",
        "purpose": ResurfacePurpose.REFLECTION,
        "context": ResurfaceContext.ACTIVE_SESSION,
        "granted_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(overrides)
    return ResurfaceGrant(**values)  # type: ignore[arg-type]


def test_sensitive_trauma_source_is_not_resurfaced_without_a_grant() -> None:
    policy = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        user_marked_sensitive=True,
    )

    decision = policy.evaluate(
        purpose=ResurfacePurpose.REFLECTION,
        context=ResurfaceContext.ACTIVE_SESSION,
        at=NOW,
    )

    assert not decision.allowed
    assert decision.reason is ResurfaceDecisionReason.DEFAULT_DENY


def test_exact_purpose_and_context_are_required() -> None:
    policy = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        user_marked_sensitive=True,
        resurface_grants=(grant(),),
    )

    assert policy.may_resurface(
        purpose=ResurfacePurpose.REFLECTION,
        context=ResurfaceContext.ACTIVE_SESSION,
        at=NOW,
    )
    assert not policy.may_resurface(
        purpose=ResurfacePurpose.MEMOIR,
        context=ResurfaceContext.ACTIVE_SESSION,
        at=NOW,
    )
    assert not policy.may_resurface(
        purpose=ResurfacePurpose.REFLECTION,
        context=ResurfaceContext.MEMOIR_DRAFT,
        at=NOW,
    )


def test_grant_is_invalid_at_its_expiry_instant() -> None:
    expiring_grant = grant()
    policy = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        resurface_grants=(expiring_grant,),
    )

    decision = policy.evaluate(
        purpose=ResurfacePurpose.REFLECTION,
        context=ResurfaceContext.ACTIVE_SESSION,
        at=expiring_grant.expires_at,
    )

    assert not decision.allowed
    assert decision.reason is ResurfaceDecisionReason.EXPIRED


def test_revocation_stops_resurfacing_immediately() -> None:
    revoked_at = NOW + timedelta(minutes=10)
    revoked_grant = grant().revoke(at=revoked_at)
    policy = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        resurface_grants=(revoked_grant,),
    )

    assert policy.may_resurface(
        purpose=ResurfacePurpose.REFLECTION,
        context=ResurfaceContext.ACTIVE_SESSION,
        at=revoked_at - timedelta(microseconds=1),
    )
    decision = policy.evaluate(
        purpose=ResurfacePurpose.REFLECTION,
        context=ResurfaceContext.ACTIVE_SESSION,
        at=revoked_at,
    )
    assert not decision.allowed
    assert decision.reason is ResurfaceDecisionReason.REVOKED


def test_external_preview_surfaces_remain_forbidden_even_with_a_grant() -> None:
    preview_grant = grant(context=ResurfaceContext.LOCK_SCREEN_PREVIEW)
    policy = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        resurface_grants=(preview_grant,),
    )

    decision = policy.evaluate(
        purpose=ResurfacePurpose.REFLECTION,
        context=ResurfaceContext.LOCK_SCREEN_PREVIEW,
        at=NOW,
    )

    assert not decision.allowed
    assert decision.reason is ResurfaceDecisionReason.SURFACE_FORBIDDEN


def test_feature_level_opt_in_is_separate_from_a_memoir_grant() -> None:
    memoir_grant = grant(
        purpose=ResurfacePurpose.MEMOIR,
        context=ResurfaceContext.MEMOIR_DRAFT,
    )
    excluded = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        resurface_grants=(memoir_grant,),
    )
    included = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        resurface_grants=(memoir_grant,),
        include_in_memoir=True,
    )

    assert not excluded.may_resurface(
        purpose=ResurfacePurpose.MEMOIR,
        context=ResurfaceContext.MEMOIR_DRAFT,
        at=NOW,
    )
    assert included.may_resurface(
        purpose=ResurfacePurpose.MEMOIR,
        context=ResurfaceContext.MEMOIR_DRAFT,
        at=NOW,
    )


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        grant(granted_at=datetime(2030, 1, 1))


def test_truthy_strings_cannot_enable_sensitive_feature_opt_ins() -> None:
    with pytest.raises(TypeError, match="include_in_memoir"):
        SensitiveMemoryPolicy(
            source_id="sensitive-source",
            include_in_memoir="false",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("unsafe_alias", ["LOCK_SCREEN_PREVIEW", "lockscreen"])
def test_unknown_or_noncanonical_context_aliases_are_rejected(unsafe_alias: str) -> None:
    with pytest.raises(TypeError, match="context must be a ResurfaceContext"):
        grant(context=unsafe_alias)


def test_policy_evaluation_rejects_raw_context_strings() -> None:
    policy = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        resurface_grants=(grant(),),
    )

    with pytest.raises(TypeError, match="context must be a ResurfaceContext"):
        policy.evaluate(
            purpose=ResurfacePurpose.REFLECTION,
            context="active_session",  # type: ignore[arg-type]
            at=NOW,
        )
