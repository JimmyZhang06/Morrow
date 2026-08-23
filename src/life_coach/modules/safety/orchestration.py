"""Mandatory composition boundary between safety routing and ordinary features."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TypeVar

from ._time import require_aware
from .authority import (
    SafetyAuthorityPort,
    SafetyAuthorityReceipt,
    SafetyAuthorityVerificationError,
    SafetyBinding,
    SafetyPermitClaims,
    TrustedClock,
    read_trusted_time,
)
from .gateway import SafetyDecision, SafetyRoute
from .output_policy import NonDiagnosticOutputPolicy

_ResultT = TypeVar("_ResultT")


class OrdinaryOperation(StrEnum):
    """Ordinary operations that a non-ordinary safety route must block."""

    REFLECTION_DEEPENING = "reflection_deepening"
    TODO_CREATION = "todo_creation"
    EXPERIMENT_ACCEPTANCE = "experiment_acceptance"
    IF_THEN_PLAN_ACCEPTANCE = "if_then_plan_acceptance"
    EXTERNAL_ACTION_AUTHORIZATION = "external_action_authorization"
    EXTERNAL_ACTION_EXECUTION = "external_action_execution"


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


class SafetyBindingMismatchError(PermissionError):
    """Raised when signed safety scope differs from the current request scope."""


@dataclass(frozen=True, slots=True)
class OrdinaryFlowPermit:
    """Opaque, operation-specific proof issued from an ordinary decision."""

    claims: SafetyPermitClaims
    authority_receipt: SafetyAuthorityReceipt
    route: SafetyRoute = field(default=SafetyRoute.ORDINARY_COACH, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.claims, SafetyPermitClaims):
            raise TypeError("claims must be SafetyPermitClaims")
        if not isinstance(self.authority_receipt, SafetyAuthorityReceipt):
            raise TypeError("authority_receipt must be a SafetyAuthorityReceipt")
        try:
            OrdinaryOperation(self.claims.operation)
        except ValueError as error:
            raise ValueError("permit claims contain an invalid operation") from error

    @property
    def binding(self) -> SafetyBinding:
        return self.claims.binding

    @property
    def operation(self) -> OrdinaryOperation:
        return OrdinaryOperation(self.claims.operation)

    @property
    def policy_generation(self) -> str:
        return self.claims.policy_generation

    @property
    def issued_at(self) -> datetime:
        return self.claims.issued_at

    @property
    def expires_at(self) -> datetime:
        return self.claims.expires_at

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
    expected_binding: SafetyBinding
    clock: TrustedClock
    authority: SafetyAuthorityPort
    output_policy: NonDiagnosticOutputPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.decision, SafetyDecision):
            raise TypeError("decision must be a SafetyDecision")
        if not isinstance(self.expected_binding, SafetyBinding):
            raise TypeError("expected_binding must be a SafetyBinding")
        if not isinstance(self.clock, TrustedClock):
            raise TypeError("clock must implement TrustedClock")
        if not isinstance(self.authority, SafetyAuthorityPort):
            raise TypeError("authority must implement SafetyAuthorityPort")
        if not isinstance(self.output_policy, NonDiagnosticOutputPolicy):
            raise TypeError("output_policy must be a NonDiagnosticOutputPolicy")
        if (
            self.output_policy.vault_id != self.expected_binding.vault_id
            or self.output_policy.session_id != self.expected_binding.session_id
        ):
            raise SafetyBindingMismatchError(
                "output policy scope does not match the current safety binding"
            )

    def authorize_ordinary(self, operation: OrdinaryOperation) -> OrdinaryFlowPermit:
        """Issue a permit only for a current, explicitly ordinary decision."""

        at = read_trusted_time(self.clock)
        return self._authorize_at(operation, at=at)

    def _authorize_at(
        self,
        operation: OrdinaryOperation,
        *,
        at: datetime,
    ) -> OrdinaryFlowPermit:
        if not isinstance(operation, OrdinaryOperation):
            raise TypeError("operation must be an OrdinaryOperation")
        require_aware(at, field_name="at")
        if self.decision.binding != self.expected_binding:
            raise SafetyBindingMismatchError(
                "safety decision binding does not match the current request"
            )
        if (
            self.authority.verify_decision(
                self.decision.claims,
                self.decision.authority_receipt,
            )
            is not True
        ):
            raise SafetyAuthorityVerificationError("safety decision authority is invalid")
        if not self.decision.is_current(at=at):
            raise StaleSafetyDecisionError("SafetyDecision is not current")
        if self.decision.route is not SafetyRoute.ORDINARY_COACH:
            raise OrdinaryFlowBlockedError(operation=operation, decision=self.decision)

        claims = SafetyPermitClaims(
            binding=self.expected_binding,
            operation=operation.value,
            policy_generation=self.expected_binding.policy_generation,
            issued_at=at,
            expires_at=self.decision.valid_until,
            decision_receipt=self.decision.authority_receipt,
        )
        receipt = self.authority.issue_permit(claims)
        if not isinstance(receipt, SafetyAuthorityReceipt):
            raise SafetyAuthorityVerificationError("authority returned an invalid permit receipt")
        permit = OrdinaryFlowPermit(claims=claims, authority_receipt=receipt)
        if self.authority.verify_permit(claims, receipt) is not True:
            raise SafetyAuthorityVerificationError("issued ordinary permit could not be verified")
        return permit

    def execute_ordinary(
        self,
        operation: OrdinaryOperation,
        *,
        execute: Callable[[OrdinaryFlowPermit], _ResultT],
    ) -> _ResultT:
        """Guard and execute one ordinary feature through the composition boundary.

        Application adapters use this entry point instead of invoking reflection
        or action transitions directly.  The callback is never entered unless a
        current ``ORDINARY_COACH`` decision issued an operation-specific permit.
        """

        at = read_trusted_time(self.clock)
        permit = self._authorize_at(operation, at=at)
        return execute(permit)

    def release_output(self, text: str) -> str:
        """Return text only after every configured output layer clears it."""

        return self.output_policy.release(text)
