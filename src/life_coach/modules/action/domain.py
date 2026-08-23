"""Pure domain rules for explicitly confirmed, policy-gated actions.

The module contains no classifier, clock, persistence adapter, or concrete
side-effecting connector. Trusted clocks and authority ports are injected at each
transition, and external jobs atomically claim a stable authorization before I/O.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, Self, cast

from life_coach.modules.action.authority import (
    ActionAuthorityClaims,
    ActionAuthorityPort,
    ActionCredentialKind,
    OpaqueActionReceipt,
)
from life_coach.modules.safety.authority import (
    SafetyAuthorityPort,
    SafetyBinding,
    TrustedClock,
    normalize_input_fingerprint,
    read_trusted_time,
)
from life_coach.modules.safety.orchestration import OrdinaryFlowPermit, OrdinaryOperation


class ActionDomainError(ValueError):
    """Base class for rejected action-domain transitions."""


class InvalidActionCandidateError(ActionDomainError):
    """Raised when an action-domain value is incomplete or inconsistent."""


class ConfirmationRequiredError(ActionDomainError):
    """Raised when a transition lacks explicit confirmation from the user."""


class ConfirmationMismatchError(ActionDomainError):
    """Raised when confirmation does not bind the exact current object."""


class ConfirmationNotCurrentError(ConfirmationRequiredError):
    """Raised when confirmation is expired or not yet effective."""


class IntentCannotBecomeTaskError(ActionDomainError):
    """Raised when an intent classification is not eligible for task creation."""


class GoalNotEndorsedError(ActionDomainError):
    """Raised when an if-then plan is proposed for an unendorsed goal."""


class InvalidIfThenPlanError(ActionDomainError):
    """Raised when an if-then plan lacks an observable cue or small behavior."""


class ActionSafetyGateError(ActionDomainError):
    """Base class for fail-closed action safety gate failures."""


class ActionSafetyRequiredError(ActionSafetyGateError):
    """Raised when an actionable transition has no safety verdict."""


class ActionSafetyBlockedError(ActionSafetyGateError):
    """Raised when policy blocks the proposed behavior."""


class ActionSafetySubjectMismatchError(ActionSafetyGateError):
    """Raised when a verdict does not bind the exact current candidate."""


class ActionSafetyNotCurrentError(ActionSafetyGateError):
    """Raised when a safety verdict is expired or not yet effective."""


class ExternalActionAlreadyConsumedError(ActionDomainError):
    """Raised when persistent atomic consumption does not claim an authorization."""


class ExternalActionExecutionTimeError(ActionDomainError):
    """Raised when execution is outside the authorization's valid time window."""


class ExternalActionAuthorizationExpiredError(ActionDomainError):
    """Raised when an external authorization has expired."""


class ExternalActionAuthorizationRevokedError(ActionDomainError):
    """Raised when an external authorization has been revoked."""


class ExternalActionAuthorizationLineageError(ActionDomainError):
    """Raised when a stable authorization ID is reused with changed immutable terms."""


class ActionAuthorityRejectedError(ActionDomainError):
    """Raised when an opaque action credential receipt is missing or untrusted."""


class SafetyPermitRejectedError(ActionDomainError):
    """Raised when an operation-specific safety permit is not exact and trusted."""


class ExternalActionClaimedError(ActionDomainError):
    """Raised when a claimed external action can no longer be revoked or reclaimed."""


class ActionIntent(StrEnum):
    """Intent classes kept distinct throughout the action pipeline."""

    COMMITMENT = "commitment"
    WISH = "wish"
    CONCERN = "concern"
    IDEA = "idea"
    EXPERIMENT = "experiment"
    EXTERNAL_ACTION = "external_action"


ActionIntentType = ActionIntent


class ConfirmationActor(StrEnum):
    USER = "user"
    MODEL = "model"
    SYSTEM = "system"


