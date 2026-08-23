from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from life_coach.modules.action import ActionCandidate, to_task
from life_coach.modules.reflection import ReflectionPolicy, ReflectionState
from life_coach.modules.safety.gateway import RuleBasedSafetyGateway, SafetyState
from life_coach.modules.safety.orchestration import (
    OrdinaryFlowBlockedError,
    OrdinaryOperation,
    SafetyOrchestrationContract,
    StaleSafetyDecisionError,
)
from life_coach.modules.safety.output_policy import (
    NonDiagnosticOutputPolicy,
    OutputVerificationStatus,
    UnsafeGeneratedOutputError,
)

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)


class VerifiedSemanticOutput:
    def __init__(self) -> None:
        self.seen_texts: list[str] = []

    def verify(self, text: str) -> OutputVerificationStatus:
        self.seen_texts.append(text)
        return OutputVerificationStatus.VERIFIED


def safety_state(**overrides: object) -> SafetyState:
    values: dict[str, object] = {
        "session_id": "session",
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
    }
    values.update(overrides)
    return SafetyState(**values)  # type: ignore[arg-type]


def test_crisis_precheck_blocks_ordinary_work_then_checks_safety_output() -> None:
    decision = RuleBasedSafetyGateway().evaluate(
        safety_state(possible_current_danger=True),
        at=NOW,
    )
    verifier = VerifiedSemanticOutput()
    contract = SafetyOrchestrationContract(
        decision,
        NonDiagnosticOutputPolicy(semantic_verifier=verifier),
    )

    reflection_state = ReflectionState(session_id="reflection-session")
    with pytest.raises(OrdinaryFlowBlockedError):
        contract.execute_ordinary(
            OrdinaryOperation.REFLECTION_DEEPENING,
            at=NOW,
            execute=lambda _permit: ReflectionPolicy().assess(reflection_state),
        )

    task_candidate = ActionCandidate.commitment(
        "candidate-1",
        "Send the draft",
        vault_id="vault-1",
        principal_id="user-1",
        purpose="coach.action",
        policy_version="action-policy-v2",
        policy_snapshot="snapshot-1",
    )
    with pytest.raises(OrdinaryFlowBlockedError):
        contract.execute_ordinary(
            OrdinaryOperation.TODO_CREATION,
            at=NOW,
            execute=lambda _permit: to_task(task_candidate, at=NOW),
        )

    for operation in (
        OrdinaryOperation.EXPERIMENT_ACCEPTANCE,
        OrdinaryOperation.EXTERNAL_ACTION_AUTHORIZATION,
    ):
        with pytest.raises(OrdinaryFlowBlockedError):
            contract.execute_ordinary(
                operation,
                at=NOW,
                execute=lambda _permit: pytest.fail("blocked operation was executed"),
            )

    with pytest.raises(UnsafeGeneratedOutputError):
        contract.release_output("Emergency services are on the way.")

    released = contract.release_output(
        "If you may be in immediate danger, contact local emergency services now."
    )
    assert released.startswith("If you may be in immediate danger")
    assert verifier.seen_texts == [released]


def test_ordinary_route_issues_operation_specific_current_permit() -> None:
    decision = RuleBasedSafetyGateway().evaluate(safety_state(), at=NOW)
    contract = SafetyOrchestrationContract(decision)

    permit = contract.authorize_ordinary(OrdinaryOperation.TODO_CREATION, at=NOW)

    assert permit.operation is OrdinaryOperation.TODO_CREATION
    assert permit.is_current(at=NOW)
    assert not permit.is_current(at=permit.expires_at)


def test_ordinary_route_executes_only_after_issuing_a_current_permit() -> None:
    decision = RuleBasedSafetyGateway().evaluate(safety_state(), at=NOW)
    contract = SafetyOrchestrationContract(decision)

    result = contract.execute_ordinary(
        OrdinaryOperation.REFLECTION_DEEPENING,
        at=NOW,
        execute=lambda permit: (permit.operation, permit.is_current(at=NOW)),
    )

    assert result == (OrdinaryOperation.REFLECTION_DEEPENING, True)


def test_stale_decision_cannot_authorize_ordinary_work() -> None:
    state = safety_state()
    decision = RuleBasedSafetyGateway().evaluate(state, at=NOW)
    contract = SafetyOrchestrationContract(decision)

    with pytest.raises(StaleSafetyDecisionError):
        contract.authorize_ordinary(
            OrdinaryOperation.REFLECTION_DEEPENING,
            at=state.expires_at,
        )


def test_missing_semantic_output_verification_fails_closed() -> None:
    decision = RuleBasedSafetyGateway().evaluate(safety_state(), at=NOW)

    with pytest.raises(UnsafeGeneratedOutputError):
        SafetyOrchestrationContract(decision).release_output(
            "A tentative and non-diagnostic reflection."
        )
