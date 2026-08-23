"""Trusted receipt boundary for otherwise forgeable action-domain assertions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class ActionCredentialKind(StrEnum):
    USER_CONFIRMATION = "user_confirmation"
    SAFETY_VERDICT = "safety_verdict"


@dataclass(frozen=True, slots=True)
class OpaqueActionReceipt:
    """Opaque lookup handle; constructing one does not make it trusted."""

    receipt_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.receipt_id, str) or not self.receipt_id.strip():
            raise ValueError("receipt_id must not be blank")


@dataclass(frozen=True, slots=True)
class ActionAuthorityClaims:
    """Exact security-relevant claims stored behind an opaque receipt."""

    kind: ActionCredentialKind
    credential_id: str
    generation: int
    subject_id: str
    vault_id: str
    principal_id: str
    session_id: str
    purpose: str
    input_fingerprint: str
    policy_version: str
    policy_snapshot: str
    issued_at: datetime
    expires_at: datetime
    payload_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActionCredentialKind):
            raise TypeError("kind must be an ActionCredentialKind")
        for name, value in (
            ("credential_id", self.credential_id),
            ("subject_id", self.subject_id),
            ("vault_id", self.vault_id),
            ("principal_id", self.principal_id),
            ("session_id", self.session_id),
            ("purpose", self.purpose),
            ("input_fingerprint", self.input_fingerprint),
            ("policy_version", self.policy_version),
            ("policy_snapshot", self.policy_snapshot),
            ("payload_fingerprint", self.payload_fingerprint),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be blank")
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        for name, timestamp in (("issued_at", self.issued_at), ("expires_at", self.expires_at)):
            if (
                not isinstance(timestamp, datetime)
                or timestamp.tzinfo is None
                or timestamp.utcoffset() is None
            ):
                raise ValueError(f"{name} must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")


class ActionAuthorityPort(Protocol):
    """Verify that an opaque receipt was issued for exactly these claims."""

    def verify_receipt(
        self,
        receipt: OpaqueActionReceipt,
        *,
        expected_claims: ActionAuthorityClaims,
        current_time: datetime,
    ) -> bool: ...
