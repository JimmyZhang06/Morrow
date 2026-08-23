"""Pure domain rules for user-governed actions.

These objects perform neither extraction nor external side effects. They define the
policy boundary between a suggestion and something the user explicitly chose.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Self, cast


class ActionDomainError(ValueError):
    """Base class for rejected action-domain transitions."""


class InvalidActionCandidateError(ActionDomainError):
    """Raised when a candidate is incomplete or internally inconsistent."""


class ConfirmationRequiredError(ActionDomainError):
    """Raised when a transition lacks explicit confirmation from the user."""


class ConfirmationMismatchError(ActionDomainError):
    """Raised when confirmation does not bind the object being transitioned."""


class IntentCannotBecomeTaskError(ActionDomainError):
    """Raised when an intent classification is not eligible for task creation."""


class GoalNotEndorsedError(ActionDomainError):
    """Raised when an if-then plan is proposed for an unendorsed goal."""


class InvalidIfThenPlanError(ActionDomainError):
    """Raised when an if-then plan lacks an observable cue or small behavior."""


class ActionSafetyGateError(ActionDomainError):
    """Base class for fail-closed action safety gate failures."""


class ActionSafetyRequiredError(ActionSafetyGateError):
    """Raised when a safety-sensitive candidate has no verdict."""


class ActionSafetyBlockedError(ActionSafetyGateError):
    """Raised when policy blocks the proposed behavior."""


class ActionSafetySubjectMismatchError(ActionSafetyGateError):
    """Raised when a verdict was issued for a different candidate."""


class ExternalActionAlreadyConsumedError(ActionDomainError):
    """Raised when a one-use external authorization is consumed again."""


class ExternalActionExecutionTimeError(ActionDomainError):
    """Raised when execution is reported before the user's confirmation."""


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
    """Actor asserted by a confirmation event."""

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
    """``READY`` is authorized, not proof that a side effect occurred."""

    READY = "ready"
    EXECUTED = "executed"


