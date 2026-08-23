from dataclasses import replace
from datetime import datetime, timedelta
from typing import cast

import pytest

from life_coach.modules.action import (
    ActionCandidate,
    ActionSafetyBlockedError,
    ActionSafetyOutcome,
    ActionSafetyRequiredError,
    ConfirmationActor,
    ConfirmationMismatchError,
    ConfirmationRequiredError,
    ExternalActionAlreadyConsumedError,
    ExternalActionAuthorizationExpiredError,
    ExternalActionAuthorizationLineageError,
    ExternalActionAuthorizationRevokedError,
    ExternalActionState,
    ExternalAuthorizationTransition,
    InvalidActionCandidateError,
    authorize_external_action,
    confirm_candidate,
    record_external_action_execution,
    revoke_external_action_authorization,
)
from tests.action.helpers import (
    CONTEXT,
    NOW,
    ActionContext,
    MemoryConsumptionPort,
    allowed_action,
    confirmation_for_action,
    external_confirmation,
    verdict_for_action,
)


def _calendar_candidate(description: str = "Create one calendar event") -> ActionCandidate:
    return ActionCandidate.external_action_candidate(
        "external-1",
        description,
        scope="calendar.events.create",
        payload={"title": "Call Li", "starts_at": "2026-08-24T10:00:00+08:00"},
        **CONTEXT,
    )


def _allowed_calendar(description: str = "Create one calendar event") -> ActionCandidate:
    return allowed_action(_calendar_candidate(description))


def test_external_action_is_not_executable_before_final_confirmation() -> None:
    candidate = _allowed_calendar()
    assert not candidate.is_executable
    with pytest.raises(ConfirmationRequiredError):
        confirm_candidate(candidate, confirmation_for_action(candidate), at=NOW)


@pytest.mark.parametrize(
    ("scope", "payload"),
    [
        ("calendar.events.update", None),
        (None, {"title": "Call someone else"}),
    ],
)
def test_external_confirmation_must_match_scope_and_payload(
    scope: str | None,
    payload: dict[str, object] | None,
) -> None:
    candidate = _allowed_calendar()
    confirmation = external_confirmation(candidate, scope=scope, payload=payload)
    with pytest.raises(ConfirmationMismatchError):
        authorize_external_action(candidate, confirmation, MemoryConsumptionPort(), at=NOW)


def test_external_ready_requires_current_exact_allowed_verdict() -> None:
    candidate = _allowed_calendar()
    port = MemoryConsumptionPort()
    authorization = authorize_external_action(
        candidate,
        external_confirmation(candidate),
        port,
        at=NOW,
    )

    assert authorization.state is ExternalActionState.READY
    assert authorization.is_executable_at(NOW)
    assert authorization.scope == "calendar.events.create"
    assert authorization.payload["title"] == "Call Li"


def test_external_ready_fails_closed_without_or_with_blocked_verdict() -> None:
    missing = _calendar_candidate()
    with pytest.raises(ActionSafetyRequiredError):
        authorize_external_action(
            missing, external_confirmation(missing), MemoryConsumptionPort(), at=NOW
        )

    blocked = _calendar_candidate()
    blocked = blocked.with_safety_verdict(verdict_for_action(blocked, ActionSafetyOutcome.BLOCKED))
    with pytest.raises(ActionSafetyBlockedError):
        authorize_external_action(
            blocked, external_confirmation(blocked), MemoryConsumptionPort(), at=NOW
        )


def test_same_id_changed_description_rejects_old_external_confirmation() -> None:
    original = _allowed_calendar("Create Li calendar event")
    old_confirmation = external_confirmation(original)
    changed = _allowed_calendar("Create a different calendar event")
    with pytest.raises(ConfirmationMismatchError):
        authorize_external_action(changed, old_confirmation, MemoryConsumptionPort(), at=NOW)


