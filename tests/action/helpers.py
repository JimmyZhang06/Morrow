from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count
from threading import Lock
from typing import TypedDict

from life_coach.modules.action import (
    ActionAuthorityClaims,
    ActionCandidate,
    ActionSafetyOutcome,
    ActionSafetyVerdict,
    ConfirmationActor,
    ExternalActionClaim,
    ExternalActionConfirmation,
    ExternalActionRequest,
    ExternalActionSpec,
    ExternalActionState,
    ExternalAuthorizationRecord,
    ExternalAuthorizationSnapshot,
    ExternalAuthorizationTransition,
    ExternalClaimTransition,
    ExternalConnectorReceipt,
    GoalCandidate,
    IfThenPlanCandidate,
    OpaqueActionReceipt,
    UserConfirmation,
)
from life_coach.modules.safety.authority import (
    SafetyAuthorityReceipt,
    SafetyBinding,
    SafetyDecisionClaims,
    SafetyPermitClaims,
)
from life_coach.modules.safety.orchestration import OrdinaryFlowPermit, OrdinaryOperation

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
VALID_UNTIL = NOW + timedelta(minutes=30)


class ActionContext(TypedDict):
    vault_id: str
    principal_id: str
    session_id: str
    safety_input_fingerprint: str
    safety_policy_generation: str
    purpose: str
    policy_version: str
    policy_snapshot: str


CONTEXT: ActionContext = {
    "vault_id": "vault-1",
    "principal_id": "user-1",
    "session_id": "session-1",
    "safety_input_fingerprint": "a" * 64,
    "safety_policy_generation": "safety-policy-7",
    "purpose": "coach.action",
    "policy_version": "action-policy-v3",
    "policy_snapshot": "sha256:action-policy-v3-snapshot",
}


class FakeTrustedClock:
    def __init__(self, instant: datetime = NOW) -> None:
        self.instant = instant
        self.calls = 0
        self._last: datetime | None = None

    def now(self) -> datetime:
        self.calls += 1
        if self._last is not None and self.instant < self._last:
            raise RuntimeError("trusted clock rollback detected")
        self._last = self.instant
        return self.instant

    def set(self, instant: datetime) -> None:
        self.instant = instant


CLOCK = FakeTrustedClock()


class MemoryActionAuthority:
    def __init__(self) -> None:
        self._claims: dict[str, ActionAuthorityClaims] = {}
        self._lock = Lock()
        self._ids = count(1)

    def new_receipt(self, prefix: str) -> OpaqueActionReceipt:
        with self._lock:
            return OpaqueActionReceipt(f"{prefix}-{next(self._ids)}")

    def trust(self, receipt: OpaqueActionReceipt, claims: ActionAuthorityClaims) -> None:
        with self._lock:
            self._claims[receipt.receipt_id] = claims

    def verify_receipt(
        self,
        receipt: OpaqueActionReceipt,
        *,
        expected_claims: ActionAuthorityClaims,
        current_time: datetime,
    ) -> bool:
        with self._lock:
            stored = self._claims.get(receipt.receipt_id)
        return (
            stored == expected_claims
            and expected_claims.issued_at <= current_time < expected_claims.expires_at
        )


ACTION_AUTHORITY = MemoryActionAuthority()


class MemorySafetyAuthority:
    def __init__(self) -> None:
        self._decisions: dict[str, SafetyDecisionClaims] = {}
        self._permits: dict[str, SafetyPermitClaims] = {}
        self._consumed: set[str] = set()
        self._lock = Lock()
        self._ids = count(1)

    def _receipt(self, prefix: str) -> SafetyAuthorityReceipt:
        return SafetyAuthorityReceipt(f"{prefix}-{next(self._ids)}")

    def issue_decision(self, claims: SafetyDecisionClaims) -> SafetyAuthorityReceipt:
        with self._lock:
            receipt = self._receipt("decision")
            self._decisions[receipt.value] = claims
            return receipt

    def verify_decision(
        self,
        claims: SafetyDecisionClaims,
        receipt: SafetyAuthorityReceipt,
    ) -> bool:
        with self._lock:
            return self._decisions.get(receipt.value) == claims

    def issue_permit(self, claims: SafetyPermitClaims) -> SafetyAuthorityReceipt:
        with self._lock:
            parent = self._decisions.get(claims.decision_receipt.value)
            if (
                parent is None
                or parent.route != "ordinary_coach"
                or parent.binding != claims.binding
                or claims.issued_at < parent.evaluated_at
                or claims.expires_at > parent.expires_at
            ):
                raise ValueError("permit requires an exact registered ordinary decision")
            receipt = self._receipt("permit")
            self._permits[receipt.value] = claims
            return receipt

    def verify_permit(
        self,
        claims: SafetyPermitClaims,
        receipt: SafetyAuthorityReceipt,
    ) -> bool:
        with self._lock:
            return (
                self._permits.get(receipt.value) == claims and receipt.value not in self._consumed
            )

    def verify_and_consume_permit(
        self,
        claims: SafetyPermitClaims,
        receipt: SafetyAuthorityReceipt,
    ) -> bool:
        with self._lock:
            if self._permits.get(receipt.value) != claims or receipt.value in self._consumed:
                return False
            self._consumed.add(receipt.value)
            return True


