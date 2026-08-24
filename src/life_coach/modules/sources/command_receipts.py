"""Vault-scoped idempotency receipts for Source API mutations."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from life_coach.jobs.contracts import IdempotencyConflict
from life_coach.jobs.payloads import (
    VaultRequestFingerprint,
    validate_routing_name,
    validate_vault_request_fingerprint,
)

from .models import SourceCommandReceipt


class CrossVaultSourceCommandError(PermissionError):
    """A receipt repository received a command for another Vault."""


@dataclass(frozen=True, slots=True)
class SourceCommandReceiptSpec:
    vault_id: uuid.UUID
    operation: str
    client_key_hash: VaultRequestFingerprint
    request_hash: VaultRequestFingerprint
    resource_id: uuid.UUID
    resource_revision_id: uuid.UUID | None
    result_revision_no: int | None
    source_generation: int
    policy_epoch: int

    def __post_init__(self) -> None:
        validate_routing_name(self.operation)
        validate_vault_request_fingerprint(self.client_key_hash, vault_id=self.vault_id)
        validate_vault_request_fingerprint(self.request_hash, vault_id=self.vault_id)
        if self.result_revision_no is not None and self.result_revision_no < 1:
            raise ValueError("result revision must be positive")
        if self.source_generation < 0 or self.policy_epoch < 0:
            raise ValueError("Source command fences cannot be negative")


@dataclass(frozen=True, slots=True)
class SourceCommandReservation:
    receipt_id: uuid.UUID
    created: bool
    resource_id: uuid.UUID
    resource_revision_id: uuid.UUID | None
    result_revision_no: int | None
    source_generation: int
    policy_epoch: int


class SourceCommandReceiptRepository:
    """Reserve or replay one content-free command result inside the caller transaction."""

    def __init__(self, session: AsyncSession, vault_id: uuid.UUID) -> None:
        self._session = session
        self.vault_id = vault_id

    async def find(
        self,
        *,
        operation: str,
        client_key_hash: VaultRequestFingerprint,
        request_hash: VaultRequestFingerprint,
    ) -> SourceCommandReservation | None:
        validate_routing_name(operation)
        validate_vault_request_fingerprint(client_key_hash, vault_id=self.vault_id)
        validate_vault_request_fingerprint(request_hash, vault_id=self.vault_id)
        existing = (
            await self._session.scalars(
                select(SourceCommandReceipt).where(
                    SourceCommandReceipt.vault_id == self.vault_id,
                    SourceCommandReceipt.operation == operation,
                    SourceCommandReceipt.client_key_hash == str(client_key_hash),
                )
            )
        ).one_or_none()
        if existing is None:
            return None
        if not secrets.compare_digest(existing.request_hash, str(request_hash)):
            raise IdempotencyConflict("Source idempotency key was reused with a different request")
        return self._projection(existing, created=False)

    async def reserve(self, spec: SourceCommandReceiptSpec) -> SourceCommandReservation:
        if spec.vault_id != self.vault_id:
            raise CrossVaultSourceCommandError("Source command belongs to another Vault")
        statement = (
            pg_insert(SourceCommandReceipt)
            .values(
                vault_id=spec.vault_id,
                operation=spec.operation,
                client_key_hash=str(spec.client_key_hash),
                request_hash=str(spec.request_hash),
                resource_id=spec.resource_id,
                resource_revision_id=spec.resource_revision_id,
                result_revision_no=spec.result_revision_no,
                source_generation=spec.source_generation,
                policy_epoch=spec.policy_epoch,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    SourceCommandReceipt.vault_id,
                    SourceCommandReceipt.operation,
                    SourceCommandReceipt.client_key_hash,
                ]
            )
            .returning(SourceCommandReceipt)
        )
        inserted = (await self._session.execute(statement)).scalar_one_or_none()
        if inserted is not None:
            return self._projection(inserted, created=True)

        existing = await self.find(
            operation=spec.operation,
            client_key_hash=spec.client_key_hash,
            request_hash=spec.request_hash,
        )
        if existing is None:  # pragma: no cover - database uniqueness invariant
            raise RuntimeError("Source idempotency receipt disappeared after conflict")
        return existing

    @staticmethod
    def _projection(
        receipt: SourceCommandReceipt,
        *,
        created: bool,
    ) -> SourceCommandReservation:
        return SourceCommandReservation(
            receipt_id=receipt.id,
            created=created,
            resource_id=receipt.resource_id,
            resource_revision_id=receipt.resource_revision_id,
            result_revision_no=receipt.result_revision_no,
            source_generation=receipt.source_generation,
            policy_epoch=receipt.policy_epoch,
        )


__all__ = [
    "CrossVaultSourceCommandError",
    "SourceCommandReceiptRepository",
    "SourceCommandReceiptSpec",
    "SourceCommandReservation",
]