class IfThenPlanState(StrEnum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"


class ActionSafetyOutcome(StrEnum):
    """Non-scored policy outcome for a proposed behavior."""

    ALLOWED = "allowed"
    BLOCKED = "blocked"


def _require_text(name: str, value: str) -> None:
    if not value.strip():
        raise InvalidActionCandidateError(f"{name} must not be blank")


def _require_aware_datetime(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidActionCandidateError(f"{name} must include a UTC offset")


@dataclass(frozen=True, slots=True)
class ActionSafetyVerdict:
    """Fail-closed, non-scored safety decision for one exact candidate.

    ``ALLOWED`` means the safety policy found the proposed behavior suitable for a
    low-risk consumer coaching flow. It is not a clinical risk level or prediction.
    """

    subject_id: str
    outcome: ActionSafetyOutcome
    policy_version: str
    decided_at: datetime
    reason: str

    def __post_init__(self) -> None:
        _require_text("subject_id", self.subject_id)
        if not isinstance(self.outcome, ActionSafetyOutcome):
            raise InvalidActionCandidateError("outcome must be an ActionSafetyOutcome enum value")
        _require_text("policy_version", self.policy_version)
        _require_text("reason", self.reason)
        _require_aware_datetime("decided_at", self.decided_at)


def _require_allowed_safety_verdict(
    verdict: ActionSafetyVerdict | None,
    *,
    expected_subject_id: str,
) -> None:
    if verdict is None:
        raise ActionSafetyRequiredError("an ALLOWED action safety verdict is required")
    if verdict.subject_id != expected_subject_id:
        raise ActionSafetySubjectMismatchError("safety verdict targets a different candidate")
    if verdict.outcome is not ActionSafetyOutcome.ALLOWED:
        raise ActionSafetyBlockedError("action safety policy blocked the proposed behavior")


@dataclass(frozen=True, slots=True)
class UserConfirmation:
    """A confirmation event that must be explicit and user-originated to count."""

    candidate_id: str
    confirmed_at: datetime
    explicit: bool = True
    actor: ConfirmationActor = ConfirmationActor.USER

    def __post_init__(self) -> None:
        _require_text("candidate_id", self.candidate_id)
        _require_aware_datetime("confirmed_at", self.confirmed_at)
        if not isinstance(self.explicit, bool):
            raise InvalidActionCandidateError("explicit must be a bool")
        if not isinstance(self.actor, ConfirmationActor):
            raise InvalidActionCandidateError("actor must be a ConfirmationActor enum value")


def _validate_user_confirmation(
    confirmation: UserConfirmation,
    *,
    expected_candidate_id: str,
) -> None:
    if confirmation.candidate_id != expected_candidate_id:
        raise ConfirmationMismatchError("confirmation targets a different candidate")
    if not confirmation.explicit or confirmation.actor is not ConfirmationActor.USER:
        raise ConfirmationRequiredError("an explicit user confirmation is required")


@dataclass(frozen=True, slots=True)
class ExperimentProposal:
    """Terms that must be visible before an experiment can be accepted."""

    rationale: str
    cost: str
    exit_plan: str
    is_reversible: bool = False

    def __post_init__(self) -> None:
        _require_text("rationale", self.rationale)
        _require_text("cost", self.cost)
        _require_text("exit_plan", self.exit_plan)
        if not isinstance(self.is_reversible, bool):
            raise InvalidActionCandidateError("is_reversible must be a bool")
        if not self.is_reversible:
            raise InvalidActionCandidateError("experiments must be explicitly reversible")


def _normalize_json(value: object) -> object:
    """Return a detached JSON-compatible value or reject the payload."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidActionCandidateError("external payload numbers must be finite")
        return value
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        normalized: dict[str, object] = {}
        for key, child in mapping.items():
            if not isinstance(key, str):
                raise InvalidActionCandidateError("external payload keys must be strings")
            normalized[key] = _normalize_json(child)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        return [_normalize_json(child) for child in sequence]
    raise InvalidActionCandidateError("external payload must contain only JSON values")


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
        """Return a detached, read-only view of the payload."""

        decoded: object = json.loads(self._payload_json)
        if not isinstance(decoded, dict):  # pragma: no cover - construction protects this
            raise RuntimeError("canonical external payload is not an object")
        return MappingProxyType(cast(dict[str, object], decoded))

    @property
    def canonical_payload(self) -> str:
        return self._payload_json


@dataclass(frozen=True, slots=True)
class ActionCandidate:
    """A non-executable candidate with no inferred deadline or priority."""

    candidate_id: str
    intent: ActionIntent
    description: str
    deadline: datetime | None = None
    priority: str | None = None
    experiment: ExperimentProposal | None = None
    external_action: ExternalActionSpec | None = None
    safety_verdict: ActionSafetyVerdict | None = None

    def __post_init__(self) -> None:
        _require_text("candidate_id", self.candidate_id)
        _require_text("description", self.description)
        if not isinstance(self.intent, ActionIntent):
            raise InvalidActionCandidateError("intent must be an ActionIntent enum value")
        if self.priority is not None:
            _require_text("priority", self.priority)
        if self.deadline is not None:
            _require_aware_datetime("deadline", self.deadline)

        if self.intent is ActionIntent.EXPERIMENT:
            if self.experiment is None:
                raise InvalidActionCandidateError(
                    "experiment candidates require rationale, cost, and an exit plan"
                )
            _require_allowed_safety_verdict(
                self.safety_verdict,
                expected_subject_id=self.candidate_id,
            )
        elif self.experiment is not None:
            raise InvalidActionCandidateError(
                "experiment terms are only valid for experiment candidates"
            )
        elif self.safety_verdict is not None:
            raise InvalidActionCandidateError(
                "action safety verdict is only valid here for experiment candidates"
            )

        if self.intent is ActionIntent.EXTERNAL_ACTION:
            if self.external_action is None:
                raise InvalidActionCandidateError(
                    "external action candidates require a concrete scope and payload"
                )
        elif self.external_action is not None:
            raise InvalidActionCandidateError(
                "external action details are only valid for external action candidates"
            )

    @property
    def state(self) -> CandidateState:
        return CandidateState.CANDIDATE

    @property
    def is_executable(self) -> bool:
        return False

    @classmethod
    def commitment(
        cls,
        candidate_id: str,
        description: str,
        *,
        deadline: datetime | None = None,
        priority: str | None = None,
    ) -> Self:
        return cls(
            candidate_id=candidate_id,
            intent=ActionIntent.COMMITMENT,
            description=description,
            deadline=deadline,
            priority=priority,
        )

    @classmethod
    def wish(cls, candidate_id: str, description: str) -> Self:
        return cls(candidate_id=candidate_id, intent=ActionIntent.WISH, description=description)

    @classmethod
    def concern(cls, candidate_id: str, description: str) -> Self:
        return cls(candidate_id=candidate_id, intent=ActionIntent.CONCERN, description=description)

    @classmethod
    def idea(cls, candidate_id: str, description: str) -> Self:
        return cls(candidate_id=candidate_id, intent=ActionIntent.IDEA, description=description)

    @classmethod
    def experiment_candidate(
        cls,
        candidate_id: str,
        description: str,
        *,
        rationale: str,
        cost: str,
        exit_plan: str,
        is_reversible: bool = False,
        safety_verdict: ActionSafetyVerdict | None = None,
    ) -> Self:
        return cls(
            candidate_id=candidate_id,
            intent=ActionIntent.EXPERIMENT,
            description=description,
            experiment=ExperimentProposal(
                rationale=rationale,
                cost=cost,
                exit_plan=exit_plan,
                is_reversible=is_reversible,
            ),
            safety_verdict=safety_verdict,
        )

    @classmethod
    def external_action_candidate(
        cls,
        candidate_id: str,
        description: str,
        *,
        scope: str,
        payload: Mapping[str, object],
    ) -> Self:
        return cls(
            candidate_id=candidate_id,
            intent=ActionIntent.EXTERNAL_ACTION,
            description=description,
            external_action=ExternalActionSpec(scope=scope, payload=payload),
        )


@dataclass(frozen=True, slots=True)
class ConfirmedActionCandidate:
    """A candidate accompanied by valid explicit user confirmation."""

    candidate: ActionCandidate
    confirmation: UserConfirmation

    def __post_init__(self) -> None:
        if self.candidate.intent is ActionIntent.EXTERNAL_ACTION:
            raise ConfirmationRequiredError(
                "external actions require scope-and-payload-bound confirmation"
            )
        _validate_user_confirmation(
            self.confirmation,
            expected_candidate_id=self.candidate.candidate_id,
        )

    @property
    def state(self) -> CandidateState:
        return CandidateState.CONFIRMED

    @property
    def is_executable(self) -> bool:
        return False


def confirm_candidate(
    candidate: ActionCandidate,
    confirmation: UserConfirmation,
) -> ConfirmedActionCandidate:
    """Confirm a candidate without silently turning it into a task."""

    return ConfirmedActionCandidate(candidate=candidate, confirmation=confirmation)


@dataclass(frozen=True, slots=True)
class Task:
    """A task created only from a confirmed explicit commitment."""

    task_id: str
    source: ConfirmedActionCandidate
    state: TaskState = field(default=TaskState.OPEN, init=False)

    def __post_init__(self) -> None:
        _require_text("task_id", self.task_id)
        if self.source.candidate.intent is not ActionIntent.COMMITMENT:
            raise IntentCannotBecomeTaskError(
                "only a commitment can become a task; re-express the intent as a commitment"
            )

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


def to_task(
    source: ActionCandidate | ConfirmedActionCandidate,
    *,
    task_id: str | None = None,
) -> Task:
    """Convert a confirmed commitment, and only a commitment, to a task."""

    if isinstance(source, ActionCandidate):
        raise ConfirmationRequiredError("an unconfirmed candidate cannot become a task")
    candidate = source.candidate
    if candidate.intent is not ActionIntent.COMMITMENT:
        raise IntentCannotBecomeTaskError(f"{candidate.intent.value} cannot directly become a task")
    resolved_task_id = candidate.candidate_id if task_id is None else task_id
    return Task(task_id=resolved_task_id, source=source)


@dataclass(frozen=True, slots=True)
class Experiment:
    """An experiment whose reason, cost, and exit route the user accepted."""

    experiment_id: str
    source: ConfirmedActionCandidate
    state: ExperimentState = field(default=ExperimentState.ACCEPTED, init=False)

    def __post_init__(self) -> None:
        _require_text("experiment_id", self.experiment_id)
        candidate = self.source.candidate
        if candidate.intent is not ActionIntent.EXPERIMENT or candidate.experiment is None:
            raise InvalidActionCandidateError(
                "only a complete experiment candidate can be accepted"
            )

    @property
    def terms(self) -> ExperimentProposal:
        terms = self.source.candidate.experiment
        if terms is None:  # pragma: no cover - construction protects this
            raise RuntimeError("accepted experiment is missing its terms")
        return terms

    @property
    def user_accepted(self) -> bool:
        return True


def to_experiment(
    source: ActionCandidate | ConfirmedActionCandidate,
    *,
    experiment_id: str | None = None,
) -> Experiment:
    """Convert an explicitly confirmed experiment candidate to an experiment."""

    if isinstance(source, ActionCandidate):
        raise ConfirmationRequiredError("an unconfirmed experiment cannot be accepted")
    candidate = source.candidate
    if candidate.intent is not ActionIntent.EXPERIMENT:
        raise InvalidActionCandidateError("the confirmed candidate is not an experiment")
    resolved_experiment_id = candidate.candidate_id if experiment_id is None else experiment_id
    return Experiment(experiment_id=resolved_experiment_id, source=source)


def accept_experiment(
    candidate: ActionCandidate,
    confirmation: UserConfirmation,
    *,
    experiment_id: str | None = None,
) -> Experiment:
    """Confirm and accept an experiment without converting it to a Todo."""

    return to_experiment(
        confirm_candidate(candidate, confirmation),
        experiment_id=experiment_id,
    )


@dataclass(frozen=True, slots=True, init=False)
class ExternalActionConfirmation:
    """User confirmation bound to one exact external scope and payload."""

    user_confirmation: UserConfirmation
    action: ExternalActionSpec

    def __init__(
        self,
        candidate_id: str,
        confirmed_at: datetime,
        *,
        scope: str,
        payload: Mapping[str, object],
        explicit: bool = True,
        actor: ConfirmationActor = ConfirmationActor.USER,
    ) -> None:
        object.__setattr__(
            self,
            "user_confirmation",
            UserConfirmation(
                candidate_id=candidate_id,
                confirmed_at=confirmed_at,
                explicit=explicit,
                actor=actor,
            ),
        )
        object.__setattr__(self, "action", ExternalActionSpec(scope=scope, payload=payload))

    @property
    def candidate_id(self) -> str:
        return self.user_confirmation.candidate_id

    @property
    def confirmed_at(self) -> datetime:
        return self.user_confirmation.confirmed_at

    @property
    def scope(self) -> str:
        return self.action.scope

    @property
    def payload(self) -> Mapping[str, object]:
        return self.action.payload


class ExternalActionAuthorization:
    """Single-use authorization for one externally executed side effect.

    This in-memory aggregate rejects sequential reuse. A persistence adapter must
    atomically compare-and-set ``READY`` to ``EXECUTED`` before dispatching a side
    effect so concurrent workers cannot both consume the same authorization.
    """

    __slots__ = ("_candidate", "_confirmation", "_state")

    def __init__(
        self,
        candidate: ActionCandidate,
        confirmation: ExternalActionConfirmation,
    ) -> None:
        if candidate.intent is not ActionIntent.EXTERNAL_ACTION:
            raise InvalidActionCandidateError("the candidate is not an external action")
        proposal = candidate.external_action
        if proposal is None:  # pragma: no cover - ActionCandidate protects this
            raise InvalidActionCandidateError("external action proposal is missing")
        _validate_user_confirmation(
            confirmation.user_confirmation,
            expected_candidate_id=candidate.candidate_id,
        )
        if confirmation.action != proposal:
            raise ConfirmationMismatchError(
                "external confirmation must match the exact scope and payload"
            )
        self._candidate = candidate
        self._confirmation = confirmation
        self._state = ExternalActionState.READY

    @property
    def candidate(self) -> ActionCandidate:
        return self._candidate

    @property
    def confirmation(self) -> ExternalActionConfirmation:
        return self._confirmation

    @property
    def state(self) -> ExternalActionState:
        return self._state

    @property
    def is_executable(self) -> bool:
        return self._state is ExternalActionState.READY

    @property
    def scope(self) -> str:
        proposal = self.candidate.external_action
        if proposal is None:  # pragma: no cover - construction protects this
            raise RuntimeError("authorized external action is missing its proposal")
        return proposal.scope

    @property
    def payload(self) -> Mapping[str, object]:
        proposal = self.candidate.external_action
        if proposal is None:  # pragma: no cover - construction protects this
            raise RuntimeError("authorized external action is missing its proposal")
        return proposal.payload

    def _mark_executed(self) -> None:
        if self._state is not ExternalActionState.READY:
            raise ExternalActionAlreadyConsumedError(
                "external action authorization has already been consumed"
            )
        self._state = ExternalActionState.EXECUTED


def authorize_external_action(
    candidate: ActionCandidate,
    confirmation: ExternalActionConfirmation,
) -> ExternalActionAuthorization:
    """Make one exact external action executable after final confirmation."""

    return ExternalActionAuthorization(candidate=candidate, confirmation=confirmation)


@dataclass(frozen=True, slots=True)
class ExternalActionExecution:
    """Receipt that an authorized side effect was executed elsewhere."""

    authorization: ExternalActionAuthorization
    executed_at: datetime
    receipt_id: str
    state: ExternalActionState = field(default=ExternalActionState.EXECUTED, init=False)

    def __post_init__(self) -> None:
        _require_text("receipt_id", self.receipt_id)
        _require_aware_datetime("executed_at", self.executed_at)
        if self.executed_at < self.authorization.confirmation.confirmed_at:
            raise ExternalActionExecutionTimeError(
                "external action execution cannot predate user confirmation"
            )
        if self.authorization.state is not ExternalActionState.EXECUTED:
            raise ConfirmationRequiredError(
                "external action authorization must be consumed before recording execution"
            )

    @property
    def is_executable(self) -> bool:
        return False


def record_external_action_execution(
    authorization: ExternalActionAuthorization,
    *,
    executed_at: datetime,
    receipt_id: str,
) -> ExternalActionExecution:
    """Consume an authorization and record a side effect performed elsewhere.

    Distributed callers must persist an atomic ``READY`` -> ``EXECUTED``
    compare-and-set before the real side effect; this transition is the pure-domain
    counterpart of that persistence rule.
    """

    _require_text("receipt_id", receipt_id)
    _require_aware_datetime("executed_at", executed_at)
    if executed_at < authorization.confirmation.confirmed_at:
        raise ExternalActionExecutionTimeError(
            "external action execution cannot predate user confirmation"
        )
    authorization._mark_executed()
    return ExternalActionExecution(
        authorization=authorization,
        executed_at=executed_at,
        receipt_id=receipt_id,
    )


@dataclass(frozen=True, slots=True)
class GoalCandidate:
    """A goal proposal not yet eligible for an if-then plan."""

    goal_id: str
    description: str

    def __post_init__(self) -> None:
        _require_text("goal_id", self.goal_id)
        _require_text("description", self.description)


@dataclass(frozen=True, slots=True)
class EndorsedGoal:
    """A goal the user explicitly recognized as their own."""

    candidate: GoalCandidate
    confirmation: UserConfirmation

    def __post_init__(self) -> None:
        _validate_user_confirmation(
            self.confirmation,
            expected_candidate_id=self.candidate.goal_id,
        )

    @property
    def goal_id(self) -> str:
        return self.candidate.goal_id

    @property
    def description(self) -> str:
        return self.candidate.description


def endorse_goal(candidate: GoalCandidate, confirmation: UserConfirmation) -> EndorsedGoal:
    """Record explicit user endorsement of an existing goal."""

    return EndorsedGoal(candidate=candidate, confirmation=confirmation)


@dataclass(frozen=True, slots=True)
class IfThenPlanCandidate:
    """One proposed if-then plan for an endorsed goal.

    The evidence flags come from a verifier. Optional time, location, review, and
    reminder values remain ``None`` unless supplied and are bound by confirmation.
    """

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
        _require_allowed_safety_verdict(
            self.safety_verdict,
            expected_subject_id=self.plan_id,
        )
        for name, text_value in (
            ("location_or_time", self.location_or_time),
            ("likely_barrier", self.likely_barrier),
            ("fallback", self.fallback),
        ):
            if text_value is not None:
                _require_text(name, text_value)
        for name, timestamp in (
            ("review_at", self.review_at),
            ("reminder_at", self.reminder_at),
        ):
            if timestamp is not None:
                _require_aware_datetime(name, timestamp)

    @property
    def state(self) -> IfThenPlanState:
        return IfThenPlanState.CANDIDATE

    @property
    def is_executable(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class IfThenPlan:
    """An if-then plan accepted by the user as one immutable proposal."""

    candidate: IfThenPlanCandidate
    confirmation: UserConfirmation
    state: IfThenPlanState = field(default=IfThenPlanState.ACCEPTED, init=False)

    def __post_init__(self) -> None:
        _validate_user_confirmation(
            self.confirmation,
            expected_candidate_id=self.candidate.plan_id,
        )

    @property
    def user_accepted(self) -> bool:
        return True


def accept_if_then_plan(
    candidate: IfThenPlanCandidate,
    confirmation: UserConfirmation,
) -> IfThenPlan:
    """Accept one concrete if-then plan after explicit user confirmation."""

    return IfThenPlan(candidate=candidate, confirmation=confirmation)
