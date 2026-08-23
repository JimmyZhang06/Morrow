from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from life_coach.modules.safety.authority import (
    SafetyAuthorityReceipt,
    SafetyAuthorityVerificationError,
    SafetyBinding,
    SafetyDecisionClaims,
    SafetyPermitClaims,
)
from life_coach.modules.safety.gateway import (
    RuleBasedSafetyGateway,
    SafetyRoute,
    SafetyState,
)
from life_coach.modules.safety.orchestration import (
    OrdinaryFlowBlockedError,
    OrdinaryFlowPermit,
    OrdinaryOperation,
    SafetyBindingMismatchError,
    SafetyOrchestrationContract,
    StaleSafetyDecisionError,
)
from life_coach.modules.safety.output_policy import (
    NonDiagnosticOutputPolicy,
    UnsafeGeneratedOutputError,
)

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)
SAFETY_POLICY_GENERATION = "safety-policy-7"


class ClockRollbackDetectedError(RuntimeError):
    pass


class FakeTrustedClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current
        self.calls = 0
        self._last_returned: datetime | None = None

    def now(self) -> datetime:
        self.calls += 1
        if self._last_returned is not None and self.current < self._last_returned:
            raise ClockRollbackDetectedError("trusted clock rolled back")
        self._last_returned = self.current
        return self.current


class FakeSafetyAuthority:
    def __init__(self) -> None:
        self._next_receipt = 0
        self.decisions: dict[str, SafetyDecisionClaims] = {}
        self.permits: dict[str, SafetyPermitClaims] = {}
        self.consumed_permits: set[str] = set()

    def _receipt(self, kind: str) -> SafetyAuthorityReceipt:
        self._next_receipt += 1
        return SafetyAuthorityReceipt(f"{kind}-receipt-{self._next_receipt}")

    def issue_decision(self, claims: SafetyDecisionClaims) -> SafetyAuthorityReceipt:
        receipt = self._receipt("decision")
        self.decisions[receipt.value] = claims
        return receipt

    def verify_decision(
        self,
        claims: SafetyDecisionClaims,
        receipt: SafetyAuthorityReceipt,
    ) -> bool:
        return self.decisions.get(receipt.value) == claims

    def issue_permit(self, claims: SafetyPermitClaims) -> SafetyAuthorityReceipt:
        parent = self.decisions.get(claims.decision_receipt.value)
        if (
            parent is None
            or parent.route != SafetyRoute.ORDINARY_COACH.value
            or parent.binding != claims.binding
            or claims.policy_generation != parent.binding.policy_generation
            or claims.issued_at < parent.evaluated_at
            or claims.expires_at > parent.expires_at
        ):
            raise SafetyAuthorityVerificationError("invalid parent decision for permit")
        receipt = self._receipt("permit")
        self.permits[receipt.value] = claims
        return receipt

    def verify_permit(
        self,
        claims: SafetyPermitClaims,
        receipt: SafetyAuthorityReceipt,
    ) -> bool:
        return (
            receipt.value not in self.consumed_permits and self.permits.get(receipt.value) == claims
        )

    def verify_and_consume_permit(
        self,
        claims: SafetyPermitClaims,
        receipt: SafetyAuthorityReceipt,
    ) -> bool:
        if not self.verify_permit(claims, receipt):
            return False
        self.consumed_permits.add(receipt.value)
        return True


def safety_state(**overrides: object) -> SafetyState:
    values: dict[str, object] = {
        "vault_id": "vault-1",
        "principal_id": "user-1",
        "session_id": "session-1",
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
    }
    values.update(overrides)
    return SafetyState(**values)  # type: ignore[arg-type]


def setup_decision(
    safety_state_value: SafetyState | None = None,
) -> tuple[object, FakeTrustedClock, FakeSafetyAuthority]:
    clock = FakeTrustedClock()
    authority = FakeSafetyAuthority()
    gateway = RuleBasedSafetyGateway(clock, authority, SAFETY_POLICY_GENERATION)
    return gateway.evaluate(safety_state_value or safety_state()), clock, authority


def contract_for(
    decision: object,
    clock: FakeTrustedClock,
    authority: FakeSafetyAuthority,
    *,
    expected_binding: SafetyBinding,
) -> SafetyOrchestrationContract:
    from life_coach.modules.safety.gateway import SafetyDecision

    assert isinstance(decision, SafetyDecision)
    output_policy = NonDiagnosticOutputPolicy(
        vault_id=expected_binding.vault_id,
        session_id=expected_binding.session_id,
        policy_generation=7,
        clock=clock,
    )
    return SafetyOrchestrationContract(
        decision=decision,
        expected_binding=expected_binding,
        clock=clock,
        authority=authority,
        output_policy=output_policy,
    )