SAFETY_AUTHORITY = MemorySafetyAuthority()


def verdict_for_action(
    candidate: ActionCandidate,
    outcome: ActionSafetyOutcome = ActionSafetyOutcome.ALLOWED,
    *,
    trusted: bool = True,
    authority: MemoryActionAuthority = ACTION_AUTHORITY,
    **overrides: object,
) -> ActionSafetyVerdict:
    receipt = authority.new_receipt("verdict")
    values: dict[str, object] = {
        "verdict_id": f"verdict:{candidate.candidate_id}",
        "generation": 1,
        "subject_id": candidate.candidate_id,
        "vault_id": candidate.vault_id,
        "principal_id": candidate.principal_id,
        "session_id": candidate.session_id,
        "purpose": candidate.purpose,
        "candidate_fingerprint": candidate.fingerprint,
        "outcome": outcome,
        "policy_version": candidate.policy_version,
        "policy_snapshot": candidate.policy_snapshot,
        "decided_at": NOW,
        "expires_at": VALID_UNTIL,
        "reason": "Non-scored action policy result",
        "authority_receipt": receipt,
    }
    values.update(overrides)
    verdict = ActionSafetyVerdict(**values)  # type: ignore[arg-type]
    if trusted:
        authority.trust(verdict.authority_receipt, verdict.authority_claims)
    return verdict


def allowed_action(candidate: ActionCandidate) -> ActionCandidate:
    return candidate.with_safety_verdict(verdict_for_action(candidate))


def confirmation_for_action(
    candidate: ActionCandidate,
    *,
    trusted: bool = True,
    authority: MemoryActionAuthority = ACTION_AUTHORITY,
    **overrides: object,
) -> UserConfirmation:
    values: dict[str, object] = {
        "candidate_id": candidate.candidate_id,
        "vault_id": candidate.vault_id,
        "principal_id": candidate.principal_id,
        "session_id": candidate.session_id,
        "purpose": candidate.purpose,
        "fingerprint": candidate.fingerprint,
        "policy_version": candidate.policy_version,
        "policy_snapshot": candidate.policy_snapshot,
        "trusted": trusted,
        "authority": authority,
    }
    values.update(overrides)
    return confirmation_for_values(**values)  # type: ignore[arg-type]


def confirmation_for_goal(
    candidate: GoalCandidate,
    *,
    trusted: bool = True,
    authority: MemoryActionAuthority = ACTION_AUTHORITY,
    **overrides: object,
) -> UserConfirmation:
    values: dict[str, object] = {
        "candidate_id": candidate.goal_id,
        "vault_id": candidate.vault_id,
        "principal_id": candidate.principal_id,
        "session_id": candidate.session_id,
        "purpose": candidate.purpose,
        "fingerprint": candidate.fingerprint,
        "policy_version": candidate.policy_version,
        "policy_snapshot": candidate.policy_snapshot,
        "trusted": trusted,
        "authority": authority,
    }
    values.update(overrides)
    return confirmation_for_values(**values)  # type: ignore[arg-type]


def confirmation_for_plan(
    candidate: IfThenPlanCandidate,
    *,
    trusted: bool = True,
    authority: MemoryActionAuthority = ACTION_AUTHORITY,
    **overrides: object,
) -> UserConfirmation:
    values: dict[str, object] = {
        "candidate_id": candidate.plan_id,
        "vault_id": candidate.vault_id,
        "principal_id": candidate.principal_id,
        "session_id": candidate.session_id,
        "purpose": candidate.purpose,
        "fingerprint": candidate.fingerprint,
        "policy_version": candidate.policy_version,
        "policy_snapshot": candidate.policy_snapshot,
        "trusted": trusted,
        "authority": authority,
    }
    values.update(overrides)
    return confirmation_for_values(**values)  # type: ignore[arg-type]


