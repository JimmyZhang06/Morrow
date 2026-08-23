from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from typing import cast

import pytest

import life_coach.modules.action as action_module
from life_coach.modules.action import (
    ActionCandidate,
    ActionSafetyBlockedError,
    ActionSafetyOutcome,
    ActionSafetyRequiredError,
    ConfirmationActor,
    ConfirmationMismatchError,
    ConfirmationRequiredError,
    ExternalActionAlreadyConsumedError,
    ExternalActionAuthorization,
    ExternalActionAuthorizationLineageError,
    ExternalActionAuthorizationRevokedError,
    ExternalActionClaimedError,
    ExternalActionConfirmation,
    ExternalActionConnector,
    ExternalActionExecution,
    ExternalActionRequest,
    ExternalActionState,
    ExternalAuthorizationTransition,
    ExternalConnectorReceipt,
    InvalidActionCandidateError,
    authorize_external_action,
    confirm_candidate,
    execute_external_action,
    revoke_external_action_authorization,
)
from life_coach.modules.safety.orchestration import OrdinaryOperation
from tests.action.helpers import (
    ACTION_AUTHORITY,
    CLOCK,
    CONTEXT,
    NOW,
    SAFETY_AUTHORITY,
    ActionContext,
    FakeTrustedClock,
    MemoryConsumptionPort,
    RecordingConnector,
    allowed_action,
    confirmation_for_action,
    external_confirmation,
    permit_for_action,
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


def _authorize(
    candidate: ActionCandidate,
    *,
    port: MemoryConsumptionPort | None = None,
    confirmation: ExternalActionConfirmation | None = None,
    clock: FakeTrustedClock = CLOCK,
) -> ExternalActionAuthorization:
    resolved_port = port or MemoryConsumptionPort()
    return authorize_external_action(
        candidate,
        external_confirmation(candidate) if confirmation is None else confirmation,
        resolved_port,
        action_authority=ACTION_AUTHORITY,
        safety_authority=SAFETY_AUTHORITY,
        safety_permit=permit_for_action(candidate, OrdinaryOperation.EXTERNAL_ACTION_AUTHORIZATION),
        clock=clock,
    )


def _execute(
    authorization: ExternalActionAuthorization,
    port: MemoryConsumptionPort,
    connector: ExternalActionConnector,
    *,
    clock: FakeTrustedClock = CLOCK,
) -> ExternalActionExecution:
    return execute_external_action(
        authorization,
        port,
        connector,
        action_authority=ACTION_AUTHORITY,
        safety_authority=SAFETY_AUTHORITY,
        safety_permit=permit_for_action(
            authorization.candidate, OrdinaryOperation.EXTERNAL_ACTION_EXECUTION
        ),
        clock=clock,
    )


def test_external_action_is_not_executable_before_final_confirmation() -> None:
    candidate = _allowed_calendar()
    assert not candidate.is_executable
    with pytest.raises(ConfirmationRequiredError):
        confirm_candidate(
            candidate,
            confirmation_for_action(candidate),
            action_authority=ACTION_AUTHORITY,
            clock=CLOCK,
        )


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
        _authorize(candidate, confirmation=confirmation)


def test_external_ready_requires_current_exact_allowed_verdict() -> None:
    authorization = _authorize(_allowed_calendar())
    assert authorization.state is ExternalActionState.READY
    assert authorization.scope == "calendar.events.create"
    assert authorization.payload["title"] == "Call Li"


def test_external_ready_fails_closed_without_or_with_blocked_verdict() -> None:
    missing = _calendar_candidate()
    with pytest.raises(ActionSafetyRequiredError):
        _authorize(missing)

    blocked = _calendar_candidate()
    blocked = blocked.with_safety_verdict(verdict_for_action(blocked, ActionSafetyOutcome.BLOCKED))
    with pytest.raises(ActionSafetyBlockedError):
        _authorize(blocked)


def test_same_id_changed_description_rejects_old_external_confirmation() -> None:
    original = _allowed_calendar("Create Li calendar event")
    old_confirmation = external_confirmation(original)
    changed = _allowed_calendar("Create a different calendar event")
    with pytest.raises(ConfirmationMismatchError):
        _authorize(changed, confirmation=old_confirmation)


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
    authorization = _authorize(allowed_action(candidate))
    assert authorization.payload == {"title": "Call Li"}


def test_model_cannot_authorize_external_action() -> None:
    candidate = _allowed_calendar()
    confirmation = external_confirmation(candidate, actor=ConfirmationActor.MODEL)
    with pytest.raises(ConfirmationRequiredError):
        _authorize(candidate, confirmation=confirmation)


def test_authorization_identity_is_stable_for_same_confirmation() -> None:
    candidate = _allowed_calendar()
    confirmation = external_confirmation(candidate, generation=7)
    port = MemoryConsumptionPort()
    first = _authorize(candidate, confirmation=confirmation, port=port)
    rebuilt = _authorize(candidate, confirmation=confirmation, port=port)
    assert first.authorization_id == rebuilt.authorization_id
    assert first.generation == rebuilt.generation == 7


def test_exact_rebuild_restores_original_registration_snapshot() -> None:
    candidate = _allowed_calendar()
    confirmation = external_confirmation(candidate)
    port = MemoryConsumptionPort()
    first = _authorize(candidate, confirmation=confirmation, port=port)
    rebuilt = _authorize(
        candidate,
        confirmation=confirmation,
        port=port,
        clock=FakeTrustedClock(NOW + timedelta(minutes=1)),
    )
    assert rebuilt.state is ExternalActionState.READY
    assert rebuilt.registration_snapshot == first.registration_snapshot
    assert rebuilt.registration_snapshot.authorized_at == NOW


def test_authorization_identity_separates_policy_snapshots() -> None:
    first_candidate = _allowed_calendar()
    changed_context: ActionContext = {
        **CONTEXT,
        "policy_version": "action-policy-v4",
        "policy_snapshot": "v4",
    }
    second_candidate = allowed_action(
        ActionCandidate.external_action_candidate(
            "external-1",
            "Create one calendar event",
            scope="calendar.events.create",
            payload={"title": "Call Li", "starts_at": "2026-08-24T10:00:00+08:00"},
            **changed_context,
        )
    )
    first = _authorize(first_candidate)
    second = _authorize(second_candidate)
    assert first_candidate.fingerprint == second_candidate.fingerprint
    assert first.authorization_id != second.authorization_id


def test_authorization_id_canonical_encoding_prevents_delimiter_collision() -> None:
    first_context: ActionContext = {**CONTEXT, "vault_id": "z"}
    second_context: ActionContext = {**CONTEXT, "vault_id": "y\x1fz"}
    first_candidate = allowed_action(
        ActionCandidate.external_action_candidate(
            "external-1",
            "Create one calendar event",
            scope="calendar.events.create",
            payload={"title": "Call Li"},
            **first_context,
        )
    )
    second_candidate = allowed_action(
        ActionCandidate.external_action_candidate(
            "external-1",
            "Create one calendar event",
            scope="calendar.events.create",
            payload={"title": "Call Li"},
            **second_context,
        )
    )
    first = _authorize(
        first_candidate,
        confirmation=external_confirmation(first_candidate, confirmation_id="x\x1fy"),
    )
    second = _authorize(
        second_candidate,
        confirmation=external_confirmation(second_candidate, confirmation_id="x"),
    )
    assert first.authorization_id != second.authorization_id


def test_claim_happens_before_connector_and_completion() -> None:
    events: list[str] = []
    port = MemoryConsumptionPort(events)
    connector = RecordingConnector(events=events)
    authorization = _authorize(_allowed_calendar(), port=port)
    execution = _execute(authorization, port, connector)
    assert events[-3:] == ["claim", "connector", "complete"]
    assert execution.state is ExternalActionState.EXECUTED
    assert authorization.state is ExternalActionState.EXECUTED
    assert connector.calls[0].claim_id == execution.claim_id


def test_connector_failure_leaves_claimed_and_cannot_retry() -> None:
    candidate = _allowed_calendar()
    confirmation = external_confirmation(candidate)
    port = MemoryConsumptionPort()
    authorization = _authorize(candidate, confirmation=confirmation, port=port)
    failing = RecordingConnector(error=RuntimeError("connector unavailable"))
    with pytest.raises(RuntimeError, match="connector unavailable"):
        _execute(authorization, port, failing)
    assert authorization.state is ExternalActionState.CLAIMED
    assert port.state(authorization.authorization_id) is ExternalActionState.CLAIMED

    rebuilt = _authorize(candidate, confirmation=confirmation, port=port)
    retry_connector = RecordingConnector()
    with pytest.raises(ExternalActionClaimedError):
        _execute(rebuilt, port, retry_connector)
    assert retry_connector.calls == []


def test_rebuilt_authorization_cannot_execute_connector_twice() -> None:
    candidate = _allowed_calendar()
    confirmation = external_confirmation(candidate)
    port = MemoryConsumptionPort()
    first = _authorize(candidate, confirmation=confirmation, port=port)
    rebuilt = _authorize(candidate, confirmation=confirmation, port=port)
    connector = RecordingConnector()
    _execute(first, port, connector)
    with pytest.raises(ExternalActionAlreadyConsumedError):
        _execute(rebuilt, port, connector)
    assert len(connector.calls) == 1


def test_double_concurrent_execution_invokes_connector_once() -> None:
    candidate = _allowed_calendar()
    confirmation = external_confirmation(candidate)
    port = MemoryConsumptionPort()
    first = _authorize(candidate, confirmation=confirmation, port=port)
    second = _authorize(candidate, confirmation=confirmation, port=port)
    connector = RecordingConnector()

    def run(
        authorization: ExternalActionAuthorization,
        clock: FakeTrustedClock,
    ) -> object:
        try:
            return _execute(authorization, port, connector, clock=clock)
        except (ExternalActionClaimedError, ExternalActionAlreadyConsumedError) as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                run,
                (first, second),
                (FakeTrustedClock(), FakeTrustedClock()),
            )
        )
    assert len(connector.calls) == 1
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert port.state(first.authorization_id) is ExternalActionState.EXECUTED