def test_crisis_precheck_blocks_every_ordinary_operation_without_callback() -> None:
    decision, clock, authority = setup_decision(safety_state(possible_current_danger=True))
    from life_coach.modules.safety.gateway import SafetyDecision

    assert isinstance(decision, SafetyDecision)
    contract = contract_for(
        decision,
        clock,
        authority,
        expected_binding=decision.binding,
    )

    for operation in OrdinaryOperation:
        with pytest.raises(OrdinaryFlowBlockedError):
            contract.execute_ordinary(
                operation,
                execute=lambda _permit: pytest.fail("crisis callback was executed"),
            )

    assert authority.permits == {}


def test_authorize_reads_clock_once_and_issues_exact_unconsumed_permit() -> None:
    decision, clock, authority = setup_decision()
    from life_coach.modules.safety.gateway import SafetyDecision

    assert isinstance(decision, SafetyDecision)
    contract = contract_for(
        decision,
        clock,
        authority,
        expected_binding=decision.binding,
    )
    calls_before = clock.calls

    permit = contract.authorize_ordinary(OrdinaryOperation.TODO_CREATION)

    assert clock.calls == calls_before + 1
    assert permit.binding == decision.binding
    assert permit.operation is OrdinaryOperation.TODO_CREATION
    assert permit.policy_generation == decision.binding.policy_generation
    assert permit.expires_at == decision.expires_at
    assert permit.claims.decision_receipt == decision.authority_receipt
    assert authority.verify_permit(permit.claims, permit.authority_receipt)
    assert permit.authority_receipt.value not in authority.consumed_permits


def test_execute_reads_clock_once_and_passes_verified_unconsumed_permit() -> None:
    decision, clock, authority = setup_decision()
    from life_coach.modules.safety.gateway import SafetyDecision

    assert isinstance(decision, SafetyDecision)
    contract = contract_for(
        decision,
        clock,
        authority,
        expected_binding=decision.binding,
    )
    calls_before = clock.calls

    result = contract.execute_ordinary(
        OrdinaryOperation.IF_THEN_PLAN_ACCEPTANCE,
        execute=lambda permit: (
            permit.operation,
            authority.verify_permit(permit.claims, permit.authority_receipt),
        ),
    )

    assert clock.calls == calls_before + 1
    assert result == (OrdinaryOperation.IF_THEN_PLAN_ACCEPTANCE, True)
    assert authority.consumed_permits == set()


def test_action_boundary_can_atomically_consume_and_replay_is_rejected() -> None:
    decision, clock, authority = setup_decision()
    from life_coach.modules.safety.gateway import SafetyDecision

    assert isinstance(decision, SafetyDecision)
    contract = contract_for(
        decision,
        clock,
        authority,
        expected_binding=decision.binding,
    )
    permit = contract.authorize_ordinary(OrdinaryOperation.EXTERNAL_ACTION_EXECUTION)

    assert authority.verify_and_consume_permit(permit.claims, permit.authority_receipt)
    assert not authority.verify_and_consume_permit(permit.claims, permit.authority_receipt)
    assert not authority.verify_permit(permit.claims, permit.authority_receipt)


def test_publicly_constructed_permit_is_rejected_by_authority() -> None:
    decision, clock, authority = setup_decision()
    from life_coach.modules.safety.gateway import SafetyDecision

    assert isinstance(decision, SafetyDecision)
    contract = contract_for(
        decision,
        clock,
        authority,
        expected_binding=decision.binding,
    )
    issued = contract.authorize_ordinary(OrdinaryOperation.TODO_CREATION)
    handmade = OrdinaryFlowPermit(
        claims=issued.claims,
        authority_receipt=SafetyAuthorityReceipt("handmade-permit"),
    )

    assert not authority.verify_permit(handmade.claims, handmade.authority_receipt)


@pytest.mark.parametrize(
    ("binding_field", "changed_value"),
    [
        ("vault_id", "vault-attacker"),
        ("principal_id", "user-attacker"),
        ("session_id", "session-attacker"),
        ("input_fingerprint", "b" * 64),
    ],
)
def test_permit_cannot_replay_across_binding_scope(
    binding_field: str,
    changed_value: str,
) -> None:
    decision, clock, authority = setup_decision()
    from life_coach.modules.safety.gateway import SafetyDecision

    assert isinstance(decision, SafetyDecision)
    contract = contract_for(
        decision,
        clock,
        authority,
        expected_binding=decision.binding,
    )
    permit = contract.authorize_ordinary(OrdinaryOperation.TODO_CREATION)
    attacker_binding = replace(decision.binding, **{binding_field: changed_value})
    changed_claims = replace(
        permit.claims,
        binding=attacker_binding,
        policy_generation=attacker_binding.policy_generation,
    )

    assert not authority.verify_permit(changed_claims, permit.authority_receipt)
    assert not authority.verify_and_consume_permit(
        changed_claims,
        permit.authority_receipt,
    )


