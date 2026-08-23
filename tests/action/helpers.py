from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TypedDict

from life_coach.modules.action import (
    ActionCandidate,
    ActionSafetyOutcome,
    ActionSafetyVerdict,
    ConfirmationActor,
    ExternalActionConfirmation,
    ExternalActionSpec,
    ExternalActionState,
    ExternalAuthorizationRecord,
    ExternalAuthorizationSnapshot,
    ExternalAuthorizationTransition,
    GoalCandidate,
    IfThenPlanCandidate,
    UserConfirmation,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
VALID_UNTIL = NOW + timedelta(minutes=30)


class ActionContext(TypedDict):
    vault_id: str
    principal_id: str
    purpose: str
    policy_version: str
    policy_snapshot: str


CONTEXT: ActionContext = {
    "vault_id": "vault-1",
    "principal_id": "user-1",
    "purpose": "coach.action",
    "policy_version": "action-policy-v2",
    "policy_snapshot": "sha256:action-policy-v2-snapshot",
}


def verdict_for_action(
    candidate: ActionCandidate,
    outcome: ActionSafetyOutcome = ActionSafetyOutcome.ALLOWED,
    **overrides: object,
) -> ActionSafetyVerdict:
    values: dict[str, object] = {
        "subject_id": candidate.candidate_id,
        "vault_id": candidate.vault_id,
        "principal_id": candidate.principal_id,
        "purpose": candidate.purpose,
        "candidate_fingerprint": candidate.fingerprint,
        "outcome": outcome,
        "policy_version": candidate.policy_version,
        "policy_snapshot": candidate.policy_snapshot,
        "decided_at": NOW,
        "expires_at": VALID_UNTIL,
        "reason": "Non-scored action policy result",
    }
    values.update(overrides)
    return ActionSafetyVerdict(**values)  # type: ignore[arg-type]


def allowed_action(candidate: ActionCandidate) -> ActionCandidate:
    return candidate.with_safety_verdict(verdict_for_action(candidate))


def confirmation_for_action(
    candidate: ActionCandidate,
    **overrides: object,
) -> UserConfirmation:
    values: dict[str, object] = {
        "candidate_id": candidate.candidate_id,
        "vault_id": candidate.vault_id,
        "principal_id": candidate.principal_id,
        "purpose": candidate.purpose,
        "fingerprint": candidate.fingerprint,
        "policy_version": candidate.policy_version,
        "policy_snapshot": candidate.policy_snapshot,
    }
    values.update(overrides)
    return confirmation_for_values(**values)  # type: ignore[arg-type]


def confirmation_for_goal(candidate: GoalCandidate, **overrides: object) -> UserConfirmation:
    values: dict[str, object] = {
        "candidate_id": candidate.goal_id,
        "vault_id": candidate.vault_id,
        "principal_id": candidate.principal_id,
        "purpose": candidate.purpose,
        "fingerprint": candidate.fingerprint,
        "policy_version": candidate.policy_version,
        "policy_snapshot": candidate.policy_snapshot,
    }
    values.update(overrides)
    return confirmation_for_values(**values)  # type: ignore[arg-type]


def confirmation_for_plan(candidate: IfThenPlanCandidate, **overrides: object) -> UserConfirmation:
    values: dict[str, object] = {
        "candidate_id": candidate.plan_id,
        "vault_id": candidate.vault_id,
        "principal_id": candidate.principal_id,
        "purpose": candidate.purpose,
        "fingerprint": candidate.fingerprint,
        "policy_version": candidate.policy_version,
        "policy_snapshot": candidate.policy_snapshot,
    }
    values.update(overrides)
    return confirmation_for_values(**values)  # type: ignore[arg-type]


def confirmation_for_values(
    *,
    candidate_id: str,
    vault_id: str,
    principal_id: str,
    purpose: str,
    fingerprint: str,
    policy_version: str,
    policy_snapshot: str,
    **overrides: object,
) -> UserConfirmation:
    values: dict[str, object] = {
        "confirmation_id": f"confirmation:{candidate_id}",
        "generation": 1,
        "candidate_id": candidate_id,
        "vault_id": vault_id,
        "principal_id": principal_id,
        "purpose": purpose,
        "candidate_fingerprint": fingerprint,
        "policy_version": policy_version,
        "policy_snapshot": policy_snapshot,
        "confirmed_at": NOW,
        "expires_at": VALID_UNTIL,
        "explicit": True,
        "actor": ConfirmationActor.USER,
    }
    values.update(overrides)
    return UserConfirmation(**values)  # type: ignore[arg-type]


def verdict_for_plan(
    candidate: IfThenPlanCandidate,
    outcome: ActionSafetyOutcome = ActionSafetyOutcome.ALLOWED,
    **overrides: object,
) -> ActionSafetyVerdict:
    values: dict[str, object] = {
        "subject_id": candidate.plan_id,
        "vault_id": candidate.vault_id,
        "principal_id": candidate.principal_id,
        "purpose": candidate.purpose,
        "candidate_fingerprint": candidate.fingerprint,
        "outcome": outcome,
        "policy_version": candidate.policy_version,
        "policy_snapshot": candidate.policy_snapshot,
        "decided_at": NOW,
        "expires_at": VALID_UNTIL,
        "reason": "Non-scored action policy result",
    }
    values.update(overrides)
    return ActionSafetyVerdict(**values)  # type: ignore[arg-type]


def external_confirmation(
    candidate: ActionCandidate,
    *,
    scope: str | None = None,
    payload: dict[str, object] | None = None,
    **confirmation_overrides: object,
) -> ExternalActionConfirmation:
    proposal = candidate.external_action
    assert proposal is not None
    resolved_payload = dict(proposal.payload) if payload is None else payload
    return ExternalActionConfirmation(
        confirmation_for_action(candidate, **confirmation_overrides),
        ExternalActionSpec(scope or proposal.scope, resolved_payload),
    )


class MemoryConsumptionPort:
    """Test double that models a durable compare-and-set authorization row."""

    def __init__(self) -> None:
        self._records: dict[str, ExternalAuthorizationRecord] = {}

    def ensure_ready_authorization(
        self,
        *,
        snapshot: ExternalAuthorizationSnapshot,
    ) -> ExternalAuthorizationRecord:
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
        record = self._records.get(authorization_id)
        if record is None or record.snapshot.fingerprint != authorization_snapshot_fingerprint:
            return ExternalAuthorizationTransition(None, False)
        if record.state is ExternalActionState.READY:
            self._records[authorization_id] = ExternalAuthorizationRecord(
                record.snapshot, ExternalActionState.REVOKED
            )
            return ExternalAuthorizationTransition(ExternalActionState.REVOKED, True)
        return ExternalAuthorizationTransition(record.state, False)

    def consume_ready_authorization(
        self,
        *,
        authorization_id: str,
        authorization_snapshot_fingerprint: str,
        consumed_at: datetime,
    ) -> ExternalAuthorizationTransition:
        record = self._records.get(authorization_id)
        if record is None or record.snapshot.fingerprint != authorization_snapshot_fingerprint:
            return ExternalAuthorizationTransition(None, False)
        if consumed_at >= record.snapshot.expires_at:
            return ExternalAuthorizationTransition(record.state, False)
        if record.state is ExternalActionState.READY:
            self._records[authorization_id] = ExternalAuthorizationRecord(
                record.snapshot, ExternalActionState.EXECUTED
            )
            return ExternalAuthorizationTransition(ExternalActionState.EXECUTED, True)
        return ExternalAuthorizationTransition(record.state, False)
