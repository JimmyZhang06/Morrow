"""Safe domain errors for vault identity operations."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


class IdentityError(Exception):
    """Base class for identity-domain failures."""


@dataclass(eq=False)
class VaultNotFound(IdentityError):
    """The vault is absent, tombstoned, or outside the caller's boundary."""

    vault_id: UUID

    def __str__(self) -> str:
        return f"vault {self.vault_id} was not found"


@dataclass(eq=False)
class StaleVaultSnapshot(IdentityError):
    """A worker snapshot no longer matches the vault's privacy fences."""

    vault_id: UUID
    expected_policy_epoch: int
    expected_source_generation: int

    def __str__(self) -> str:
        return f"vault snapshot for {self.vault_id} is stale"