def test_external_payload_is_detached_from_mutable_mapping() -> None:
    payload: dict[str, object] = {"title": "Call Li"}
    candidate = ActionCandidate.external_action_candidate(
        "external-1",
        "Create event",
        scope="calendar.events.create",
        payload=payload,
        **CONTEXT,
    )
    payload["title"] = "Changed later"
    candidate = allowed_action(candidate)
    port = MemoryConsumptionPort()
    authorization = authorize_external_action(
        candidate,
        external_confirmation(candidate),
        port,
        at=NOW,
    )
    assert authorization.payload == {"title": "Call Li"}


def test_model_cannot_authorize_external_action() -> None:
    candidate = _allowed_calendar()
    confirmation = external_confirmation(candidate, actor=ConfirmationActor.MODEL)
    with pytest.raises(ConfirmationRequiredError):
        authorize_external_action(candidate, confirmation, MemoryConsumptionPort(), at=NOW)


def test_authorization_identity_is_stable_for_same_confirmation() -> None:
    candidate = _allowed_calendar()
    confirmation = external_confirmation(candidate, generation=7)
    port = MemoryConsumptionPort()
    first = authorize_external_action(candidate, confirmation, port, at=NOW)
    rebuilt = authorize_external_action(candidate, confirmation, port, at=NOW)
    assert first.authorization_id == rebuilt.authorization_id
    assert first.generation == rebuilt.generation == 7


def test_exact_rebuild_later_restores_original_registration_snapshot() -> None:
    candidate = _allowed_calendar()
    confirmation = external_confirmation(candidate)
    port = MemoryConsumptionPort()
    first = authorize_external_action(candidate, confirmation, port, at=NOW)

    rebuilt = authorize_external_action(
        candidate,
        confirmation,
        port,
        at=NOW + timedelta(minutes=1),
    )

    assert rebuilt.state is ExternalActionState.READY
    assert rebuilt.registration_snapshot == first.registration_snapshot
    assert rebuilt.registration_snapshot.authorized_at == NOW


def test_authorization_identity_separates_policy_snapshots() -> None:
    first_candidate = _allowed_calendar()
    changed_context: ActionContext = {
        **CONTEXT,
        "policy_version": "action-policy-v3",
        "policy_snapshot": "v3",
    }
    second_candidate = ActionCandidate.external_action_candidate(
        "external-1",
        "Create one calendar event",
        scope="calendar.events.create",
        payload={"title": "Call Li", "starts_at": "2026-08-24T10:00:00+08:00"},
        **changed_context,
    )
    second_candidate = allowed_action(second_candidate)
    port = MemoryConsumptionPort()

    first = authorize_external_action(
        first_candidate,
        external_confirmation(first_candidate),
        port,
        at=NOW,
    )
    second = authorize_external_action(
        second_candidate,
        external_confirmation(second_candidate),
        port,
        at=NOW,
    )

    assert first_candidate.fingerprint == second_candidate.fingerprint
    assert first.authorization_id != second.authorization_id


def test_authorization_id_canonical_encoding_prevents_delimiter_collision() -> None:
    first_context: ActionContext = {**CONTEXT, "vault_id": "z"}
    second_context: ActionContext = {**CONTEXT, "vault_id": "y\x1fz"}
    first_candidate = ActionCandidate.external_action_candidate(
        "external-1",
        "Create one calendar event",
        scope="calendar.events.create",
        payload={"title": "Call Li"},
        **first_context,
    )
    second_candidate = ActionCandidate.external_action_candidate(
        "external-1",
        "Create one calendar event",
        scope="calendar.events.create",
        payload={"title": "Call Li"},
        **second_context,
    )
    first_candidate = allowed_action(first_candidate)
    second_candidate = allowed_action(second_candidate)

    first = authorize_external_action(
        first_candidate,
        external_confirmation(first_candidate, confirmation_id="x\x1fy"),
        MemoryConsumptionPort(),
        at=NOW,
    )
    second = authorize_external_action(
        second_candidate,
        external_confirmation(second_candidate, confirmation_id="x"),
        MemoryConsumptionPort(),
        at=NOW,
    )

    assert first.authorization_id != second.authorization_id


