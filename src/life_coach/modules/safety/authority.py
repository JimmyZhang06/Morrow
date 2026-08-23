"""Trusted time and opaque authority contracts for safety decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from ._time import require_aware
from ._validation import require_bool

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class SafetyAuthorityVerificationError(PermissionError):
    """Raised when opaque authority cannot verify exact immutable claims."""


@runtime_checkable
class TrustedClock(Protocol):
    """Application-supplied authoritative clock.

    Implementations are responsible for detecting source rollback.  Domain
    entry points call ``now`` exactly once per operation and never accept a
    caller-provided timestamp as an authority substitute.
    """

    def now(self) -> datetime:
        """Return one timezone-aware authoritative instant."""


def read_trusted_time(clock: TrustedClock) -> datetime:
    """Read a trusted clock exactly once and reject an ambiguous instant."""

    instant = clock.now()
    require_aware(instant, field_name="trusted_clock.now()")
    return instant


def _normalize_required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def normalize_input_fingerprint(value: object) -> str:
    """Return one canonical lowercase SHA-256 hex fingerprint."""

    normalized = _normalize_required_text(value, field_name="input_fingerprint").lower()
    if normalized.startswith("sha256:"):
        normalized = normalized.removeprefix("sha256:")
    if _SHA256_HEX.fullmatch(normalized) is None:
        raise ValueError("input_fingerprint must be a SHA-256 fingerprint")
    return normalized


@dataclass(frozen=True, slots=True)
class SafetyBinding:
    """Exact tenant, principal, session, input, and policy authority scope."""

    vault_id: str
    principal_id: str
    session_id: str
    input_fingerprint: str
    policy_generation: str

    def __post_init__(self) -> None:
        for field_name in ("vault_id", "principal_id", "session_id", "policy_generation"):
            object.__setattr__(
                self,
                field_name,
                _normalize_required_text(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "input_fingerprint",
            normalize_input_fingerprint(self.input_fingerprint),
        )


@dataclass(frozen=True, slots=True)
class SafetyAuthorityReceipt:
    """Opaque evidence whose authenticity only the authority port can decide."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "value",
            _normalize_required_text(self.value, field_name="authority_receipt"),
        )


@dataclass(frozen=True, slots=True)
class SafetyDecisionClaims:
    """Immutable claims covered by one safety-decision receipt."""

    binding: SafetyBinding
    route: str
    priority: str
    may_suggest_selected_supporter: bool
    evaluated_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.binding, SafetyBinding):
            raise TypeError("binding must be a SafetyBinding")
        object.__setattr__(self, "route", _normalize_required_text(self.route, field_name="route"))
        object.__setattr__(
            self,
            "priority",
            _normalize_required_text(self.priority, field_name="priority"),
        )
        require_bool(
            self.may_suggest_selected_supporter,
            field_name="may_suggest_selected_supporter",
        )
        require_aware(self.evaluated_at, field_name="evaluated_at")
        require_aware(self.expires_at, field_name="expires_at")
        if self.expires_at <= self.evaluated_at:
            raise ValueError("expires_at must be after evaluated_at")


@dataclass(frozen=True, slots=True)
class SafetyPermitClaims:
    """Operation-specific immutable claims covered by one permit receipt."""

    binding: SafetyBinding
    operation: str
    policy_generation: str
    issued_at: datetime
    expires_at: datetime
    decision_receipt: SafetyAuthorityReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.binding, SafetyBinding):
            raise TypeError("binding must be a SafetyBinding")
        object.__setattr__(
            self,
            "operation",
            _normalize_required_text(self.operation, field_name="operation"),
        )
        object.__setattr__(
            self,
            "policy_generation",
            _normalize_required_text(
                self.policy_generation,
                field_name="policy_generation",
            ),
        )
        if self.policy_generation != self.binding.policy_generation:
            raise ValueError("permit policy_generation must match its binding")
        require_aware(self.issued_at, field_name="issued_at")
        require_aware(self.expires_at, field_name="expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        if not isinstance(self.decision_receipt, SafetyAuthorityReceipt):
            raise TypeError("decision_receipt must be a SafetyAuthorityReceipt")


@runtime_checkable
class SafetyAuthorityPort(Protocol):
    """Opaque issuance, verification, and single-use consumption boundary."""

    def issue_decision(self, claims: SafetyDecisionClaims) -> SafetyAuthorityReceipt:
        """Issue a receipt over every exact decision claim."""

    def verify_decision(
        self,
        claims: SafetyDecisionClaims,
        receipt: SafetyAuthorityReceipt,
    ) -> bool:
        """Verify an exact decision; unknown or changed claims return ``False``."""

    def issue_permit(self, claims: SafetyPermitClaims) -> SafetyAuthorityReceipt:
        """Issue only after internally verifying the parent is registered and ordinary.

        Implementations must resolve ``claims.decision_receipt``, require exact
        binding and expiry containment, and require the registered decision
        route to be ``ordinary_coach``.  Orchestrator checks are defense in
        depth, not authority for issuance.
        """

    def verify_permit(
        self,
        claims: SafetyPermitClaims,
        receipt: SafetyAuthorityReceipt,
    ) -> bool:
        """Verify exact unconsumed permit claims without consuming the permit."""

    def verify_and_consume_permit(
        self,
        claims: SafetyPermitClaims,
        receipt: SafetyAuthorityReceipt,
    ) -> bool:
        """Atomically verify and consume once; replay must return ``False``."""