class CandidateState(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"


class TaskState(StrEnum):
    OPEN = "open"


class ExperimentState(StrEnum):
    ACCEPTED = "accepted"


class ExternalActionState(StrEnum):
    """``READY`` still requires a current, atomic persistent claim."""

    READY = "ready"
    CLAIMED = "claimed"
    REVOKED = "revoked"
    EXECUTED = "executed"


class IfThenPlanState(StrEnum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"


class ActionSafetyOutcome(StrEnum):
    """Non-scored policy outcome for a proposed behavior."""

    ALLOWED = "allowed"
    BLOCKED = "blocked"


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidActionCandidateError(f"{name} must not be blank")


def _require_aware_datetime(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidActionCandidateError(f"{name} must include a UTC offset")


def _require_positive_generation(generation: int) -> None:
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise InvalidActionCandidateError("generation must be a positive integer")


def _canonical_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _normalize_json(value: object) -> object:
    """Return a detached JSON-compatible value or reject the payload."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidActionCandidateError("payload numbers must be finite")
        return value
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        normalized: dict[str, object] = {}
        for key, child in mapping.items():
            if not isinstance(key, str):
                raise InvalidActionCandidateError("payload keys must be strings")
            normalized[key] = _normalize_json(child)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_json(child) for child in cast(Sequence[object], value)]
    raise InvalidActionCandidateError("payload must contain only JSON values")


def _fingerprint(kind: str, content: Mapping[str, object]) -> str:
    canonical = json.dumps(
        {"kind": kind, "content": _normalize_json(content)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_policy_binding(
    *,
    actual_version: str,
    actual_snapshot: str,
    expected_version: str,
    expected_snapshot: str,
    error_type: type[ActionDomainError],
) -> None:
    if actual_version != expected_version or actual_snapshot != expected_snapshot:
        raise error_type("policy version or immutable snapshot does not match the candidate")


@dataclass(frozen=True, slots=True)
class ActionSafetyVerdict:
    """Fail-closed, non-scored decision bound to one immutable candidate."""

    verdict_id: str
    generation: int
    subject_id: str
    vault_id: str
    principal_id: str
    session_id: str
    purpose: str
    candidate_fingerprint: str
    outcome: ActionSafetyOutcome
    policy_version: str
    policy_snapshot: str
    decided_at: datetime
    expires_at: datetime
    reason: str
    authority_receipt: OpaqueActionReceipt

    def __post_init__(self) -> None:
        for name, value in (
            ("verdict_id", self.verdict_id),
            ("subject_id", self.subject_id),
            ("vault_id", self.vault_id),
            ("principal_id", self.principal_id),
            ("session_id", self.session_id),
            ("purpose", self.purpose),
            ("candidate_fingerprint", self.candidate_fingerprint),
            ("policy_version", self.policy_version),
            ("policy_snapshot", self.policy_snapshot),
            ("reason", self.reason),
        ):
            _require_text(name, value)
        _require_positive_generation(self.generation)
        if not isinstance(self.outcome, ActionSafetyOutcome):
            raise InvalidActionCandidateError("outcome must be an ActionSafetyOutcome enum value")
        _require_aware_datetime("decided_at", self.decided_at)
        _require_aware_datetime("expires_at", self.expires_at)
        if self.expires_at <= self.decided_at:
            raise InvalidActionCandidateError("safety verdict must expire after it is decided")
        if not isinstance(self.authority_receipt, OpaqueActionReceipt):
            raise InvalidActionCandidateError("authority_receipt must be opaque action evidence")

    @property
    def authority_claims(self) -> ActionAuthorityClaims:
        return ActionAuthorityClaims(
            kind=ActionCredentialKind.SAFETY_VERDICT,
            credential_id=self.verdict_id,
            generation=self.generation,
            subject_id=self.subject_id,
            vault_id=self.vault_id,
            principal_id=self.principal_id,
            session_id=self.session_id,
            purpose=self.purpose,
            input_fingerprint=self.candidate_fingerprint,
            policy_version=self.policy_version,
            policy_snapshot=self.policy_snapshot,
            issued_at=self.decided_at,
            expires_at=self.expires_at,
            payload_fingerprint=_fingerprint(
                "action-safety-verdict-payload-v1",
                {"outcome": self.outcome.value, "reason": self.reason},
            ),
        )


@dataclass(frozen=True, slots=True)
class UserConfirmation:
    """Explicit user event bound to exact content, authority, purpose, and policy."""

    confirmation_id: str
    generation: int
    candidate_id: str
    vault_id: str
    principal_id: str
    session_id: str
    purpose: str
    candidate_fingerprint: str
    policy_version: str
    policy_snapshot: str
    confirmed_at: datetime
    expires_at: datetime
    explicit: bool
    actor: ConfirmationActor
    authority_receipt: OpaqueActionReceipt

    def __post_init__(self) -> None:
        for name, value in (
            ("confirmation_id", self.confirmation_id),
            ("candidate_id", self.candidate_id),
            ("vault_id", self.vault_id),
            ("principal_id", self.principal_id),
            ("session_id", self.session_id),
            ("purpose", self.purpose),
            ("candidate_fingerprint", self.candidate_fingerprint),
            ("policy_version", self.policy_version),
            ("policy_snapshot", self.policy_snapshot),
        ):
            _require_text(name, value)
        _require_positive_generation(self.generation)
        _require_aware_datetime("confirmed_at", self.confirmed_at)
        _require_aware_datetime("expires_at", self.expires_at)
        if self.expires_at <= self.confirmed_at:
            raise InvalidActionCandidateError("confirmation must expire after it is recorded")
        if not isinstance(self.explicit, bool):
            raise InvalidActionCandidateError("explicit must be a bool")
        if not isinstance(self.actor, ConfirmationActor):
            raise InvalidActionCandidateError("actor must be a ConfirmationActor enum value")
        if not isinstance(self.authority_receipt, OpaqueActionReceipt):
            raise InvalidActionCandidateError("authority_receipt must be opaque action evidence")

    @property
    def authority_claims(self) -> ActionAuthorityClaims:
        return ActionAuthorityClaims(
            kind=ActionCredentialKind.USER_CONFIRMATION,
            credential_id=self.confirmation_id,
            generation=self.generation,
            subject_id=self.candidate_id,
            vault_id=self.vault_id,
            principal_id=self.principal_id,
            session_id=self.session_id,
            purpose=self.purpose,
            input_fingerprint=self.candidate_fingerprint,
            policy_version=self.policy_version,
            policy_snapshot=self.policy_snapshot,
            issued_at=self.confirmed_at,
            expires_at=self.expires_at,
            payload_fingerprint=_fingerprint(
                "user-confirmation-payload-v1",
                {"explicit": self.explicit, "actor": self.actor.value},
            ),
        )


def _validate_user_confirmation(
    confirmation: UserConfirmation,
    *,
    action_authority: ActionAuthorityPort,
    expected_candidate_id: str,
    expected_vault_id: str,
    expected_principal_id: str,
    expected_session_id: str,
    expected_purpose: str,
    expected_fingerprint: str,
    expected_policy_version: str,
    expected_policy_snapshot: str,
    at: datetime,
) -> None:
    _require_aware_datetime("at", at)
    if not confirmation.explicit or confirmation.actor is not ConfirmationActor.USER:
        raise ConfirmationRequiredError("an explicit user confirmation is required")
    if (
        confirmation.candidate_id != expected_candidate_id
        or confirmation.vault_id != expected_vault_id
        or confirmation.principal_id != expected_principal_id
        or confirmation.session_id != expected_session_id
        or confirmation.purpose != expected_purpose
        or confirmation.candidate_fingerprint != expected_fingerprint
    ):
        raise ConfirmationMismatchError(
            "confirmation does not bind the exact candidate, vault, principal, and purpose"
        )
    _validate_policy_binding(
        actual_version=confirmation.policy_version,
        actual_snapshot=confirmation.policy_snapshot,
        expected_version=expected_policy_version,
        expected_snapshot=expected_policy_snapshot,
        error_type=ConfirmationMismatchError,
    )
    if at < confirmation.confirmed_at or at >= confirmation.expires_at:
        raise ConfirmationNotCurrentError("user confirmation is not current")
    if (
        action_authority.verify_receipt(
            confirmation.authority_receipt,
            expected_claims=confirmation.authority_claims,
            current_time=at,
        )
        is not True
    ):
        raise ActionAuthorityRejectedError("user confirmation receipt is not trusted")


def _require_allowed_safety_verdict(
    verdict: ActionSafetyVerdict | None,
    *,
    action_authority: ActionAuthorityPort,
    expected_subject_id: str,
    expected_vault_id: str,
    expected_principal_id: str,
    expected_session_id: str,
    expected_purpose: str,
    expected_fingerprint: str,
    expected_policy_version: str,
    expected_policy_snapshot: str,
    at: datetime,
) -> None:
    _require_aware_datetime("at", at)
    if verdict is None:
        raise ActionSafetyRequiredError("a current ALLOWED action safety verdict is required")
    if (
        verdict.subject_id != expected_subject_id
        or verdict.vault_id != expected_vault_id
        or verdict.principal_id != expected_principal_id
        or verdict.session_id != expected_session_id
        or verdict.purpose != expected_purpose
        or verdict.candidate_fingerprint != expected_fingerprint
    ):
        raise ActionSafetySubjectMismatchError(
            "safety verdict does not bind the exact candidate, vault, principal, and purpose"
        )
    _validate_policy_binding(
        actual_version=verdict.policy_version,
        actual_snapshot=verdict.policy_snapshot,
        expected_version=expected_policy_version,
        expected_snapshot=expected_policy_snapshot,
        error_type=ActionSafetySubjectMismatchError,
    )
    if at < verdict.decided_at or at >= verdict.expires_at:
        raise ActionSafetyNotCurrentError("action safety verdict is not current")
    if verdict.outcome is not ActionSafetyOutcome.ALLOWED:
        raise ActionSafetyBlockedError("action safety policy blocked the proposed behavior")
    if (
        action_authority.verify_receipt(
            verdict.authority_receipt,
            expected_claims=verdict.authority_claims,
            current_time=at,
        )
        is not True
    ):
        raise ActionAuthorityRejectedError("action safety verdict receipt is not trusted")


@dataclass(frozen=True, slots=True)
class ExperimentProposal:
    rationale: str
    cost: str
    exit_plan: str
    is_reversible: bool

    def __post_init__(self) -> None:
        _require_text("rationale", self.rationale)
        _require_text("cost", self.cost)
        _require_text("exit_plan", self.exit_plan)
        if not isinstance(self.is_reversible, bool):
            raise InvalidActionCandidateError("is_reversible must be a bool")
        if not self.is_reversible:
            raise InvalidActionCandidateError("experiments must be explicitly reversible")


@dataclass(frozen=True, slots=True, init=False)
class ExternalActionSpec:
    """Immutable, canonical scope and payload for one proposed side effect."""

    scope: str
    _payload_json: str = field(repr=False)

    def __init__(self, scope: str, payload: Mapping[str, object]) -> None:
        _require_text("scope", scope)
        normalized = _normalize_json(payload)
        if not isinstance(normalized, dict):
            raise InvalidActionCandidateError("external payload must be an object")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(
            self,
            "_payload_json",
            json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )

    @property
    def payload(self) -> Mapping[str, object]:
        decoded: object = json.loads(self._payload_json)
        if not isinstance(decoded, dict):  # pragma: no cover
            raise RuntimeError("canonical external payload is not an object")
        return MappingProxyType(cast(dict[str, object], decoded))

    @property
    def canonical_payload(self) -> str:
        return self._payload_json


@dataclass(frozen=True, slots=True)
class ActionCandidate:
    """A non-executable candidate; its fingerprint excludes verdicts and confirmations."""

    candidate_id: str
    vault_id: str
    principal_id: str
    session_id: str
    safety_input_fingerprint: str
    safety_policy_generation: str
    purpose: str
    policy_version: str
    policy_snapshot: str
    intent: ActionIntent
    description: str
    deadline: datetime | None = None
    priority: str | None = None
    experiment: ExperimentProposal | None = None
    external_action: ExternalActionSpec | None = None
    safety_verdict: ActionSafetyVerdict | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("candidate_id", self.candidate_id),
            ("vault_id", self.vault_id),
            ("principal_id", self.principal_id),
            ("session_id", self.session_id),
            ("purpose", self.purpose),
            ("policy_version", self.policy_version),
            ("policy_snapshot", self.policy_snapshot),
            ("description", self.description),
        ):
            _require_text(name, value)
        object.__setattr__(
            self,
            "safety_input_fingerprint",
            normalize_input_fingerprint(self.safety_input_fingerprint),
        )
        _require_text("safety_policy_generation", self.safety_policy_generation)
        if not isinstance(self.intent, ActionIntent):
            raise InvalidActionCandidateError("intent must be an ActionIntent enum value")
        if self.priority is not None:
            _require_text("priority", self.priority)
        if self.deadline is not None:
            _require_aware_datetime("deadline", self.deadline)
        if self.intent in {ActionIntent.WISH, ActionIntent.CONCERN, ActionIntent.IDEA} and (
            self.deadline is not None or self.priority is not None
        ):
            raise InvalidActionCandidateError(
                "wish, concern, and idea candidates cannot carry deadline or priority"
            )
        if self.intent is ActionIntent.EXPERIMENT:
            if self.experiment is None:
                raise InvalidActionCandidateError("experiment candidates require visible terms")
        elif self.experiment is not None:
            raise InvalidActionCandidateError("experiment terms require experiment intent")
        if self.intent is ActionIntent.EXTERNAL_ACTION:
            if self.external_action is None:
                raise InvalidActionCandidateError("external actions require scope and payload")
        elif self.external_action is not None:
            raise InvalidActionCandidateError("external details require external action intent")
        if self.intent in {ActionIntent.WISH, ActionIntent.CONCERN, ActionIntent.IDEA}:
            if self.safety_verdict is not None:
                raise InvalidActionCandidateError("non-actionable intents do not accept verdicts")
        elif self.safety_verdict is not None:
            self._validate_verdict_binding(self.safety_verdict)

    @property
    def state(self) -> CandidateState:
        return CandidateState.CANDIDATE

    @property
    def is_executable(self) -> bool:
        return False

    @property
    def fingerprint(self) -> str:
        experiment: object = None
        if self.experiment is not None:
            experiment = {
                "rationale": self.experiment.rationale,
                "cost": self.experiment.cost,
                "exit_plan": self.experiment.exit_plan,
                "is_reversible": self.experiment.is_reversible,
            }
        external: object = None
        if self.external_action is not None:
            external = {
                "scope": self.external_action.scope,
                "payload": json.loads(self.external_action.canonical_payload),
            }
        return _fingerprint(
            "action-candidate-v1",
            {
                "intent": self.intent.value,
                "description": self.description,
                "deadline": _canonical_datetime(self.deadline),
                "priority": self.priority,
                "experiment": experiment,
                "external_action": external,
            },
        )

    def _validate_verdict_binding(self, verdict: ActionSafetyVerdict) -> None:
        if (
            verdict.subject_id != self.candidate_id
            or verdict.vault_id != self.vault_id
            or verdict.principal_id != self.principal_id
            or verdict.session_id != self.session_id
            or verdict.purpose != self.purpose
            or verdict.candidate_fingerprint != self.fingerprint
            or verdict.policy_version != self.policy_version
            or verdict.policy_snapshot != self.policy_snapshot
        ):
            raise ActionSafetySubjectMismatchError("verdict does not bind this exact candidate")

    def with_safety_verdict(self, verdict: ActionSafetyVerdict) -> Self:
        """Attach an immutable verdict; conversions still revalidate it every time."""

        return replace(self, safety_verdict=verdict)

    @classmethod
    def commitment(
        cls,
        candidate_id: str,
        description: str,
        *,
        vault_id: str,
        principal_id: str,
        session_id: str,
        safety_input_fingerprint: str,
        safety_policy_generation: str,
        purpose: str,
        policy_version: str,
        policy_snapshot: str,
        deadline: datetime | None = None,
        priority: str | None = None,
        safety_verdict: ActionSafetyVerdict | None = None,
    ) -> Self:
        return cls(
            candidate_id=candidate_id,
            vault_id=vault_id,
            principal_id=principal_id,
            session_id=session_id,
            safety_input_fingerprint=safety_input_fingerprint,
            safety_policy_generation=safety_policy_generation,
            purpose=purpose,
            policy_version=policy_version,
            policy_snapshot=policy_snapshot,
            intent=ActionIntent.COMMITMENT,
            description=description,
            deadline=deadline,
            priority=priority,
            safety_verdict=safety_verdict,
        )

    @classmethod
    def wish(
        cls,
        candidate_id: str,
        description: str,
        *,
        vault_id: str,
        principal_id: str,
        session_id: str,
        safety_input_fingerprint: str,
        safety_policy_generation: str,
        purpose: str,
        policy_version: str,
        policy_snapshot: str,
    ) -> Self:
        return cls(
            candidate_id=candidate_id,
            vault_id=vault_id,
            principal_id=principal_id,
            session_id=session_id,
            safety_input_fingerprint=safety_input_fingerprint,
            safety_policy_generation=safety_policy_generation,
            purpose=purpose,
            policy_version=policy_version,
            policy_snapshot=policy_snapshot,
            intent=ActionIntent.WISH,
            description=description,
        )

    @classmethod
    def concern(
        cls,
        candidate_id: str,
        description: str,
        *,
        vault_id: str,
        principal_id: str,
        session_id: str,
        safety_input_fingerprint: str,
        safety_policy_generation: str,
        purpose: str,
        policy_version: str,
        policy_snapshot: str,
    ) -> Self:
        return cls(
            candidate_id=candidate_id,
            vault_id=vault_id,
            principal_id=principal_id,
            session_id=session_id,
            safety_input_fingerprint=safety_input_fingerprint,
            safety_policy_generation=safety_policy_generation,
            purpose=purpose,
            policy_version=policy_version,
            policy_snapshot=policy_snapshot,
            intent=ActionIntent.CONCERN,
            description=description,
        )

    @classmethod
    def idea(
        cls,
        candidate_id: str,
        description: str,
        *,
        vault_id: str,
        principal_id: str,
        session_id: str,
        safety_input_fingerprint: str,
        safety_policy_generation: str,
        purpose: str,
        policy_version: str,
        policy_snapshot: str,
    ) -> Self:
        return cls(
            candidate_id=candidate_id,
            vault_id=vault_id,
            principal_id=principal_id,
            session_id=session_id,
            safety_input_fingerprint=safety_input_fingerprint,
            safety_policy_generation=safety_policy_generation,
            purpose=purpose,
            policy_version=policy_version,
            policy_snapshot=policy_snapshot,
            intent=ActionIntent.IDEA,
            description=description,
        )

    @classmethod
    def experiment_candidate(
        cls,
        candidate_id: str,
        description: str,
        *,
        vault_id: str,
        principal_id: str,
        session_id: str,
        safety_input_fingerprint: str,
        safety_policy_generation: str,
        purpose: str,
        policy_version: str,
        policy_snapshot: str,
        rationale: str,
        cost: str,
        exit_plan: str,
        is_reversible: bool,
        safety_verdict: ActionSafetyVerdict | None = None,
    ) -> Self:
        return cls(
            candidate_id=candidate_id,
            vault_id=vault_id,
            principal_id=principal_id,
            session_id=session_id,
            safety_input_fingerprint=safety_input_fingerprint,
            safety_policy_generation=safety_policy_generation,
            purpose=purpose,
            policy_version=policy_version,
            policy_snapshot=policy_snapshot,
            intent=ActionIntent.EXPERIMENT,
            description=description,
            experiment=ExperimentProposal(rationale, cost, exit_plan, is_reversible),
            safety_verdict=safety_verdict,
        )

    @classmethod
    def external_action_candidate(
        cls,
        candidate_id: str,
        description: str,
        *,
        vault_id: str,
        principal_id: str,
        session_id: str,
        safety_input_fingerprint: str,
        safety_policy_generation: str,
        purpose: str,
        policy_version: str,
        policy_snapshot: str,
        scope: str,
        payload: Mapping[str, object],
        safety_verdict: ActionSafetyVerdict | None = None,
    ) -> Self:
        return cls(
            candidate_id=candidate_id,
            vault_id=vault_id,
            principal_id=principal_id,
            session_id=session_id,
            safety_input_fingerprint=safety_input_fingerprint,
            safety_policy_generation=safety_policy_generation,
            purpose=purpose,
            policy_version=policy_version,
            policy_snapshot=policy_snapshot,
            intent=ActionIntent.EXTERNAL_ACTION,
            description=description,
            external_action=ExternalActionSpec(scope, payload),
            safety_verdict=safety_verdict,
        )


def _validate_action_confirmation(
    candidate: ActionCandidate,
    confirmation: UserConfirmation,
    *,
    action_authority: ActionAuthorityPort,
    at: datetime,
) -> None:
    fingerprint = candidate.fingerprint
    _validate_user_confirmation(
        confirmation,
        action_authority=action_authority,
        expected_candidate_id=candidate.candidate_id,
        expected_vault_id=candidate.vault_id,
        expected_principal_id=candidate.principal_id,
        expected_session_id=candidate.session_id,
        expected_purpose=candidate.purpose,
        expected_fingerprint=fingerprint,
        expected_policy_version=candidate.policy_version,
        expected_policy_snapshot=candidate.policy_snapshot,
        at=at,
    )


def _validate_action_safety(
    candidate: ActionCandidate,
    *,
    action_authority: ActionAuthorityPort,
    at: datetime,
) -> None:
    _require_allowed_safety_verdict(
        candidate.safety_verdict,
        action_authority=action_authority,
        expected_subject_id=candidate.candidate_id,
        expected_vault_id=candidate.vault_id,
        expected_principal_id=candidate.principal_id,
        expected_session_id=candidate.session_id,
        expected_purpose=candidate.purpose,
        expected_fingerprint=candidate.fingerprint,
        expected_policy_version=candidate.policy_version,
        expected_policy_snapshot=candidate.policy_snapshot,
        at=at,
    )


def _validate_action_candidate_credentials(
    candidate: ActionCandidate,
    confirmation: UserConfirmation,
    *,
    action_authority: ActionAuthorityPort,
    at: datetime,
) -> None:
    _validate_action_confirmation(
        candidate,
        confirmation,
        action_authority=action_authority,
        at=at,
    )
    _validate_action_safety(candidate, action_authority=action_authority, at=at)


def _consume_safety_permit(
    permit: OrdinaryFlowPermit,
    *,
    operation: OrdinaryOperation,
    vault_id: str,
    principal_id: str,
    session_id: str,
    input_fingerprint: str,
    safety_policy_generation: str,
    safety_authority: SafetyAuthorityPort,
    at: datetime,
) -> None:
    if not isinstance(permit, OrdinaryFlowPermit):
        raise SafetyPermitRejectedError("an opaque ordinary-flow permit is required")
    expected_binding = SafetyBinding(
        vault_id=vault_id,
        principal_id=principal_id,
        session_id=session_id,
        input_fingerprint=input_fingerprint,
        policy_generation=safety_policy_generation,
    )
    if (
        permit.operation is not operation
        or permit.binding != expected_binding
        or permit.policy_generation != safety_policy_generation
        or not permit.is_current(at=at)
    ):
        raise SafetyPermitRejectedError("safety permit claims do not match this operation")
    if (
        safety_authority.verify_and_consume_permit(
            permit.claims,
            permit.authority_receipt,
        )
        is not True
    ):
        raise SafetyPermitRejectedError("safety permit receipt is untrusted or already consumed")


@dataclass(frozen=True, slots=True)
class ConfirmedActionCandidate:
    candidate: ActionCandidate
    confirmation: UserConfirmation
    validated_at: datetime

    def __post_init__(self) -> None:
        if self.candidate.intent is ActionIntent.EXTERNAL_ACTION:
            raise ConfirmationRequiredError("external actions use final external confirmation")
        _require_aware_datetime("validated_at", self.validated_at)

    @property
    def state(self) -> CandidateState:
        return CandidateState.CONFIRMED

    @property
    def is_executable(self) -> bool:
        return False


def confirm_candidate(
    candidate: ActionCandidate,
    confirmation: UserConfirmation,
    *,
    action_authority: ActionAuthorityPort,
    clock: TrustedClock,
) -> ConfirmedActionCandidate:
    """Confirm an actionable candidate without silently turning it into a task."""

    current_time = read_trusted_time(clock)
    _validate_action_confirmation(
        candidate,
        confirmation,
        action_authority=action_authority,
        at=current_time,
    )
    return ConfirmedActionCandidate(candidate, confirmation, current_time)


@dataclass(frozen=True, slots=True, init=False)
class Task:
    task_id: str
    source: ConfirmedActionCandidate
    created_at: datetime
    state: TaskState = field(default=TaskState.OPEN, init=False)

    def __init__(self) -> None:
        raise ActionAuthorityRejectedError("Task must be created by a verified transition")

    @property
    def source_candidate_id(self) -> str:
        return self.source.candidate.candidate_id

    @property
    def description(self) -> str:
        return self.source.candidate.description

    @property
    def deadline(self) -> datetime | None:
        return self.source.candidate.deadline

    @property
    def priority(self) -> str | None:
        return self.source.candidate.priority


def _new_task(
    task_id: str,
    source: ConfirmedActionCandidate,
    created_at: datetime,
) -> Task:
    _require_text("task_id", task_id)
    if source.candidate.intent is not ActionIntent.COMMITMENT:
        raise IntentCannotBecomeTaskError("only a commitment can become a task")
    _require_aware_datetime("created_at", created_at)
    task = object.__new__(Task)
    object.__setattr__(task, "task_id", task_id)
    object.__setattr__(task, "source", source)
    object.__setattr__(task, "created_at", created_at)
    object.__setattr__(task, "state", TaskState.OPEN)
    return task


def to_task(
    source: ActionCandidate | ConfirmedActionCandidate,
    *,
    action_authority: ActionAuthorityPort,
    safety_authority: SafetyAuthorityPort,
    safety_permit: OrdinaryFlowPermit,
    clock: TrustedClock,
    task_id: str | None = None,
) -> Task:
    current_time = read_trusted_time(clock)
    if isinstance(source, ActionCandidate):
        raise ConfirmationRequiredError("an unconfirmed candidate cannot become a task")
    if source.candidate.intent is not ActionIntent.COMMITMENT:
        raise IntentCannotBecomeTaskError(
            f"{source.candidate.intent.value} cannot directly become a task"
        )
    candidate = source.candidate
    _validate_action_candidate_credentials(
        candidate,
        source.confirmation,
        action_authority=action_authority,
        at=current_time,
    )
    _consume_safety_permit(
        safety_permit,
        operation=OrdinaryOperation.TODO_CREATION,
        vault_id=candidate.vault_id,
        principal_id=candidate.principal_id,
        session_id=candidate.session_id,
        input_fingerprint=candidate.safety_input_fingerprint,
        safety_policy_generation=candidate.safety_policy_generation,
        safety_authority=safety_authority,
        at=current_time,
    )
    return _new_task(task_id or candidate.candidate_id, source, current_time)


@dataclass(frozen=True, slots=True, init=False)
class Experiment:
    experiment_id: str
    source: ConfirmedActionCandidate
    accepted_at: datetime
    state: ExperimentState = field(default=ExperimentState.ACCEPTED, init=False)

    def __init__(self) -> None:
        raise ActionAuthorityRejectedError("Experiment must be created by a verified transition")

    @property
    def terms(self) -> ExperimentProposal:
        terms = self.source.candidate.experiment
        if terms is None:  # pragma: no cover
            raise RuntimeError("accepted experiment is missing its terms")
        return terms

    @property
    def user_accepted(self) -> bool:
        return True


def _new_experiment(
    experiment_id: str,
    source: ConfirmedActionCandidate,
    accepted_at: datetime,
) -> Experiment:
    _require_text("experiment_id", experiment_id)
    candidate = source.candidate
    if candidate.intent is not ActionIntent.EXPERIMENT or candidate.experiment is None:
        raise InvalidActionCandidateError("only a complete experiment can be accepted")
    _require_aware_datetime("accepted_at", accepted_at)
    experiment = object.__new__(Experiment)
    object.__setattr__(experiment, "experiment_id", experiment_id)
    object.__setattr__(experiment, "source", source)
    object.__setattr__(experiment, "accepted_at", accepted_at)
    object.__setattr__(experiment, "state", ExperimentState.ACCEPTED)
    return experiment


def to_experiment(
    source: ActionCandidate | ConfirmedActionCandidate,
    *,
    action_authority: ActionAuthorityPort,
    safety_authority: SafetyAuthorityPort,
    safety_permit: OrdinaryFlowPermit,
    clock: TrustedClock,
    experiment_id: str | None = None,
) -> Experiment:
    current_time = read_trusted_time(clock)
    if isinstance(source, ActionCandidate):
        raise ConfirmationRequiredError("an unconfirmed experiment cannot be accepted")
    if source.candidate.intent is not ActionIntent.EXPERIMENT:
        raise InvalidActionCandidateError("the confirmed candidate is not an experiment")
    candidate = source.candidate
    _validate_action_candidate_credentials(
        candidate,
        source.confirmation,
        action_authority=action_authority,
        at=current_time,
    )
    _consume_safety_permit(
        safety_permit,
        operation=OrdinaryOperation.EXPERIMENT_ACCEPTANCE,
        vault_id=candidate.vault_id,
        principal_id=candidate.principal_id,
        session_id=candidate.session_id,
        input_fingerprint=candidate.safety_input_fingerprint,
        safety_policy_generation=candidate.safety_policy_generation,
        safety_authority=safety_authority,
        at=current_time,
    )
    return _new_experiment(experiment_id or candidate.candidate_id, source, current_time)


def accept_experiment(
    candidate: ActionCandidate,
    confirmation: UserConfirmation,
    *,
    action_authority: ActionAuthorityPort,
    safety_authority: SafetyAuthorityPort,
    safety_permit: OrdinaryFlowPermit,
    clock: TrustedClock,
    experiment_id: str | None = None,
) -> Experiment:
    current_time = read_trusted_time(clock)
    if candidate.intent is not ActionIntent.EXPERIMENT:
        raise InvalidActionCandidateError("the candidate is not an experiment")
    _validate_action_candidate_credentials(
        candidate,
        confirmation,
        action_authority=action_authority,
        at=current_time,
    )
    _consume_safety_permit(
        safety_permit,
        operation=OrdinaryOperation.EXPERIMENT_ACCEPTANCE,
        vault_id=candidate.vault_id,
        principal_id=candidate.principal_id,
        session_id=candidate.session_id,
        input_fingerprint=candidate.safety_input_fingerprint,
        safety_policy_generation=candidate.safety_policy_generation,
        safety_authority=safety_authority,
        at=current_time,
    )
    source = ConfirmedActionCandidate(candidate, confirmation, current_time)
    return _new_experiment(experiment_id or candidate.candidate_id, source, current_time)


@dataclass(frozen=True, slots=True)
class ExternalActionConfirmation:
    """Final user confirmation plus independently canonicalized external details."""

    user_confirmation: UserConfirmation
    action: ExternalActionSpec

    @property
    def candidate_id(self) -> str:
        return self.user_confirmation.candidate_id

    @property
    def confirmed_at(self) -> datetime:
        return self.user_confirmation.confirmed_at

    @property
    def expires_at(self) -> datetime:
        return self.user_confirmation.expires_at

    @property
    def scope(self) -> str:
        return self.action.scope

    @property
    def payload(self) -> Mapping[str, object]:
        return self.action.payload


@dataclass(frozen=True, slots=True)
class ExternalAuthorizationTransition:
    """Result of a persistent compare-and-set; ``None`` means unknown token."""

    state: ExternalActionState | None
    transitioned: bool

    def __post_init__(self) -> None:
        if self.state is not None and not isinstance(self.state, ExternalActionState):
            raise InvalidActionCandidateError("transition state must be an ExternalActionState")
        if not isinstance(self.transitioned, bool):
            raise InvalidActionCandidateError("transitioned must be a bool")
        if self.transitioned and self.state not in {
            ExternalActionState.CLAIMED,
            ExternalActionState.REVOKED,
            ExternalActionState.EXECUTED,
        }:
            raise InvalidActionCandidateError(
                "a successful transition must end in a terminal authorization state"
            )


@dataclass(frozen=True, slots=True)
class ExternalActionClaim:
    """Opaque persistent claim created only by a READY -> CLAIMED CAS."""

    claim_id: str
    authorization_id: str
    authorization_snapshot_fingerprint: str
    claimed_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("claim_id", self.claim_id),
            ("authorization_id", self.authorization_id),
            ("authorization_snapshot_fingerprint", self.authorization_snapshot_fingerprint),
        ):
            _require_text(name, value)
        _require_aware_datetime("claimed_at", self.claimed_at)


@dataclass(frozen=True, slots=True)
class ExternalClaimTransition:
    state: ExternalActionState | None
    transitioned: bool
    claim: ExternalActionClaim | None

    def __post_init__(self) -> None:
        if self.state is not None and not isinstance(self.state, ExternalActionState):
            raise InvalidActionCandidateError("claim transition state is invalid")
        if not isinstance(self.transitioned, bool):
            raise InvalidActionCandidateError("transitioned must be a bool")
        if self.transitioned:
            if self.state is not ExternalActionState.CLAIMED or self.claim is None:
                raise InvalidActionCandidateError(
                    "successful claim must produce CLAIMED and a token"
                )
        elif self.claim is not None:
            raise InvalidActionCandidateError("failed claim cannot expose a claim token")
        if self.claim is not None and not isinstance(self.claim, ExternalActionClaim):
            raise InvalidActionCandidateError("claim token has an invalid type")


@dataclass(frozen=True, slots=True)
class ExternalConnectorReceipt:
    receipt_id: str

    def __post_init__(self) -> None:
        _require_text("receipt_id", self.receipt_id)


@dataclass(frozen=True, slots=True)
class ExternalActionRequest:
    authorization_id: str
    claim_id: str
    scope: str
    payload: Mapping[str, object]


class ExternalActionConnector(Protocol):
    """Side-effect connector invoked only after a persistent claim succeeds."""

    def execute(self, request: ExternalActionRequest) -> ExternalConnectorReceipt: ...


@dataclass(frozen=True, slots=True)
class ExternalAuthorizationSnapshot:
    """Immutable first-registration terms for one authorization lineage."""

    authorization_id: str
    generation: int
    confirmation_id: str
    vault_id: str
    principal_id: str
    session_id: str
    safety_input_fingerprint: str
    safety_policy_generation: str
    purpose: str
    candidate_fingerprint: str
    policy_version: str
    policy_snapshot: str
    authorized_at: datetime
    confirmed_at: datetime
    confirmation_expires_at: datetime
    verdict_decided_at: datetime
    verdict_expires_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        for name, text_value in (
            ("authorization_id", self.authorization_id),
            ("confirmation_id", self.confirmation_id),
            ("vault_id", self.vault_id),
            ("principal_id", self.principal_id),
            ("session_id", self.session_id),
            ("safety_input_fingerprint", self.safety_input_fingerprint),
            ("safety_policy_generation", self.safety_policy_generation),
            ("purpose", self.purpose),
            ("candidate_fingerprint", self.candidate_fingerprint),
            ("policy_version", self.policy_version),
            ("policy_snapshot", self.policy_snapshot),
        ):
            _require_text(name, text_value)
        _require_positive_generation(self.generation)
        for name, timestamp in (
            ("authorized_at", self.authorized_at),
            ("confirmed_at", self.confirmed_at),
            ("confirmation_expires_at", self.confirmation_expires_at),
            ("verdict_decided_at", self.verdict_decided_at),
            ("verdict_expires_at", self.verdict_expires_at),
            ("expires_at", self.expires_at),
        ):
            _require_aware_datetime(name, timestamp)
        if self.authorized_at < self.confirmed_at:
            raise InvalidActionCandidateError("authorization cannot predate confirmation")
        if self.expires_at <= self.authorized_at:
            raise InvalidActionCandidateError("authorization must expire after registration")
        if self.confirmation_expires_at <= self.confirmed_at:
            raise InvalidActionCandidateError("persisted confirmation validity is invalid")
        if self.verdict_expires_at <= self.verdict_decided_at:
            raise InvalidActionCandidateError("persisted verdict validity is invalid")
        if self.expires_at != min(self.confirmation_expires_at, self.verdict_expires_at):
            raise InvalidActionCandidateError(
                "effective expiry must preserve both credential bounds"
            )

    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            "external-authorization-snapshot-v1",
            {
                "authorization_id": self.authorization_id,
                "generation": self.generation,
                "confirmation_id": self.confirmation_id,
                "vault_id": self.vault_id,
                "principal_id": self.principal_id,
                "session_id": self.session_id,
                "safety_input_fingerprint": self.safety_input_fingerprint,
                "safety_policy_generation": self.safety_policy_generation,
                "purpose": self.purpose,
                "candidate_fingerprint": self.candidate_fingerprint,
                "policy_version": self.policy_version,
                "policy_snapshot": self.policy_snapshot,
                "authorized_at": _canonical_datetime(self.authorized_at),
                "confirmed_at": _canonical_datetime(self.confirmed_at),
                "confirmation_expires_at": _canonical_datetime(self.confirmation_expires_at),
                "verdict_decided_at": _canonical_datetime(self.verdict_decided_at),
                "verdict_expires_at": _canonical_datetime(self.verdict_expires_at),
                "expires_at": _canonical_datetime(self.expires_at),
            },
        )

    @property
    def lineage_fingerprint(self) -> str:
        """Stable binding fields; excludes each caller's current evaluation time."""

        return _fingerprint(
            "external-authorization-lineage-v1",
            {
                "authorization_id": self.authorization_id,
                "generation": self.generation,
                "confirmation_id": self.confirmation_id,
                "vault_id": self.vault_id,
                "principal_id": self.principal_id,
                "session_id": self.session_id,
                "safety_input_fingerprint": self.safety_input_fingerprint,
                "safety_policy_generation": self.safety_policy_generation,
                "purpose": self.purpose,
                "candidate_fingerprint": self.candidate_fingerprint,
                "policy_version": self.policy_version,
                "policy_snapshot": self.policy_snapshot,
                "confirmed_at": _canonical_datetime(self.confirmed_at),
                "confirmation_expires_at": _canonical_datetime(self.confirmation_expires_at),
                "verdict_decided_at": _canonical_datetime(self.verdict_decided_at),
                "verdict_expires_at": _canonical_datetime(self.verdict_expires_at),
                "expires_at": _canonical_datetime(self.expires_at),
            },
        )


@dataclass(frozen=True, slots=True)
class ExternalAuthorizationRecord:
    """Persistent record returned by idempotent first-registration."""

    snapshot: ExternalAuthorizationSnapshot
    state: ExternalActionState

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ExternalAuthorizationSnapshot):
            raise InvalidActionCandidateError("authorization snapshot has an invalid type")
        if not isinstance(self.state, ExternalActionState):
            raise InvalidActionCandidateError("persistent authorization state is invalid")


class ExternalActionAuthorizationPort(Protocol):
    """Jobs persistence boundary for the full authorization lifecycle.

    Implementations key one immutable lineage by ``authorization_id`` alone.
    Registration is insert-if-absent and returns the original snapshot and state;
    it must never replace that snapshot or reset a terminal state. Revocation and
    consumption are atomic compare-and-set transitions and never create an unknown
    token. Consumption must compare ``consumed_at`` with the stored original expiry.
    """

    def ensure_ready_authorization(
        self,
        *,
        snapshot: ExternalAuthorizationSnapshot,
    ) -> ExternalAuthorizationRecord: ...

    def revoke_ready_authorization(
        self,
        *,
        authorization_id: str,
        authorization_snapshot_fingerprint: str,
        revoked_at: datetime,
    ) -> ExternalAuthorizationTransition: ...

    def claim_ready_authorization(
        self,
        *,
        authorization_id: str,
        authorization_snapshot_fingerprint: str,
        claimed_at: datetime,
    ) -> ExternalClaimTransition: ...

    def complete_claimed_authorization(
        self,
        *,
        claim: ExternalActionClaim,
        connector_receipt: ExternalConnectorReceipt,
        executed_at: datetime,
    ) -> ExternalAuthorizationTransition: ...


# Compatibility name for callers that initially implemented only the consume side.
ExternalActionConsumptionPort = ExternalActionAuthorizationPort


class ExternalActionAuthorization:
    """Stable, revocable authorization for one exact external side effect."""

    _authorization_id: str
    _candidate: ActionCandidate
    _confirmation: ExternalActionConfirmation
    _expires_at: datetime
    _generation: int
    _registration_snapshot: ExternalAuthorizationSnapshot
    _state: ExternalActionState

    __slots__ = (
        "_authorization_id",
        "_candidate",
        "_confirmation",
        "_expires_at",
        "_generation",
        "_registration_snapshot",
        "_state",
    )

    def __init__(self) -> None:
        raise ActionAuthorityRejectedError("external authorization requires a verified transition")

    @property
    def authorization_id(self) -> str:
        return self._authorization_id

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def candidate(self) -> ActionCandidate:
        return self._candidate

    @property
    def confirmation(self) -> ExternalActionConfirmation:
        return self._confirmation

    @property
    def expires_at(self) -> datetime:
        return self._expires_at

    @property
    def registration_snapshot(self) -> ExternalAuthorizationSnapshot:
        return self._registration_snapshot

    @property
    def state(self) -> ExternalActionState:
        return self._state

    @property
    def is_executable(self) -> bool:
        return self._state is ExternalActionState.READY

    @property
    def scope(self) -> str:
        proposal = self.candidate.external_action
        if proposal is None:  # pragma: no cover
            raise RuntimeError("authorized external action is missing its proposal")
        return proposal.scope

    @property
    def payload(self) -> Mapping[str, object]:
        proposal = self.candidate.external_action
        if proposal is None:  # pragma: no cover
            raise RuntimeError("authorized external action is missing its proposal")
        return proposal.payload

    def _assert_current(self, at: datetime) -> None:
        _require_aware_datetime("at", at)
        if self._state is ExternalActionState.REVOKED:
            raise ExternalActionAuthorizationRevokedError("authorization is revoked")
        if self._state is ExternalActionState.CLAIMED:
            raise ExternalActionClaimedError("authorization is already claimed")
        if self._state is ExternalActionState.EXECUTED:
            raise ExternalActionAlreadyConsumedError("authorization was already consumed")
        if at < self.confirmation.confirmed_at:
            raise ExternalActionExecutionTimeError("execution predates user confirmation")
        if at >= self.expires_at:
            raise ExternalActionAuthorizationExpiredError("authorization has expired")

    def _mark_executed(self) -> None:
        self._state = ExternalActionState.EXECUTED

    def _mark_claimed(self) -> None:
        self._state = ExternalActionState.CLAIMED

    def _validate_revocation_time(self, at: datetime) -> None:
        _require_aware_datetime("revoked_at", at)
        if at < self.confirmation.confirmed_at:
            raise ExternalActionExecutionTimeError("revocation predates user confirmation")
        if self._state is ExternalActionState.CLAIMED:
            raise ExternalActionClaimedError("claimed authorization cannot be revoked")
        if self._state is ExternalActionState.EXECUTED:
            raise ExternalActionAlreadyConsumedError("executed authorization cannot be revoked")

    def _restore_persistent_state(self, state: ExternalActionState) -> None:
        if not isinstance(state, ExternalActionState):
            raise InvalidActionCandidateError("persistent authorization state is invalid")
        self._state = state

    def _restore_persistent_record(self, record: ExternalAuthorizationRecord) -> None:
        self._registration_snapshot = record.snapshot
        self._expires_at = record.snapshot.expires_at
        self._restore_persistent_state(record.state)


def _new_external_action_authorization(
    candidate: ActionCandidate,
    confirmation: ExternalActionConfirmation,
    *,
    authorized_at: datetime,
) -> ExternalActionAuthorization:
    if candidate.intent is not ActionIntent.EXTERNAL_ACTION:
        raise InvalidActionCandidateError("the candidate is not an external action")
    proposal = candidate.external_action
    if proposal is None:  # pragma: no cover
        raise InvalidActionCandidateError("external action proposal is missing")
    if confirmation.action != proposal:
        raise ConfirmationMismatchError("external scope or payload changed after confirmation")
    verdict = candidate.safety_verdict
    if verdict is None:  # pragma: no cover
        raise ActionSafetyRequiredError("a current ALLOWED verdict is required")
    binding = confirmation.user_confirmation
    authorization_id = _fingerprint(
        "external-authorization-id-v1",
        {
            "confirmation_id": binding.confirmation_id,
            "vault_id": candidate.vault_id,
            "principal_id": candidate.principal_id,
            "session_id": candidate.session_id,
            "safety_input_fingerprint": candidate.safety_input_fingerprint,
            "safety_policy_generation": candidate.safety_policy_generation,
            "purpose": candidate.purpose,
            "candidate_id": candidate.candidate_id,
            "candidate_fingerprint": candidate.fingerprint,
            "policy_version": candidate.policy_version,
            "policy_snapshot": candidate.policy_snapshot,
        },
    )
    expires_at = min(binding.expires_at, verdict.expires_at)
    snapshot = ExternalAuthorizationSnapshot(
        authorization_id=authorization_id,
        generation=binding.generation,
        confirmation_id=binding.confirmation_id,
        vault_id=candidate.vault_id,
        principal_id=candidate.principal_id,
        session_id=candidate.session_id,
        safety_input_fingerprint=candidate.safety_input_fingerprint,
        safety_policy_generation=candidate.safety_policy_generation,
        purpose=candidate.purpose,
        candidate_fingerprint=candidate.fingerprint,
        policy_version=candidate.policy_version,
        policy_snapshot=candidate.policy_snapshot,
        authorized_at=authorized_at,
        confirmed_at=binding.confirmed_at,
        confirmation_expires_at=binding.expires_at,
        verdict_decided_at=verdict.decided_at,
        verdict_expires_at=verdict.expires_at,
        expires_at=expires_at,
    )
    authorization = object.__new__(ExternalActionAuthorization)
    authorization._authorization_id = authorization_id
    authorization._generation = binding.generation
    authorization._candidate = candidate
    authorization._confirmation = confirmation
    authorization._expires_at = expires_at
    authorization._registration_snapshot = snapshot
    authorization._state = ExternalActionState.READY
    return authorization


def authorize_external_action(
    candidate: ActionCandidate,
    confirmation: ExternalActionConfirmation,
    authorization_port: ExternalActionAuthorizationPort,
    *,
    action_authority: ActionAuthorityPort,
    safety_authority: SafetyAuthorityPort,
    safety_permit: OrdinaryFlowPermit,
    clock: TrustedClock,
) -> ExternalActionAuthorization:
    current_time = read_trusted_time(clock)
    if candidate.intent is not ActionIntent.EXTERNAL_ACTION:
        raise InvalidActionCandidateError("the candidate is not an external action")
    proposal = candidate.external_action
    if proposal is None or confirmation.action != proposal:
        raise ConfirmationMismatchError("external scope or payload changed after confirmation")
    _validate_action_candidate_credentials(
        candidate,
        confirmation.user_confirmation,
        action_authority=action_authority,
        at=current_time,
    )
    _consume_safety_permit(
        safety_permit,
        operation=OrdinaryOperation.EXTERNAL_ACTION_AUTHORIZATION,
        vault_id=candidate.vault_id,
        principal_id=candidate.principal_id,
        session_id=candidate.session_id,
        input_fingerprint=candidate.safety_input_fingerprint,
        safety_policy_generation=candidate.safety_policy_generation,
        safety_authority=safety_authority,
        at=current_time,
    )
    authorization = _new_external_action_authorization(
        candidate,
        confirmation,
        authorized_at=current_time,
    )
    record = authorization_port.ensure_ready_authorization(
        snapshot=authorization.registration_snapshot,
    )
    if (
        record.snapshot.lineage_fingerprint
        != authorization.registration_snapshot.lineage_fingerprint
    ):
        raise ExternalActionAuthorizationLineageError(
            "authorization ID is already bound to different immutable registration terms"
        )
    authorization._restore_persistent_record(record)
    return authorization


def revoke_external_action_authorization(
    authorization: ExternalActionAuthorization,
    authorization_port: ExternalActionAuthorizationPort,
    *,
    clock: TrustedClock,
) -> None:
    """Persistently revoke READY without allowing reconstruction to reset it."""

    current_time = read_trusted_time(clock)
    authorization._validate_revocation_time(current_time)
    transition = authorization_port.revoke_ready_authorization(
        authorization_id=authorization.authorization_id,
        authorization_snapshot_fingerprint=authorization.registration_snapshot.fingerprint,
        revoked_at=current_time,
    )
    if transition.state is ExternalActionState.EXECUTED:
        authorization._restore_persistent_state(ExternalActionState.EXECUTED)
        raise ExternalActionAlreadyConsumedError("executed authorization cannot be revoked")
    if transition.state is ExternalActionState.CLAIMED:
        authorization._restore_persistent_state(ExternalActionState.CLAIMED)
        raise ExternalActionClaimedError("claimed authorization cannot be revoked")
    if transition.state is not ExternalActionState.REVOKED:
        raise ExternalActionAuthorizationRevokedError(
            "persistent authorization is unknown or could not be revoked"
        )
    authorization._restore_persistent_state(ExternalActionState.REVOKED)


@dataclass(frozen=True, slots=True, init=False)
class ExternalActionExecution:
    authorization_id: str
    generation: int
    candidate_fingerprint: str
    executed_at: datetime
    receipt_id: str
    claim_id: str
    state: ExternalActionState = field(default=ExternalActionState.EXECUTED, init=False)

    def __init__(self) -> None:
        raise ActionAuthorityRejectedError(
            "external execution requires claim-first connector execution"
        )

    @property
    def is_executable(self) -> bool:
        return False


def _new_external_action_execution(
    *,
    authorization_id: str,
    generation: int,
    candidate_fingerprint: str,
    executed_at: datetime,
    receipt_id: str,
    claim_id: str,
) -> ExternalActionExecution:
    _require_text("authorization_id", authorization_id)
    _require_positive_generation(generation)
    _require_text("candidate_fingerprint", candidate_fingerprint)
    _require_text("receipt_id", receipt_id)
    _require_text("claim_id", claim_id)
    _require_aware_datetime("executed_at", executed_at)
    execution = object.__new__(ExternalActionExecution)
    object.__setattr__(execution, "authorization_id", authorization_id)
    object.__setattr__(execution, "generation", generation)
    object.__setattr__(execution, "candidate_fingerprint", candidate_fingerprint)
    object.__setattr__(execution, "executed_at", executed_at)
    object.__setattr__(execution, "receipt_id", receipt_id)
    object.__setattr__(execution, "claim_id", claim_id)
    object.__setattr__(execution, "state", ExternalActionState.EXECUTED)
    return execution


def execute_external_action(
    authorization: ExternalActionAuthorization,
    authorization_port: ExternalActionAuthorizationPort,
    connector: ExternalActionConnector,
    *,
    action_authority: ActionAuthorityPort,
    safety_authority: SafetyAuthorityPort,
    safety_permit: OrdinaryFlowPermit,
    clock: TrustedClock,
) -> ExternalActionExecution:
    """Claim atomically, invoke the connector, then finalize the exact claim."""

    current_time = read_trusted_time(clock)
    candidate = authorization.candidate
    _validate_action_candidate_credentials(
        candidate,
        authorization.confirmation.user_confirmation,
        action_authority=action_authority,
        at=current_time,
    )
    _consume_safety_permit(
        safety_permit,
        operation=OrdinaryOperation.EXTERNAL_ACTION_EXECUTION,
        vault_id=candidate.vault_id,
        principal_id=candidate.principal_id,
        session_id=candidate.session_id,
        input_fingerprint=candidate.safety_input_fingerprint,
        safety_policy_generation=candidate.safety_policy_generation,
        safety_authority=safety_authority,
        at=current_time,
    )
    authorization._assert_current(current_time)
    claim_transition = authorization_port.claim_ready_authorization(
        authorization_id=authorization.authorization_id,
        authorization_snapshot_fingerprint=authorization.registration_snapshot.fingerprint,
        claimed_at=current_time,
    )
    if (
        not claim_transition.transitioned
        or claim_transition.state is not ExternalActionState.CLAIMED
        or claim_transition.claim is None
    ):
        if claim_transition.state is ExternalActionState.REVOKED:
            authorization._restore_persistent_state(ExternalActionState.REVOKED)
            raise ExternalActionAuthorizationRevokedError("persistent authorization is revoked")
        if claim_transition.state is ExternalActionState.CLAIMED:
            authorization._restore_persistent_state(ExternalActionState.CLAIMED)
            raise ExternalActionClaimedError("external authorization is already claimed")
        raise ExternalActionAlreadyConsumedError("external authorization cannot be claimed")
    claim = claim_transition.claim
    if (
        claim.authorization_id != authorization.authorization_id
        or claim.authorization_snapshot_fingerprint
        != authorization.registration_snapshot.fingerprint
        or claim.claimed_at != current_time
    ):
        raise ExternalActionClaimedError("persistent port returned a mismatched claim")
    authorization._mark_claimed()
    connector_receipt = connector.execute(
        ExternalActionRequest(
            authorization_id=authorization.authorization_id,
            claim_id=claim.claim_id,
            scope=authorization.scope,
            payload=authorization.payload,
        )
    )
    if not isinstance(connector_receipt, ExternalConnectorReceipt):
        raise ExternalActionClaimedError("connector returned an invalid receipt")
    completion = authorization_port.complete_claimed_authorization(
        claim=claim,
        connector_receipt=connector_receipt,
        executed_at=current_time,
    )
    if not completion.transitioned or completion.state is not ExternalActionState.EXECUTED:
        raise ExternalActionClaimedError("claimed external action could not be finalized")
    authorization._mark_executed()
    return _new_external_action_execution(
        authorization_id=authorization.authorization_id,
        generation=authorization.generation,
        candidate_fingerprint=candidate.fingerprint,
        executed_at=current_time,
        receipt_id=connector_receipt.receipt_id,
        claim_id=claim.claim_id,
    )


@dataclass(frozen=True, slots=True)
class GoalCandidate:
    goal_id: str
    vault_id: str
    principal_id: str
    session_id: str
    safety_input_fingerprint: str
    safety_policy_generation: str
    purpose: str
    policy_version: str
    policy_snapshot: str
    description: str

    def __post_init__(self) -> None:
        for name, value in (
            ("goal_id", self.goal_id),
            ("vault_id", self.vault_id),
            ("principal_id", self.principal_id),
            ("session_id", self.session_id),
            ("purpose", self.purpose),
            ("policy_version", self.policy_version),
            ("policy_snapshot", self.policy_snapshot),
            ("description", self.description),
        ):
            _require_text(name, value)
        object.__setattr__(
            self,
            "safety_input_fingerprint",
            normalize_input_fingerprint(self.safety_input_fingerprint),
        )
        _require_text("safety_policy_generation", self.safety_policy_generation)

    @property
    def fingerprint(self) -> str:
        return _fingerprint("goal-candidate-v1", {"description": self.description})


@dataclass(frozen=True, slots=True)
class EndorsedGoal:
    candidate: GoalCandidate
    confirmation: UserConfirmation
    endorsed_at: datetime

    def __post_init__(self) -> None:
        _require_aware_datetime("endorsed_at", self.endorsed_at)

    @property
    def goal_id(self) -> str:
        return self.candidate.goal_id

    @property
    def description(self) -> str:
        return self.candidate.description


def endorse_goal(
    candidate: GoalCandidate,
    confirmation: UserConfirmation,
    *,
    action_authority: ActionAuthorityPort,
    clock: TrustedClock,
) -> EndorsedGoal:
    current_time = read_trusted_time(clock)
    _validate_user_confirmation(
        confirmation,
        action_authority=action_authority,
        expected_candidate_id=candidate.goal_id,
        expected_vault_id=candidate.vault_id,
        expected_principal_id=candidate.principal_id,
        expected_session_id=candidate.session_id,
        expected_purpose=candidate.purpose,
        expected_fingerprint=candidate.fingerprint,
        expected_policy_version=candidate.policy_version,
        expected_policy_snapshot=candidate.policy_snapshot,
        at=current_time,
    )
    return EndorsedGoal(candidate, confirmation, current_time)


@dataclass(frozen=True, slots=True)
class IfThenPlanCandidate:
    plan_id: str
    goal: EndorsedGoal
    observable_cue: str
    then_action: str
    cue_is_observable: bool
    action_is_small_and_concrete: bool
    safety_verdict: ActionSafetyVerdict | None = None
    location_or_time: str | None = None
    likely_barrier: str | None = None
    fallback: str | None = None
    review_at: datetime | None = None
    reminder_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text("plan_id", self.plan_id)
        if not isinstance(self.goal, EndorsedGoal):
            raise GoalNotEndorsedError("if-then plans require an endorsed user goal")
        _require_text("observable_cue", self.observable_cue)
        _require_text("then_action", self.then_action)
        if not isinstance(self.cue_is_observable, bool):
            raise InvalidIfThenPlanError("cue_is_observable must be a bool")
        if not isinstance(self.action_is_small_and_concrete, bool):
            raise InvalidIfThenPlanError("action_is_small_and_concrete must be a bool")
        if not self.cue_is_observable:
            raise InvalidIfThenPlanError("the if cue must be concretely observable")
        if not self.action_is_small_and_concrete:
            raise InvalidIfThenPlanError("the then action must be a concrete small behavior")
        for name, text_value in (
            ("location_or_time", self.location_or_time),
            ("likely_barrier", self.likely_barrier),
            ("fallback", self.fallback),
        ):
            if text_value is not None:
                _require_text(name, text_value)
        for name, timestamp in (("review_at", self.review_at), ("reminder_at", self.reminder_at)):
            if timestamp is not None:
                _require_aware_datetime(name, timestamp)
        if self.safety_verdict is not None:
            self._validate_verdict_binding(self.safety_verdict)

    @property
    def vault_id(self) -> str:
        return self.goal.candidate.vault_id

    @property
    def principal_id(self) -> str:
        return self.goal.candidate.principal_id

    @property
    def session_id(self) -> str:
        return self.goal.candidate.session_id

    @property
    def safety_policy_generation(self) -> str:
        return self.goal.candidate.safety_policy_generation

    @property
    def safety_input_fingerprint(self) -> str:
        return self.goal.candidate.safety_input_fingerprint

    @property
    def purpose(self) -> str:
        return self.goal.candidate.purpose

    @property
    def policy_version(self) -> str:
        return self.goal.candidate.policy_version

    @property
    def policy_snapshot(self) -> str:
        return self.goal.candidate.policy_snapshot

    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            "if-then-plan-v1",
            {
                "goal_id": self.goal.goal_id,
                "goal_fingerprint": self.goal.candidate.fingerprint,
                "observable_cue": self.observable_cue,
                "then_action": self.then_action,
                "cue_is_observable": self.cue_is_observable,
                "action_is_small_and_concrete": self.action_is_small_and_concrete,
                "location_or_time": self.location_or_time,
                "likely_barrier": self.likely_barrier,
                "fallback": self.fallback,
                "review_at": _canonical_datetime(self.review_at),
                "reminder_at": _canonical_datetime(self.reminder_at),
            },
        )

    def _validate_verdict_binding(self, verdict: ActionSafetyVerdict) -> None:
        if (
            verdict.subject_id != self.plan_id
            or verdict.vault_id != self.vault_id
            or verdict.principal_id != self.principal_id
            or verdict.session_id != self.session_id
            or verdict.purpose != self.purpose
            or verdict.candidate_fingerprint != self.fingerprint
            or verdict.policy_version != self.policy_version
            or verdict.policy_snapshot != self.policy_snapshot
        ):
            raise ActionSafetySubjectMismatchError("verdict does not bind this exact if-then plan")

    def with_safety_verdict(self, verdict: ActionSafetyVerdict) -> Self:
        return replace(self, safety_verdict=verdict)

    @property
    def state(self) -> IfThenPlanState:
        return IfThenPlanState.CANDIDATE

    @property
    def is_executable(self) -> bool:
        return False


def _validate_if_then_credentials(
    candidate: IfThenPlanCandidate,
    confirmation: UserConfirmation,
    *,
    action_authority: ActionAuthorityPort,
    at: datetime,
) -> None:
    fingerprint = candidate.fingerprint
    _validate_user_confirmation(
        confirmation,
        action_authority=action_authority,
        expected_candidate_id=candidate.plan_id,
        expected_vault_id=candidate.vault_id,
        expected_principal_id=candidate.principal_id,
        expected_session_id=candidate.session_id,
        expected_purpose=candidate.purpose,
        expected_fingerprint=fingerprint,
        expected_policy_version=candidate.policy_version,
        expected_policy_snapshot=candidate.policy_snapshot,
        at=at,
    )
    _require_allowed_safety_verdict(
        candidate.safety_verdict,
        action_authority=action_authority,
        expected_subject_id=candidate.plan_id,
        expected_vault_id=candidate.vault_id,
        expected_principal_id=candidate.principal_id,
        expected_session_id=candidate.session_id,
        expected_purpose=candidate.purpose,
        expected_fingerprint=fingerprint,
        expected_policy_version=candidate.policy_version,
        expected_policy_snapshot=candidate.policy_snapshot,
        at=at,
    )
    goal = candidate.goal.candidate
    _validate_user_confirmation(
        candidate.goal.confirmation,
        action_authority=action_authority,
        expected_candidate_id=goal.goal_id,
        expected_vault_id=goal.vault_id,
        expected_principal_id=goal.principal_id,
        expected_session_id=goal.session_id,
        expected_purpose=goal.purpose,
        expected_fingerprint=goal.fingerprint,
        expected_policy_version=goal.policy_version,
        expected_policy_snapshot=goal.policy_snapshot,
        at=at,
    )


@dataclass(frozen=True, slots=True, init=False)
class IfThenPlan:
    candidate: IfThenPlanCandidate
    confirmation: UserConfirmation
    accepted_at: datetime
    state: IfThenPlanState = field(default=IfThenPlanState.ACCEPTED, init=False)

    def __init__(self) -> None:
        raise ActionAuthorityRejectedError("IfThenPlan must be created by a verified transition")

    @property
    def user_accepted(self) -> bool:
        return True


def _new_if_then_plan(
    candidate: IfThenPlanCandidate,
    confirmation: UserConfirmation,
    accepted_at: datetime,
) -> IfThenPlan:
    _require_aware_datetime("accepted_at", accepted_at)
    plan = object.__new__(IfThenPlan)
    object.__setattr__(plan, "candidate", candidate)
    object.__setattr__(plan, "confirmation", confirmation)
    object.__setattr__(plan, "accepted_at", accepted_at)
    object.__setattr__(plan, "state", IfThenPlanState.ACCEPTED)
    return plan


def accept_if_then_plan(
    candidate: IfThenPlanCandidate,
    confirmation: UserConfirmation,
    *,
    action_authority: ActionAuthorityPort,
    safety_authority: SafetyAuthorityPort,
    safety_permit: OrdinaryFlowPermit,
    clock: TrustedClock,
) -> IfThenPlan:
    current_time = read_trusted_time(clock)
    _validate_if_then_credentials(
        candidate,
        confirmation,
        action_authority=action_authority,
        at=current_time,
    )
    _consume_safety_permit(
        safety_permit,
        operation=OrdinaryOperation.IF_THEN_PLAN_ACCEPTANCE,
        vault_id=candidate.vault_id,
        principal_id=candidate.principal_id,
        session_id=candidate.session_id,
        input_fingerprint=candidate.safety_input_fingerprint,
        safety_policy_generation=candidate.safety_policy_generation,
        safety_authority=safety_authority,
        at=current_time,
    )
    return _new_if_then_plan(candidate, confirmation, current_time)
