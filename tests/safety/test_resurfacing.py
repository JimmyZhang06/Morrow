from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from life_coach.modules.safety import (
    ResurfaceContext,
    ResurfaceDecisionReason,
    ResurfaceGrant,
    ResurfacePurpose,
    SensitiveMemoryPolicy,
    evaluate_grants,
)

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)


def grant(**overrides: object) -> ResurfaceGrant:
    values: dict[str, object] = {
        "grant_id": "resurface-grant-1",
        "version": 1,
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
    assert revoked_grant.grant_id == "resurface-grant-1"
    assert revoked_grant.version == 2


@pytest.mark.parametrize("old_copy_last", [False, True])
def test_latest_revocation_wins_over_an_old_active_copy(old_copy_last: bool) -> None:
    active = grant()
    revoked_at = NOW + timedelta(minutes=10)
    revoked = active.revoke(at=revoked_at)
    revisions = (revoked, active) if old_copy_last else (active, revoked)
    policy = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        resurface_grants=revisions,
    )

    decision = policy.evaluate(
        purpose=ResurfacePurpose.REFLECTION,
        context=ResurfaceContext.ACTIVE_SESSION,
        at=revoked_at,
    )

    assert not decision.allowed
    assert decision.reason is ResurfaceDecisionReason.REVOKED


def test_conflicting_states_at_the_same_grant_version_fail_closed() -> None:
    active = grant()
    conflicting = grant(revoked_at=NOW)
    policy = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        resurface_grants=(active, conflicting),
    )

    decision = policy.evaluate(
        purpose=ResurfacePurpose.REFLECTION,
        context=ResurfaceContext.ACTIVE_SESSION,
        at=NOW,
    )

    assert not decision.allowed
    assert decision.reason is ResurfaceDecisionReason.DEFAULT_DENY


def test_conflicting_old_revision_still_fails_closed_after_a_newer_revision() -> None:
    active = grant()
    revoked = active.revoke(at=NOW + timedelta(minutes=10))
    conflicting_old_revision = grant(expires_at=NOW + timedelta(hours=2))
    policy = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        resurface_grants=(active, revoked, conflicting_old_revision),
    )

    decision = policy.evaluate(
        purpose=ResurfacePurpose.REFLECTION,
        context=ResurfaceContext.ACTIVE_SESSION,
        at=NOW,
    )

    assert not decision.allowed
    assert decision.reason is ResurfaceDecisionReason.DEFAULT_DENY


def test_higher_active_version_cannot_resurrect_a_revoked_grant() -> None:
    active = grant()
    revoked_at = NOW + timedelta(minutes=10)
    revoked = active.revoke(at=revoked_at)
    invalid_resurrection = grant(version=3)
    policy = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        resurface_grants=(active, revoked, invalid_resurrection),
    )

    decision = policy.evaluate(
        purpose=ResurfacePurpose.REFLECTION,
        context=ResurfaceContext.ACTIVE_SESSION,
        at=revoked_at,
    )

    assert not decision.allowed
    assert decision.reason is ResurfaceDecisionReason.DEFAULT_DENY


@pytest.mark.parametrize(
    "scope_override",
    [
        {"source_id": "different-source"},
        {"purpose": ResurfacePurpose.USER_REQUESTED_REVIEW},
        {"context": ResurfaceContext.NEUTRAL_PREVIEW},
        {"granted_at": NOW - timedelta(minutes=1)},
        {"expires_at": NOW + timedelta(hours=2)},
    ],
)
def test_grant_scope_cannot_mutate_across_versions(
    scope_override: dict[str, object],
) -> None:
    original = grant()
    mutated = grant(version=2, **scope_override)

    decision = evaluate_grants(
        (original, mutated),
        source_id=mutated.source_id,
        purpose=mutated.purpose,
        context=mutated.context,
        at=NOW,
    )

    assert not decision.allowed
    assert decision.reason is ResurfaceDecisionReason.DEFAULT_DENY


def test_identical_duplicate_of_a_grant_revision_is_not_a_conflict() -> None:
    active = grant()
    policy = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        resurface_grants=(active, active),
    )

    assert policy.may_resurface(
        purpose=ResurfacePurpose.REFLECTION,
        context=ResurfaceContext.ACTIVE_SESSION,
        at=NOW,
    )


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


@pytest.mark.parametrize(
    ("context", "purpose", "registry"),
    [
        (
            ResurfaceContext.MEMOIR_DRAFT,
            "reflection:v2",
            {"reflection:v2": ResurfacePurpose.REFLECTION},
        ),
        (
            ResurfaceContext.ANNIVERSARY_REVIEW,
            "reflection:v2",
            {"reflection:v2": ResurfacePurpose.REFLECTION},
        ),
    ],
)
def test_protected_context_opt_in_cannot_be_bypassed_by_a_purpose_alias(
    context: ResurfaceContext,
    purpose: str,
    registry: dict[str, ResurfacePurpose],
) -> None:
    policy = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        resurface_grants=(grant(purpose=purpose, context=context),),
    )

    decision = policy.evaluate(
        purpose=purpose,
        context=context,
        at=NOW,
        purpose_registry=registry,
    )

    assert not decision.allowed
    assert decision.reason is ResurfaceDecisionReason.FEATURE_NOT_OPTED_IN


@pytest.mark.parametrize("unknown_purpose", ["reflection:v2", "future_workflow"])
def test_unknown_or_versioned_purpose_defaults_to_denial(unknown_purpose: str) -> None:
    policy = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        resurface_grants=(grant(purpose=unknown_purpose),),
    )

    decision = policy.evaluate(
        purpose=unknown_purpose,
        context=ResurfaceContext.ACTIVE_SESSION,
        at=NOW,
    )

    assert not decision.allowed
    assert decision.reason is ResurfaceDecisionReason.DEFAULT_DENY
    assert not policy.resurface_grants[0].allows(
        purpose=unknown_purpose,
        context=ResurfaceContext.ACTIVE_SESSION,
        at=NOW,
    )


def test_explicit_registry_can_map_a_versioned_purpose_to_a_canonical_scope() -> None:
    versioned_purpose = "reflection:v2"
    policy = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        resurface_grants=(grant(purpose=versioned_purpose),),
    )

    assert policy.may_resurface(
        purpose=versioned_purpose,
        context=ResurfaceContext.ACTIVE_SESSION,
        at=NOW,
        purpose_registry={versioned_purpose: ResurfacePurpose.REFLECTION},
    )


def test_registry_cannot_redefine_a_canonical_purpose() -> None:
    policy = SensitiveMemoryPolicy(
        source_id="sensitive-source",
        resurface_grants=(grant(),),
    )

    with pytest.raises(ValueError, match="must not redefine canonical purposes"):
        policy.evaluate(
            purpose=ResurfacePurpose.REFLECTION,
            context=ResurfaceContext.ACTIVE_SESSION,
            at=NOW,
            purpose_registry={
                ResurfacePurpose.REFLECTION: ResurfacePurpose.MEMOIR,
            },
        )


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        grant(granted_at=datetime(2030, 1, 1))


@pytest.mark.parametrize("invalid_version", [True, 1.0, "1"])
def test_grant_version_rejects_non_integer_lookalikes(invalid_version: object) -> None:
    with pytest.raises(TypeError, match="version must be an integer"):
        grant(version=invalid_version)


def test_grant_version_must_be_positive() -> None:
    with pytest.raises(ValueError, match="version must be at least 1"):
        grant(version=0)


def test_grant_id_must_be_nonempty() -> None:
    with pytest.raises(ValueError, match="grant_id must not be empty"):
        grant(grant_id="  ")


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