def confirmation_for_values(
    *,
    candidate_id: str,
    vault_id: str,
    principal_id: str,
    session_id: str,
    purpose: str,
    fingerprint: str,
    policy_version: str,
    policy_snapshot: str,
    trusted: bool = True,
    authority: MemoryActionAuthority = ACTION_AUTHORITY,
    **overrides: object,
) -> UserConfirmation:
    receipt = authority.new_receipt("confirmation")
    values: dict[str, object] = {
        "confirmation_id": f"confirmation:{candidate_id}",
        "generation": 1,
        "candidate_id": candidate_id,
        "vault_id": vault_id,
        "principal_id": principal_id,
        "session_id": session_id,
        "purpose": purpose,
        "candidate_fingerprint": fingerprint,
        "policy_version": policy_version,
        "policy_snapshot": policy_snapshot,
        "confirmed_at": NOW,
        "expires_at": VALID_UNTIL,
        "explicit": True,
        "actor": ConfirmationActor.USER,
        "authority_receipt": receipt,
    }
    values.update(overrides)
    confirmation = UserConfirmation(**values)  # type: ignore[arg-type]
    if trusted:
        authority.trust(confirmation.authority_receipt, confirmation.authority_claims)
    return confirmation


def verdict_for_plan(
    candidate: IfThenPlanCandidate,
    outcome: ActionSafetyOutcome = ActionSafetyOutcome.ALLOWED,
    *,
    trusted: bool = True,
    authority: MemoryActionAuthority = ACTION_AUTHORITY,
    **overrides: object,
) -> ActionSafetyVerdict:
    receipt = authority.new_receipt("plan-verdict")
    values: dict[str, object] = {
        "verdict_id": f"verdict:{candidate.plan_id}",
        "generation": 1,
        "subject_id": candidate.plan_id,
        "vault_id": candidate.vault_id,
        "principal_id": candidate.principal_id,
        "session_id": candidate.session_id,
        "purpose": candidate.purpose,
        "candidate_fingerprint": candidate.fingerprint,
        "outcome": outcome,
        "policy_version": candidate.policy_version,
        "policy_snapshot": candidate.policy_snapshot,
        "decided_at": NOW,
        "expires_at": VALID_UNTIL,
        "reason": "Non-scored action policy result",
        "authority_receipt": receipt,
    }
    values.update(overrides)
    verdict = ActionSafetyVerdict(**values)  # type: ignore[arg-type]
    if trusted:
        authority.trust(verdict.authority_receipt, verdict.authority_claims)
    return verdict


def external_confirmation(
    candidate: ActionCandidate,
    *,
    scope: str | None = None,
    payload: dict[str, object] | None = None,
    trusted: bool = True,
    authority: MemoryActionAuthority = ACTION_AUTHORITY,
    **confirmation_overrides: object,
) -> ExternalActionConfirmation:
    proposal = candidate.external_action
    assert proposal is not None
    resolved_payload = dict(proposal.payload) if payload is None else payload
    return ExternalActionConfirmation(
        confirmation_for_action(
            candidate,
            trusted=trusted,
            authority=authority,
            **confirmation_overrides,
        ),
        ExternalActionSpec(scope or proposal.scope, resolved_payload),
    )


def safety_permit_for_values(
    *,
    vault_id: str,
    principal_id: str,
    session_id: str,
    input_fingerprint: str,
    policy_generation: str,
    operation: OrdinaryOperation,
    authority: MemorySafetyAuthority = SAFETY_AUTHORITY,
    issued_at: datetime = NOW,
    expires_at: datetime = VALID_UNTIL,
    trusted: bool = True,
) -> OrdinaryFlowPermit:
    binding = SafetyBinding(
        vault_id=vault_id,
        principal_id=principal_id,
        session_id=session_id,
        input_fingerprint=input_fingerprint,
        policy_generation=policy_generation,
    )
    claims = SafetyPermitClaims(
        binding=binding,
        operation=operation.value,
        policy_generation=policy_generation,
        issued_at=issued_at,
        expires_at=expires_at,
        decision_receipt=(
            authority.issue_decision(
                SafetyDecisionClaims(
                    binding=binding,
                    route="ordinary_coach",
                    priority="normal",
                    may_suggest_selected_supporter=False,
                    evaluated_at=issued_at,
                    expires_at=expires_at,
                )
            )
            if trusted
            else SafetyAuthorityReceipt(f"untrusted-parent:{operation.value}:{session_id}")
        ),
    )
    receipt = (
        authority.issue_permit(claims)
        if trusted
        else SafetyAuthorityReceipt(f"untrusted:{operation.value}:{session_id}")
    )
    return OrdinaryFlowPermit(claims=claims, authority_receipt=receipt)


def permit_for_action(
    candidate: ActionCandidate,
    operation: OrdinaryOperation,
    **overrides: object,
) -> OrdinaryFlowPermit:
    values: dict[str, object] = {
        "vault_id": candidate.vault_id,
        "principal_id": candidate.principal_id,
        "session_id": candidate.session_id,
        "input_fingerprint": candidate.safety_input_fingerprint,
        "policy_generation": candidate.safety_policy_generation,
        "operation": operation,
    }
    values.update(overrides)
    return safety_permit_for_values(**values)  # type: ignore[arg-type]