def test_stolen_valid_decision_is_rejected_against_current_request_binding() -> None:
    decision, clock, authority = setup_decision()
    from life_coach.modules.safety.gateway import SafetyDecision

    assert isinstance(decision, SafetyDecision)
    attacker_binding = replace(
        decision.binding,
        session_id="session-attacker",
        input_fingerprint="b" * 64,
    )
    contract = contract_for(
        decision,
        clock,
        authority,
        expected_binding=attacker_binding,
    )

    with pytest.raises(SafetyBindingMismatchError):
        contract.execute_ordinary(
            OrdinaryOperation.REFLECTION_DEEPENING,
            execute=lambda _permit: pytest.fail("stolen decision entered callback"),
        )


def test_forged_decision_receipt_is_rejected_before_callback() -> None:
    decision, clock, authority = setup_decision()
    from life_coach.modules.safety.gateway import SafetyDecision

    assert isinstance(decision, SafetyDecision)
    forged = replace(
        decision,
        authority_receipt=SafetyAuthorityReceipt("forged-decision"),
    )
    contract = contract_for(
        forged,
        clock,
        authority,
        expected_binding=decision.binding,
    )

    with pytest.raises(SafetyAuthorityVerificationError):
        contract.execute_ordinary(
            OrdinaryOperation.REFLECTION_DEEPENING,
            execute=lambda _permit: pytest.fail("forged decision entered callback"),
        )


def test_authority_refuses_permit_for_crisis_parent_even_if_called_directly() -> None:
    decision, _, authority = setup_decision(safety_state(possible_current_danger=True))
    from life_coach.modules.safety.gateway import SafetyDecision

    assert isinstance(decision, SafetyDecision)
    claims = SafetyPermitClaims(
        binding=decision.binding,
        operation=OrdinaryOperation.TODO_CREATION.value,
        policy_generation=decision.binding.policy_generation,
        issued_at=NOW,
        expires_at=decision.expires_at,
        decision_receipt=decision.authority_receipt,
    )

    with pytest.raises(SafetyAuthorityVerificationError):
        authority.issue_permit(claims)


def test_stale_decision_cannot_authorize_ordinary_work() -> None:
    safety_state_value = safety_state()
    decision, clock, authority = setup_decision(safety_state_value)
    from life_coach.modules.safety.gateway import SafetyDecision

    assert isinstance(decision, SafetyDecision)
    contract = contract_for(
        decision,
        clock,
        authority,
        expected_binding=decision.binding,
    )
    clock.current = safety_state_value.expires_at

    with pytest.raises(StaleSafetyDecisionError):
        contract.authorize_ordinary(OrdinaryOperation.REFLECTION_DEEPENING)


def test_clock_rollback_object_blocks_orchestration() -> None:
    decision, clock, authority = setup_decision()
    from life_coach.modules.safety.gateway import SafetyDecision

    assert isinstance(decision, SafetyDecision)
    contract = contract_for(
        decision,
        clock,
        authority,
        expected_binding=decision.binding,
    )
    clock.current = NOW - timedelta(seconds=1)

    with pytest.raises(ClockRollbackDetectedError):
        contract.authorize_ordinary(OrdinaryOperation.TODO_CREATION)


def test_execute_no_longer_accepts_caller_controlled_time() -> None:
    decision, clock, authority = setup_decision()
    from life_coach.modules.safety.gateway import SafetyDecision

    assert isinstance(decision, SafetyDecision)
    contract = contract_for(
        decision,
        clock,
        authority,
        expected_binding=decision.binding,
    )

    with pytest.raises(TypeError):
        contract.execute_ordinary(  # type: ignore[call-arg]
            OrdinaryOperation.TODO_CREATION,
            at=NOW,
            execute=lambda _permit: None,
        )


def test_missing_semantic_output_verification_fails_closed() -> None:
    decision, clock, authority = setup_decision()
    from life_coach.modules.safety.gateway import SafetyDecision

    assert isinstance(decision, SafetyDecision)
    contract = contract_for(
        decision,
        clock,
        authority,
        expected_binding=decision.binding,
    )

    with pytest.raises(UnsafeGeneratedOutputError):
        contract.release_output("A tentative and non-diagnostic reflection.")


def test_output_policy_cannot_replay_across_safety_session() -> None:
    decision, clock, authority = setup_decision()
    from life_coach.modules.safety.gateway import SafetyDecision

    assert isinstance(decision, SafetyDecision)
    wrong_scope_policy = NonDiagnosticOutputPolicy(
        vault_id=decision.binding.vault_id,
        session_id="session-attacker",
        policy_generation=7,
        clock=clock,
    )

    with pytest.raises(SafetyBindingMismatchError):
        SafetyOrchestrationContract(
            decision=decision,
            expected_binding=decision.binding,
            clock=clock,
            authority=authority,
            output_policy=wrong_scope_policy,
        )