class _BlockingConnector:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.calls = 0

    def execute(self, request: ExternalActionRequest) -> ExternalConnectorReceipt:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=5)
        return ExternalConnectorReceipt(f"connector:{request.claim_id}")


def test_claim_wins_ready_revoke_race() -> None:
    candidate = _allowed_calendar()
    confirmation = external_confirmation(candidate)
    port = MemoryConsumptionPort()
    executing = _authorize(candidate, confirmation=confirmation, port=port)
    revoking = _authorize(candidate, confirmation=confirmation, port=port)
    connector = _BlockingConnector()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            execute_external_action,
            executing,
            port,
            connector,
            action_authority=ACTION_AUTHORITY,
            safety_authority=SAFETY_AUTHORITY,
            safety_permit=permit_for_action(candidate, OrdinaryOperation.EXTERNAL_ACTION_EXECUTION),
            clock=FakeTrustedClock(),
        )
        assert connector.entered.wait(timeout=5)
        with pytest.raises(ExternalActionClaimedError):
            revoke_external_action_authorization(revoking, port, clock=FakeTrustedClock())
        connector.release.set()
        execution = future.result(timeout=5)
    assert execution.state is ExternalActionState.EXECUTED
    assert connector.calls == 1


def test_revoke_wins_ready_claim_race() -> None:
    candidate = _allowed_calendar()
    port = MemoryConsumptionPort()
    authorization = _authorize(candidate, port=port)
    revoke_external_action_authorization(authorization, port, clock=FakeTrustedClock())
    connector = RecordingConnector()
    with pytest.raises(ExternalActionAuthorizationRevokedError):
        _execute(authorization, port, connector, clock=FakeTrustedClock())
    assert connector.calls == []
    assert port.state(authorization.authorization_id) is ExternalActionState.REVOKED