def test_rebuilt_authorization_cannot_bypass_persistent_atomic_consumption() -> None:
    candidate = _allowed_calendar()
    confirmation = external_confirmation(candidate)
    port = MemoryConsumptionPort()
    first = authorize_external_action(candidate, confirmation, port, at=NOW)
    rebuilt = authorize_external_action(candidate, confirmation, port, at=NOW)

    execution = record_external_action_execution(
        first,
        port,
        executed_at=NOW,
        receipt_id="calendar-receipt-1",
    )
    assert execution.authorization_id == first.authorization_id
    rehydrated_after_execution = authorize_external_action(candidate, confirmation, port, at=NOW)
    assert rehydrated_after_execution.state is ExternalActionState.EXECUTED
    assert not rehydrated_after_execution.is_executable

    with pytest.raises(ExternalActionAlreadyConsumedError):
        record_external_action_execution(
            rebuilt,
            port,
            executed_at=NOW + timedelta(seconds=1),
            receipt_id="calendar-receipt-2",
        )


def test_consumed_confirmation_cannot_reopen_lineage_with_new_generation() -> None:
    candidate = _allowed_calendar()
    confirmation = external_confirmation(candidate)
    port = MemoryConsumptionPort()
    authorization = authorize_external_action(candidate, confirmation, port, at=NOW)
    record_external_action_execution(
        authorization,
        port,
        executed_at=NOW,
        receipt_id="receipt-1",
    )
    changed_confirmation = replace(
        confirmation,
        user_confirmation=replace(confirmation.user_confirmation, generation=2),
    )

    with pytest.raises(ExternalActionAuthorizationLineageError):
        authorize_external_action(candidate, changed_confirmation, port, at=NOW)


def test_local_authorization_is_single_use() -> None:
    candidate = _allowed_calendar()
    port = MemoryConsumptionPort()
    authorization = authorize_external_action(
        candidate, external_confirmation(candidate), port, at=NOW
    )
    record_external_action_execution(
        authorization,
        port,
        executed_at=NOW,
        receipt_id="receipt-1",
    )
    assert authorization.state is ExternalActionState.EXECUTED
    with pytest.raises(ExternalActionAlreadyConsumedError):
        record_external_action_execution(
            authorization,
            port,
            executed_at=NOW,
            receipt_id="receipt-2",
        )


def test_expired_authorization_cannot_be_consumed() -> None:
    candidate = _calendar_candidate()
    candidate = candidate.with_safety_verdict(
        verdict_for_action(candidate, expires_at=NOW + timedelta(seconds=1))
    )
    port = MemoryConsumptionPort()
    authorization = authorize_external_action(
        candidate, external_confirmation(candidate), port, at=NOW
    )
    with pytest.raises(ExternalActionAuthorizationExpiredError):
        record_external_action_execution(
            authorization,
            port,
            executed_at=NOW + timedelta(seconds=1),
            receipt_id="receipt-1",
        )


def test_registered_expiry_cannot_be_extended_by_rebuilding_credentials() -> None:
    candidate = _calendar_candidate()
    candidate = candidate.with_safety_verdict(
        verdict_for_action(candidate, expires_at=NOW + timedelta(seconds=1))
    )
    confirmation = external_confirmation(candidate, expires_at=NOW + timedelta(seconds=1))
    port = MemoryConsumptionPort()
    authorization = authorize_external_action(candidate, confirmation, port, at=NOW)
    verdict = candidate.safety_verdict
    assert verdict is not None
    extended_candidate = candidate.with_safety_verdict(
        replace(verdict, expires_at=NOW + timedelta(minutes=30))
    )
    extended_confirmation = replace(
        confirmation,
        user_confirmation=replace(
            confirmation.user_confirmation,
            expires_at=NOW + timedelta(minutes=30),
        ),
    )

    with pytest.raises(ExternalActionAuthorizationLineageError):
        authorize_external_action(extended_candidate, extended_confirmation, port, at=NOW)

    persisted = port.consume_ready_authorization(
        authorization_id=authorization.authorization_id,
        authorization_snapshot_fingerprint=authorization.registration_snapshot.fingerprint,
        consumed_at=NOW + timedelta(seconds=1),
    )
    assert not persisted.transitioned


