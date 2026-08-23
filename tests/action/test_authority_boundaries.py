from __future__ import annotations

from datetime import timedelta

import pytest

from life_coach.modules.action import (
    ActionAuthorityRejectedError,
    ActionCandidate,
    Experiment,
    ExternalActionAuthorization,
    ExternalActionExecution,
    IfThenPlan,
    SafetyPermitRejectedError,
    Task,
    confirm_candidate,
    domain,
    to_task,
)
from life_coach.modules.safety import OrdinaryOperation
from tests.action.helpers import (
    ACTION_AUTHORITY,
    CONTEXT,
    NOW,
    SAFETY_AUTHORITY,
    FakeTrustedClock,
    allowed_action,
    confirmation_for_action,
    permit_for_action,
    verdict_for_action,
)


def _commitment() -> ActionCandidate:
    return ActionCandidate.commitment("secure-commitment", "Reply to Li", **CONTEXT)


def test_untrusted_confirmation_and_verdict_receipts_fail_closed() -> None:
    candidate = allowed_action(_commitment())
    with pytest.raises(ActionAuthorityRejectedError):
        confirm_candidate(
            candidate,
            confirmation_for_action(candidate, trusted=False),
            action_authority=ACTION_AUTHORITY,
            clock=FakeTrustedClock(),
        )

    candidate = _commitment()
    candidate = candidate.with_safety_verdict(verdict_for_action(candidate, trusted=False))
    confirmed = confirm_candidate(
        candidate,
        confirmation_for_action(candidate),
        action_authority=ACTION_AUTHORITY,
        clock=FakeTrustedClock(),
    )
    with pytest.raises(ActionAuthorityRejectedError):
        to_task(
            confirmed,
            action_authority=ACTION_AUTHORITY,
            safety_authority=SAFETY_AUTHORITY,
            safety_permit=permit_for_action(candidate, OrdinaryOperation.TODO_CREATION),
            clock=FakeTrustedClock(),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("session_id", "other-session"), ("input_fingerprint", "b" * 64)],
)
def test_safety_permit_binding_rejects_cross_session_or_wrong_input(
    field: str,
    value: str,
) -> None:
    candidate = allowed_action(_commitment())
    confirmed = confirm_candidate(
        candidate,
        confirmation_for_action(candidate),
        action_authority=ACTION_AUTHORITY,
        clock=FakeTrustedClock(),
    )
    permit = permit_for_action(
        candidate,
        OrdinaryOperation.TODO_CREATION,
        **{field: value},
    )
    with pytest.raises(SafetyPermitRejectedError):
        to_task(
            confirmed,
            action_authority=ACTION_AUTHORITY,
            safety_authority=SAFETY_AUTHORITY,
            safety_permit=permit,
            clock=FakeTrustedClock(),
        )


def test_safety_permit_is_single_use() -> None:
    candidate = allowed_action(_commitment())
    confirmation = confirmation_for_action(candidate)
    confirmed = confirm_candidate(
        candidate,
        confirmation,
        action_authority=ACTION_AUTHORITY,
        clock=FakeTrustedClock(),
    )
    permit = permit_for_action(candidate, OrdinaryOperation.TODO_CREATION)
    to_task(
        confirmed,
        action_authority=ACTION_AUTHORITY,
        safety_authority=SAFETY_AUTHORITY,
        safety_permit=permit,
        clock=FakeTrustedClock(),
    )
    with pytest.raises(SafetyPermitRejectedError):
        to_task(
            confirmed,
            action_authority=ACTION_AUTHORITY,
            safety_authority=SAFETY_AUTHORITY,
            safety_permit=permit,
            clock=FakeTrustedClock(),
        )


def test_trusted_clock_is_read_once_and_rejects_rollback() -> None:
    candidate = allowed_action(_commitment())
    clock = FakeTrustedClock()
    confirm_candidate(
        candidate,
        confirmation_for_action(candidate),
        action_authority=ACTION_AUTHORITY,
        clock=clock,
    )
    assert clock.calls == 1
    clock.set(NOW - timedelta(seconds=1))
    with pytest.raises(RuntimeError, match="rollback"):
        confirm_candidate(
            candidate,
            confirmation_for_action(candidate),
            action_authority=ACTION_AUTHORITY,
            clock=clock,
        )


@pytest.mark.parametrize(
    "constructor",
    [Task, Experiment, IfThenPlan, ExternalActionAuthorization, ExternalActionExecution],
)
def test_executable_results_have_no_public_constructor_bypass(constructor: object) -> None:
    assert not hasattr(domain, "_VERIFIED_CONSTRUCTION")
    with pytest.raises(ActionAuthorityRejectedError):
        constructor()  # type: ignore[operator]
