"""Content-free contracts for the durable governed-model receipt boundary."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from life_coach.modules.model_runs.models import ModelRunState

from life_coach.ai.contracts import ModelInputKind, RetentionPolicy, SensitivityLevel
from life_coach.jobs.payloads import (
    VaultRequestFingerprint,
    validate_routing_name,
    validate_technical_identifier,
    validate_vault_request_fingerprint,
)

_TECHNICAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")
_VERSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_CONSENT_SNAPSHOT_ID = re.compile(r"^consent:[0-9a-f]{64}\Z")


class ModelRunIdempotencyConflict(RuntimeError):
    """A scoped idempotency key was reused with a different immutable binding."""


class CrossVaultModelRunError(PermissionError):
    """A vault-scoped repository received a contract from another vault."""


class ModelRunArtifactConflict(RuntimeError):
    """A run or Knowledge artifact already has a different lineage binding."""


def _technical(value: str, *, field: str) -> str:
    if _TECHNICAL_ID.fullmatch(value) is None:
        raise ValueError(f"{field} must use the technical identifier format")
    return value


def _version(value: str, *, field: str) -> str:
    if _VERSION_ID.fullmatch(value) is None:
        raise ValueError(f"{field} must use the version identifier format")
    return value


@dataclass(frozen=True, slots=True)
class ModelRunInputSpec:
    """One technical input reference; plaintext is deliberately unrepresentable."""

    vault_id: uuid.UUID
    kind: ModelInputKind
    object_id: uuid.UUID
    content_fingerprint: VaultRequestFingerprint
    ordinal: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ModelInputKind):
            raise TypeError("model run input kind must be a ModelInputKind")
        validate_vault_request_fingerprint(
            self.content_fingerprint,
            vault_id=self.vault_id,
        )
        if self.ordinal < 0:
            raise ValueError("model run input ordinal cannot be negative")


@dataclass(frozen=True, slots=True)
class ModelRunReceiptSpec:
    """Immutable authority and routing facts used to prepare one ModelRun receipt."""

    vault_id: uuid.UUID
    task_type: str
    idempotency_key: str
    request_hash: VaultRequestFingerprint
    task_definition_hash: VaultRequestFingerprint
    provider: str
    model: str
    model_revision: str
    prompt_template_version: str
    schema_version: str
    pipeline_version: str
    consent_snapshot_id: str
    policy_epoch: int
    source_generation: int
    actual_sensitivity: SensitivityLevel
    data_residency: str
    retention_policy: RetentionPolicy

    def __post_init__(self) -> None:
        validate_routing_name(self.task_type)
        validate_technical_identifier(self.idempotency_key)
        validate_vault_request_fingerprint(self.request_hash, vault_id=self.vault_id)
        validate_vault_request_fingerprint(self.task_definition_hash, vault_id=self.vault_id)
        _technical(self.provider, field="provider")
        _technical(self.model, field="model")
        _version(self.model_revision, field="model_revision")
        _version(self.prompt_template_version, field="prompt_template_version")
        _version(self.schema_version, field="schema_version")
        _version(self.pipeline_version, field="pipeline_version")
        if _CONSENT_SNAPSHOT_ID.fullmatch(self.consent_snapshot_id) is None:
            raise ValueError("consent_snapshot_id must use the authoritative snapshot format")
        _technical(self.data_residency, field="data_residency")
        if self.policy_epoch < 0 or self.source_generation < 0:
            raise ValueError("model run fence generations cannot be negative")
        if not isinstance(self.actual_sensitivity, SensitivityLevel):
            raise TypeError("actual_sensitivity must be a SensitivityLevel")
        if not isinstance(self.retention_policy, RetentionPolicy):
            raise TypeError("retention_policy must be a RetentionPolicy")


@dataclass(frozen=True, slots=True)
class ModelRunWrite:
    run_id: uuid.UUID
    created: bool


@dataclass(frozen=True, slots=True)
class ModelRunDispatchTicket:
    """The exact receipt generation allowed to cross the provider I/O boundary."""

    run_id: uuid.UUID
    vault_id: uuid.UUID
    dispatch_generation: int

    def __post_init__(self) -> None:
        if self.dispatch_generation < 1:
            raise ValueError("dispatch generation must be positive")


@dataclass(frozen=True, slots=True)
class ModelRunArtifactSpec:
    """Trusted identifiers for one durable Knowledge candidate."""

    vault_id: uuid.UUID
    derived_object_id: uuid.UUID
    memory_claim_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ModelRunArtifactRef:
    """Content-free artifact identity returned by replay projections."""

    artifact_id: uuid.UUID
    vault_id: uuid.UUID
    model_run_id: uuid.UUID
    derived_object_id: uuid.UUID
    memory_claim_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ModelRunArtifactWrite:
    artifact: ModelRunArtifactRef
    created: bool


@dataclass(frozen=True, slots=True)
class ModelRunProjection:
    """Minimal durable state needed to resolve an idempotent replay."""

    run_id: uuid.UUID
    vault_id: uuid.UUID
    state: ModelRunState
    attempt: int
    dispatch_generation: int
    provider_request_id: str | None
    safe_error_code: str | None
    artifact: ModelRunArtifactRef | None

    def __post_init__(self) -> None:
        if self.attempt < 0 or self.dispatch_generation < 0:
            raise ValueError("model run projection generations cannot be negative")


__all__ = [
    "CrossVaultModelRunError",
    "ModelRunArtifactConflict",
    "ModelRunArtifactRef",
    "ModelRunArtifactSpec",
    "ModelRunArtifactWrite",
    "ModelRunDispatchTicket",
    "ModelRunIdempotencyConflict",
    "ModelRunInputSpec",
    "ModelRunProjection",
    "ModelRunReceiptSpec",
    "ModelRunWrite",
]