def permit_for_plan(
    candidate: IfThenPlanCandidate,
    operation: OrdinaryOperation = OrdinaryOperation.IF_THEN_PLAN_ACCEPTANCE,
    **overrides: object,
) -> OrdinaryFlowPermit:
    values: dict[str, object] = {
        "vault_id": candidate.vault_id,
        "principal_id": candidate.principal_id,
        "session_id": candidate.session_id,
        "input_fingerprint": candidate.safety_input_fingerprint,
        "policy_generation": candidate.safety_policy_generation,
        "operation": operation,
    }
    values.update(overrides)
    return safety_permit_for_values(**values)  # type: ignore[arg-type]


class MemoryAuthorizationPort:
    """Thread-safe durable READY/CLAIMED/terminal state test double."""

    def __init__(self, events: list[str] | None = None) -> None:
        self._records: dict[str, ExternalAuthorizationRecord] = {}
        self._claims: dict[str, ExternalActionClaim] = {}
        self._lock = Lock()
        self._claim_ids = count(1)
        self.events = events

    def _event(self, value: str) -> None:
        if self.events is not None:
            self.events.append(value)

    def ensure_ready_authorization(
        self,
        *,
        snapshot: ExternalAuthorizationSnapshot,
    ) -> ExternalAuthorizationRecord:
        with self._lock:
            self._event("ensure")
            return self._records.setdefault(
                snapshot.authorization_id,
                ExternalAuthorizationRecord(snapshot, ExternalActionState.READY),
            )

    def revoke_ready_authorization(
        self,
        *,
        authorization_id: str,
        authorization_snapshot_fingerprint: str,
        revoked_at: datetime,
    ) -> ExternalAuthorizationTransition:
        del revoked_at
        with self._lock:
            self._event("revoke")
            record = self._records.get(authorization_id)
            if record is None or record.snapshot.fingerprint != authorization_snapshot_fingerprint:
                return ExternalAuthorizationTransition(None, False)
            if record.state is ExternalActionState.READY:
                self._records[authorization_id] = ExternalAuthorizationRecord(
                    record.snapshot, ExternalActionState.REVOKED
                )
                return ExternalAuthorizationTransition(ExternalActionState.REVOKED, True)
            return ExternalAuthorizationTransition(record.state, False)

    def claim_ready_authorization(
        self,
        *,
        authorization_id: str,
        authorization_snapshot_fingerprint: str,
        claimed_at: datetime,
    ) -> ExternalClaimTransition:
        with self._lock:
            self._event("claim")
            record = self._records.get(authorization_id)
            if (
                record is None
                or record.snapshot.fingerprint != authorization_snapshot_fingerprint
                or claimed_at >= record.snapshot.expires_at
            ):
                return ExternalClaimTransition(
                    None if record is None else record.state, False, None
                )
            if record.state is not ExternalActionState.READY:
                return ExternalClaimTransition(record.state, False, None)
            claim = ExternalActionClaim(
                claim_id=f"claim-{next(self._claim_ids)}",
                authorization_id=authorization_id,
                authorization_snapshot_fingerprint=authorization_snapshot_fingerprint,
                claimed_at=claimed_at,
            )
            self._records[authorization_id] = ExternalAuthorizationRecord(
                record.snapshot, ExternalActionState.CLAIMED
            )
            self._claims[authorization_id] = claim
            return ExternalClaimTransition(ExternalActionState.CLAIMED, True, claim)

    def complete_claimed_authorization(
        self,
        *,
        claim: ExternalActionClaim,
        connector_receipt: ExternalConnectorReceipt,
        executed_at: datetime,
    ) -> ExternalAuthorizationTransition:
        del connector_receipt, executed_at
        with self._lock:
            self._event("complete")
            record = self._records.get(claim.authorization_id)
            if (
                record is None
                or record.state is not ExternalActionState.CLAIMED
                or self._claims.get(claim.authorization_id) != claim
            ):
                return ExternalAuthorizationTransition(
                    None if record is None else record.state, False
                )
            self._records[claim.authorization_id] = ExternalAuthorizationRecord(
                record.snapshot, ExternalActionState.EXECUTED
            )
            return ExternalAuthorizationTransition(ExternalActionState.EXECUTED, True)

    def state(self, authorization_id: str) -> ExternalActionState | None:
        with self._lock:
            record = self._records.get(authorization_id)
            return None if record is None else record.state


MemoryConsumptionPort = MemoryAuthorizationPort


class RecordingConnector:
    def __init__(
        self,
        *,
        events: list[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.events = events
        self.error = error
        self.calls: list[ExternalActionRequest] = []
        self._lock = Lock()

    def execute(self, request: ExternalActionRequest) -> ExternalConnectorReceipt:
        with self._lock:
            if self.events is not None:
                self.events.append("connector")
            self.calls.append(request)
        if self.error is not None:
            raise self.error
        return ExternalConnectorReceipt(f"connector:{request.claim_id}")
