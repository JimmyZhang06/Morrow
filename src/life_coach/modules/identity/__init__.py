"""Vault identity and policy/source generation primitives."""

from life_coach.modules.identity.exceptions import (
    StaleVaultSnapshot,
    VaultNotFound,
)
from life_coach.modules.identity.models import (
    CreatedBy,
    DataClass,
    MembershipRole,
    Principal,
    Vault,
    VaultMembership,
)
from life_coach.modules.identity.service import (
    VaultSnapshot,
    capture_snapshot,
    capture_vault_snapshot,
    check_snapshot,
    create_vault,
    get_vault,
    increment_policy_epoch,
    increment_source_generation,
    is_vault_snapshot_current,
    require_current_snapshot,
)

__all__ = [
    "CreatedBy",
    "DataClass",
    "MembershipRole",
    "Principal",
    "StaleVaultSnapshot",
    "Vault",
    "VaultMembership",
    "VaultNotFound",
    "VaultSnapshot",
    "capture_snapshot",
    "capture_vault_snapshot",
    "check_snapshot",
    "create_vault",
    "get_vault",
    "increment_policy_epoch",
    "increment_source_generation",
    "is_vault_snapshot_current",
    "require_current_snapshot",
]
