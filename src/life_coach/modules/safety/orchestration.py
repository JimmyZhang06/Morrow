"""Mandatory composition boundary between safety routing and ordinary features."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TypeVar

from ._time import require_aware
from .gateway import SafetyDecision, SafetyRoute
from .output_policy import NonDiagnosticOutputPolicy

_ResultT = TypeVar("_ResultT")


class OrdinaryOperation(StrEnum):
    """Ordinary operations that a non-ordinary safety route must block."""

    REFLECTION_DEEPENING = "reflection_deepening"
    TODO_CREATION = "todo_creation"
    EXPERIMENT_ACCEPTANCE = "experiment_acceptance"
    EXTERNAL_ACTION_AUTHORIZATION = "external_action_authorization"


class OrdinaryFlowBlockedError(PermissionError):
    """Raised before an ordinary operation can run outside the ordinary route."""

    def __init__(self, *, operation: OrdinaryOperation, decision: SafetyDecision) -> None:
        super().__init__(
            f"{operation.value} is blocked while safety route is {decision.route.value}"
        )
        self.operation = operation
        self.decision = decision


class StaleSafetyDecisionError(PermissionError):
    """Raised when an operation tries to reuse an out-of-window decision."""


@dataclass(frozen=True, slots=True)
class OrdinaryFlowPermit:
    """Short-lived proof that one ordinary operation passed the safety boundary."""

    operation: OrdinaryOperation
    issued_at: datetime
    expires_at: datetime
    route: SafetyRoute = field(default=SafetyRoute.ORDINARY_COACH, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.operation, OrdinaryOperation):
            raise TypeError("operation must be an OrdinaryOperation")
        require_aware(self.issued_at, field_name="issued_at")
        require_aware(self.expires_at, field_name="expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")

    def is_current(self, *, at: datetime) -> bool:
        """Use the same half-open validity window as the source decision."""

        require_aware(at, field_name="at")
        return self.issued_at <= at < self.expires_at


@dataclass(frozen=True, slots=True)
class SafetyOrchestrationContract:
    """Enforce routing before ordinary work and output policy before display.

    Feature/application services must obtain an ``OrdinaryFlowPermit`` for the
    concrete operation instead of treating ``SafetyDecision`` as advisory.
    Consumer-visible text has only one release path here, which delegates to the
    fail-closed non-diagnostic output policy.
    """

    decision: SafetyDecision
    output_policy: NonDiagnosticOutputPolicy = field(default_factory=NonDiagnosticOutputPolicy)

    def __post_init__(self) -> None:
        if not isinstance(self.decision, SafetyDecision):
            raise TypeError("decision must be a SafetyDecision")
        if not isinstance(self.output_policy, NonDiagnosticOutputPolicy):
            raise TypeError("output_policy must be a NonDiagnosticOutputPolicy")

    def authorize_ordinary(
        self,
        operation: OrdinaryOperation,
        *,
        at: datetime,
    ) -> OrdinaryFlowPermit:
        """Issue a permit only for a current, explicitly ordinary decision."""

        if not isinstance(operation, OrdinaryOperation):
            raise TypeError("operation must be an OrdinaryOperation")
        require_aware(at, field_name="at")
        if not self.decision.is_current(at=at):
            raise StaleSafetyDecisionError("SafetyDecision is not current")
        if self.decision.route is not SafetyRoute.ORDINARY_COACH:
            raise OrdinaryFlowBlockedError(operation=operation, decision=self.decision)
        return OrdinaryFlowPermit(
            operation=operation,
            issued_at=at,
            expires_at=self.decision.valid_until,
        )

    def execute_ordinary(
        self,
        operation: OrdinaryOperation,
        *,
        at: datetime,
        execute: Callable[[OrdinaryFlowPermit], _ResultT],
    ) -> _ResultT:
        """Guard and execute one ordinary feature through the composition boundary.

        Application adapters use this entry point instead of invoking reflection
        or action transitions directly.  The callback is never entered unless a
        current ``ORDINARY_COACH`` decision issued an operation-specific permit.
        """

        permit = self.authorize_ordinary(operation, at=at)
        return execute(permit)

    def release_output(self, text: str) -> str:
        """Return text only after every configured output layer clears it."""

        return self.output_policy.release(text)