def test_consumed_confirmation_cannot_reopen_lineage_with_new_generation() -> None:
    candidate = _allowed_calendar()
    confirmation = external_confirmation(candidate)
    port = MemoryConsumptionPort()
    authorization = _authorize(candidate, confirmation=confirmation, port=port)
    _execute(authorization, port, RecordingConnector())
    changed_confirmation = external_confirmation(candidate, generation=2)
    with pytest.raises(ExternalActionAuthorizationLineageError):
        _authorize(candidate, confirmation=changed_confirmation, port=port)


def test_registered_expiry_cannot_be_extended_by_rebuilding_credentials() -> None:
    candidate = _calendar_candidate()
    candidate = candidate.with_safety_verdict(
        verdict_for_action(candidate, expires_at=NOW + timedelta(seconds=1))
    )
    confirmation = external_confirmation(candidate, expires_at=NOW + timedelta(seconds=1))
    port = MemoryConsumptionPort()
    _authorize(candidate, confirmation=confirmation, port=port)

    extended_candidate = candidate.with_safety_verdict(
        verdict_for_action(candidate, expires_at=NOW + timedelta(minutes=30))
    )
    extended_confirmation = external_confirmation(
        extended_candidate, expires_at=NOW + timedelta(minutes=30)
    )
    with pytest.raises(ExternalActionAuthorizationLineageError):
        _authorize(extended_candidate, confirmation=extended_confirmation, port=port)


def test_credential_expiry_is_immutable_lineage_when_effective_expiry_matches() -> None:
    candidate = _calendar_candidate()
    candidate = candidate.with_safety_verdict(
        verdict_for_action(candidate, expires_at=NOW + timedelta(minutes=10))
    )
    confirmation = external_confirmation(candidate, expires_at=NOW + timedelta(minutes=5))
    port = MemoryConsumptionPort()
    _authorize(candidate, confirmation=confirmation, port=port)

    changed = candidate.with_safety_verdict(
        verdict_for_action(candidate, expires_at=NOW + timedelta(minutes=20))
    )
    changed_confirmation = external_confirmation(changed, expires_at=NOW + timedelta(minutes=5))
    with pytest.raises(ExternalActionAuthorizationLineageError):
        _authorize(changed, confirmation=changed_confirmation, port=port)


def test_revoked_authorization_stays_revoked_after_rebuild() -> None:
    candidate = _allowed_calendar()
    confirmation = external_confirmation(candidate)
    port = MemoryConsumptionPort()
    authorization = _authorize(candidate, confirmation=confirmation, port=port)
    revoke_external_action_authorization(authorization, port, clock=FakeTrustedClock())
    rebuilt = _authorize(candidate, confirmation=confirmation, port=port)
    assert rebuilt.state is ExternalActionState.REVOKED
    connector = RecordingConnector()
    with pytest.raises(ExternalActionAuthorizationRevokedError):
        _execute(rebuilt, port, connector, clock=FakeTrustedClock())
    assert connector.calls == []


def test_old_connector_before_claim_record_api_no_longer_exists() -> None:
    assert not hasattr(action_module, "record_external_action_execution")


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
