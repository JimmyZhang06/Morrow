"""Safe domain errors for consent operations."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


class ConsentError(Exception):
    """Base class for consent-domain failures."""


class InvalidConsentPurpose(ConsentError, ValueError):
    """A purpose must be a non-empty, bounded identifier."""


class InvalidConsentAction(ConsentError, ValueError):
    """The requested event is neither a grant nor a revocation."""


class ConsentRecordImmutable(ConsentError):
    """Consent history is append-only and cannot be updated or deleted."""


@dataclass(eq=False)
class ConsentDenied(ConsentError):
    """The requested processing purpose has no current applicable grant."""

    vault_id: UUID
    purpose: str
    source_document_id: UUID | None = None

    def __str__(self) -> str:
        return "consent is not granted for the requested purpose"


@dataclass(eq=False)
class ConsentScopeNotFound(ConsentError):
    """A Source scope is absent or belongs to a different vault.

    The error intentionally does not reveal which case applies.
    """

    vault_id: UUID
    source_document_id: UUID

    def __str__(self) -> str:
        return "the requested Source scope was not found in this vault"
