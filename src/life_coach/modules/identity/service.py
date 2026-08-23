"""Pure synchronous Session services for vault privacy fences.

The functions deliberately flush but never commit. Transaction ownership stays
with the API/job composition root so a domain write and its jobs can be atomic.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from life_coach.modules.identity.exceptions import StaleVaultSnapshot, VaultNotFound
from life_coach.modules.identity.models import CreatedBy, DataClass, Vault
from life_coach.shared.database import utc_now


@dataclass(frozen=True, slots=True)
class VaultSnapshot:
    """Consent and Source fences captured for a unit of background work."""

    vault_id: UUID
    policy_epoch: int
    source_generation: int


def create_vault(
    session: Session,
    *,
    vault_id: UUID | None = None,
    created_by: CreatedBy = CreatedBy.USER,
    data_class: DataClass = DataClass.SENSITIVE,
) -> Vault:
    """Create a vault without taking ownership of the surrounding transaction."""

    values: dict[str, object] = {"created_by": created_by, "data_class": data_class}
    if vault_id is not None:
        values["id"] = vault_id
    vault = Vault(**values)
    session.add(vault)
    session.flush()
    return vault


def get_vault(
    session: Session,
    vault_id: UUID,
    *,
    include_deleted: bool = False,
    for_update: bool = False,
) -> Vault:
    """Load a vault while treating tombstoned rows as absent by default."""

    statement = select(Vault).where(Vault.id == vault_id)
    if not include_deleted:
        statement = statement.where(Vault.deleted_at.is_(None))
    if for_update:
        statement = statement.with_for_update()
    vault = session.scalar(statement)
    if vault is None:
        raise VaultNotFound(vault_id)
    return vault


def _increment_counter(session: Session, vault_id: UUID, column_name: str) -> int:
    if session.get_bind().dialect.name == "postgresql":
        # PostgreSQL revokes direct fence-column UPDATE from the runtime role. These
        # narrowly scoped SECURITY DEFINER functions can only advance the current vault
        # by one and preserve FORCE RLS for all other access.
        function = (
            func.life_coach_private.advance_policy_epoch
            if column_name == "policy_epoch"
            else func.life_coach_private.advance_source_generation
        )
        value = session.scalar(select(function(vault_id)))
        if value is None:
            raise VaultNotFound(vault_id)
        return int(value)

    column = getattr(Vault, column_name)
    statement = (
        update(Vault)
        .where(Vault.id == vault_id, Vault.deleted_at.is_(None))
        .values({column_name: column + 1, "updated_at": utc_now()})
        .returning(column)
        .execution_options(synchronize_session="fetch")
    )
    value = session.execute(statement).scalar_one_or_none()
    if value is None:
        raise VaultNotFound(vault_id)
    return int(value)


def increment_policy_epoch(session: Session, vault_id: UUID) -> int:
    """Atomically advance the vault policy fence and return its new value."""

    return _increment_counter(session, vault_id, "policy_epoch")


def increment_source_generation(session: Session, vault_id: UUID) -> int:
    """Atomically advance the Source mutation fence and return its new value."""

    return _increment_counter(session, vault_id, "source_generation")


def capture_vault_snapshot(session: Session, vault_id: UUID) -> VaultSnapshot:
    """Capture both privacy fences in one database read."""

    row = session.execute(
        select(Vault.policy_epoch, Vault.source_generation).where(
            Vault.id == vault_id,
            Vault.deleted_at.is_(None),
        )
    ).one_or_none()
    if row is None:
        raise VaultNotFound(vault_id)
    return VaultSnapshot(
        vault_id=vault_id,
        policy_epoch=int(row.policy_epoch),
        source_generation=int(row.source_generation),
    )


def is_vault_snapshot_current(session: Session, snapshot: VaultSnapshot) -> bool:
    """Return false after consent changes, Source changes, or vault tombstoning."""

    return (
        session.scalar(
            select(Vault.id).where(
                Vault.id == snapshot.vault_id,
                Vault.deleted_at.is_(None),
                Vault.policy_epoch == snapshot.policy_epoch,
                Vault.source_generation == snapshot.source_generation,
            )
        )
        is not None
    )


def require_current_snapshot(session: Session, snapshot: VaultSnapshot) -> None:
    """Raise a safe error when a worker must discard work produced under stale policy."""

    if not is_vault_snapshot_current(session, snapshot):
        raise StaleVaultSnapshot(
            vault_id=snapshot.vault_id,
            expected_policy_epoch=snapshot.policy_epoch,
            expected_source_generation=snapshot.source_generation,
        )


# Short names form the public job-fence API; explicit names remain available for clarity.
capture_snapshot = capture_vault_snapshot
check_snapshot = is_vault_snapshot_current