def test_lineage_freezes_each_credential_expiry_when_effective_expiry_is_unchanged() -> None:
    verdict_bound = _calendar_candidate()
    verdict_bound = verdict_bound.with_safety_verdict(
        verdict_for_action(verdict_bound, expires_at=NOW + timedelta(minutes=10))
    )
    early_confirmation = external_confirmation(
        verdict_bound,
        expires_at=NOW + timedelta(minutes=5),
    )
    verdict_port = MemoryConsumptionPort()
    authorize_external_action(verdict_bound, early_confirmation, verdict_port, at=NOW)
    verdict = verdict_bound.safety_verdict
    assert verdict is not None
    changed_verdict = verdict_bound.with_safety_verdict(
        replace(verdict, expires_at=NOW + timedelta(minutes=20))
    )
    with pytest.raises(ExternalActionAuthorizationLineageError):
        authorize_external_action(changed_verdict, early_confirmation, verdict_port, at=NOW)

    confirmation_bound = _calendar_candidate()
    confirmation_bound = confirmation_bound.with_safety_verdict(
        verdict_for_action(confirmation_bound, expires_at=NOW + timedelta(minutes=5))
    )
    confirmation = external_confirmation(
        confirmation_bound,
        expires_at=NOW + timedelta(minutes=10),
    )
    confirmation_port = MemoryConsumptionPort()
    authorize_external_action(confirmation_bound, confirmation, confirmation_port, at=NOW)
    changed_confirmation = replace(
        confirmation,
        user_confirmation=replace(
            confirmation.user_confirmation,
            expires_at=NOW + timedelta(minutes=20),
        ),
    )
    with pytest.raises(ExternalActionAuthorizationLineageError):
        authorize_external_action(
            confirmation_bound,
            changed_confirmation,
            confirmation_port,
            at=NOW,
        )


def test_revoked_authorization_cannot_be_consumed() -> None:
    candidate = _allowed_calendar()
    port = MemoryConsumptionPort()
    confirmation = external_confirmation(candidate)
    authorization = authorize_external_action(candidate, confirmation, port, at=NOW)
    revoke_external_action_authorization(authorization, port, revoked_at=NOW)
    assert authorization.state is ExternalActionState.REVOKED
    rebuilt = authorize_external_action(candidate, confirmation, port, at=NOW)
    assert rebuilt.state is ExternalActionState.REVOKED
    assert not rebuilt.is_executable
    with pytest.raises(ExternalActionAuthorizationRevokedError):
        record_external_action_execution(
            authorization,
            port,
            executed_at=NOW,
            receipt_id="receipt-1",
        )


def test_execution_timestamp_must_be_timezone_aware() -> None:
    candidate = _allowed_calendar()
    port = MemoryConsumptionPort()
    authorization = authorize_external_action(
        candidate, external_confirmation(candidate), port, at=NOW
    )
    with pytest.raises(InvalidActionCandidateError):
        record_external_action_execution(
            authorization,
            port,
            executed_at=datetime(2026, 8, 23, 12, 0),
            receipt_id="receipt-1",
        )


@pytest.mark.parametrize(
    ("state", "transitioned"),
    [
        (cast(ExternalActionState, "executed"), True),
        (ExternalActionState.EXECUTED, cast(bool, "false")),
        (ExternalActionState.READY, True),
    ],
)
def test_persistent_transition_result_is_fail_closed_at_runtime(
    state: ExternalActionState,
    transitioned: bool,
) -> None:
    with pytest.raises(InvalidActionCandidateError):
        ExternalAuthorizationTransition(state, transitioned)
