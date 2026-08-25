"""Vault-scoped, content-free receipts for governed model execution.

These tables deliberately persist only technical routing, authorization, and
lifecycle facts. Prompt text, Source plaintext, raw model output, and provider
error bodies do not belong in this persistence boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum, StrEnum
from typing import cast

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    event,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column, validates

from life_coach.ai.contracts import ModelInputKind, RetentionPolicy, SensitivityLevel
from life_coach.jobs.payloads import (
    InvalidRequestHashError,
    VaultRequestFingerprint,
    validate_request_hash,
    validate_vault_request_fingerprint,
)
from life_coach.shared.database import Base, UUIDPrimaryKeyMixin, VaultScopedMixin, utc_now


class ModelRunState(StrEnum):
    """Durable lifecycle of one authorized model invocation."""

    AUTHORIZED = "authorized"
    DISPATCHING = "dispatching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    DENIED = "denied"
    CANCELED = "canceled"


def _enum_column(enum_type: type[Enum], *, name: str, length: int) -> SAEnum:
    """Persist public enum values portably instead of Python member names."""

    return SAEnum(
        enum_type,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [str(member.value) for member in members],
        length=length,
    )


def _postgres_pattern(column: str, pattern: str, *, name: str) -> CheckConstraint:
    """Add a PostgreSQL boundary check while retaining SQLite test portability."""

    return CheckConstraint(f"{column} ~ '{pattern}'", name=name).ddl_if(dialect="postgresql")


_OPAQUE_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$"
_ROUTING_NAME_PATTERN = r"^[a-z][a-z0-9_.:-]{0,99}$"
_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_IDEMPOTENCY_KEY_PATTERN = (
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
    r"[a-z][a-z0-9_.-]{0,31}:([0-9]{1,20}|[0-9a-f]{16,64}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))$"
)


class ModelRun(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    """An authorization and outcome receipt without model input or output content."""

    __tablename__ = "model_run"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_model_run_vault_id_id"),
        UniqueConstraint(
            "vault_id",
            "task_type",
            "idempotency_key",
            name="uq_model_run_vault_task_idempotency",
        ),
        ForeignKeyConstraint(["vault_id"], ["vault.id"], name="fk_model_run_vault"),
        CheckConstraint("policy_epoch >= 0", name="model_run_policy_epoch_nonnegative"),
        CheckConstraint("source_generation >= 0", name="model_run_source_generation_nonnegative"),
        CheckConstraint("attempt >= 0", name="model_run_attempt_nonnegative"),
        CheckConstraint(
            "dispatch_generation >= 0", name="model_run_dispatch_generation_nonnegative"
        ),
        CheckConstraint(
            "length(request_hash) = 79 AND request_hash LIKE 'hmac-sha256:v1:%'",
            name="model_run_request_hash_vault_hmac",
        ),
        CheckConstraint(
            "length(task_definition_hash) = 79 AND task_definition_hash LIKE 'hmac-sha256:v1:%'",
            name="model_run_task_definition_hash_vault_hmac",
        ),
        CheckConstraint(
            "length(consent_snapshot_id) = 72 AND consent_snapshot_id LIKE 'consent:%'",
            name="model_run_consent_snapshot_format",
        ),
        CheckConstraint(
            "dispatch_expires_at IS NULL OR "
            "(dispatch_started_at IS NOT NULL AND dispatch_started_at < dispatch_expires_at)",
            name="model_run_dispatch_interval_nonempty",
        ),
        CheckConstraint(
            "dispatch_started_at IS NULL OR authorized_at <= dispatch_started_at",
            name="model_run_dispatch_after_authorization",
        ),
        CheckConstraint(
            "io_finished_at IS NULL OR "
            "(dispatch_started_at IS NOT NULL AND dispatch_started_at <= io_finished_at)",
            name="model_run_io_after_dispatch",
        ),
        CheckConstraint(
            "completed_at IS NULL OR authorized_at <= completed_at",
            name="model_run_completion_after_authorization",
        ),
        CheckConstraint(
            "completed_at IS NULL OR io_finished_at IS NULL OR io_finished_at <= completed_at",
            name="model_run_completion_after_io",
        ),
        CheckConstraint(
            "completed_at IS NULL OR io_finished_at IS NULL OR io_finished_at <= completed_at",
            name="model_run_completion_after_io",
        ),
        CheckConstraint(
            "state <> 'authorized' OR "
            "(attempt = 0 AND dispatch_generation = 0 "
            "AND dispatch_started_at IS NULL AND dispatch_expires_at IS NULL "
            "AND io_finished_at IS NULL AND completed_at IS NULL)",
            name="model_run_authorized_shape",
        ),
        CheckConstraint(
            "state <> 'dispatching' OR "
            "(attempt > 0 AND dispatch_generation > 0 "
            "AND dispatch_started_at IS NOT NULL AND dispatch_expires_at IS NOT NULL "
            "AND io_finished_at IS NULL AND completed_at IS NULL)",
            name="model_run_dispatching_shape",
        ),
        CheckConstraint(
            "state NOT IN ('succeeded', 'failed', 'unknown') OR "
            "(attempt > 0 AND dispatch_generation > 0 "
            "AND dispatch_started_at IS NOT NULL AND dispatch_expires_at IS NOT NULL "
            "AND io_finished_at IS NOT NULL AND completed_at IS NOT NULL)",
            name="model_run_io_terminal_shape",
        ),
        CheckConstraint(
            "state NOT IN ('denied', 'canceled') OR "
            "(attempt = 0 AND dispatch_generation = 0 "
            "AND dispatch_started_at IS NULL AND dispatch_expires_at IS NULL "
            "AND io_finished_at IS NULL AND completed_at IS NOT NULL)",
            name="model_run_pre_dispatch_terminal_shape",
        ),
        CheckConstraint(
            "state IN ('succeeded', 'failed', 'unknown', 'denied', 'canceled') "
            "OR completed_at IS NULL",
            name="model_run_nonterminal_without_completion",
        ),
        CheckConstraint(
            "state != 'succeeded' OR safe_error_code IS NULL",
            name="model_run_success_without_error",
        ),
        CheckConstraint(
            "state NOT IN ('failed', 'unknown', 'denied', 'canceled') "
            "OR safe_error_code IS NOT NULL",
            name="model_run_failure_has_error_code",
        ),
        CheckConstraint(
            "state IN ('succeeded', 'failed', 'unknown', 'denied', 'canceled') "
            "OR (safe_error_code IS NULL AND provider_request_id IS NULL)",
            name="model_run_nonterminal_without_outcome",
        ),
        CheckConstraint(
            "provider_request_id IS NULL OR length(provider_request_id) BETWEEN 1 AND 255",
            name="model_run_provider_request_id_length",
        ),
        CheckConstraint(
            "safe_error_code IS NULL OR length(safe_error_code) BETWEEN 1 AND 100",
            name="model_run_safe_error_code_length",
        ),
        _postgres_pattern("task_type", _ROUTING_NAME_PATTERN, name="model_run_task_type_technical"),
        _postgres_pattern(
            "idempotency_key",
            _IDEMPOTENCY_KEY_PATTERN,
            name="model_run_idempotency_key_technical",
        ),
        _postgres_pattern(
            "provider", _OPAQUE_IDENTIFIER_PATTERN, name="model_run_provider_technical"
        ),
        _postgres_pattern("model", _OPAQUE_IDENTIFIER_PATTERN, name="model_run_model_technical"),
        _postgres_pattern(
            "data_residency",
            _OPAQUE_IDENTIFIER_PATTERN,
            name="model_run_data_residency_technical",
        ),
        _postgres_pattern(
            "model_revision", _VERSION_PATTERN, name="model_run_model_revision_technical"
        ),
        _postgres_pattern(
            "prompt_template_version",
            _VERSION_PATTERN,
            name="model_run_prompt_template_version_technical",
        ),
        _postgres_pattern(
            "schema_version", _VERSION_PATTERN, name="model_run_schema_version_technical"
        ),
        _postgres_pattern(
            "pipeline_version", _VERSION_PATTERN, name="model_run_pipeline_version_technical"
        ),
        _postgres_pattern(
            "request_hash",
            r"^hmac-sha256:v1:[0-9a-f]{64}$",
            name="model_run_request_hash_hex",
        ),
        _postgres_pattern(
            "task_definition_hash",
            r"^hmac-sha256:v1:[0-9a-f]{64}$",
            name="model_run_task_definition_hash_hex",
        ),
        _postgres_pattern(
            "consent_snapshot_id",
            r"^consent:[0-9a-f]{64}$",
            name="model_run_consent_snapshot_hex",
        ),
        _postgres_pattern(
            "provider_request_id",
            r"^$|^[A-Za-z0-9][A-Za-z0-9._~:/+=@-]{0,254}$",
            name="model_run_provider_request_id_technical",
        ),
        _postgres_pattern(
            "safe_error_code",
            r"^$|^[a-z][a-z0-9_.:-]{0,99}$",
            name="model_run_safe_error_code_technical",
        ),
        Index(
            "ix_model_run_vault_state_expiry",
            "vault_id",
            "state",
            "dispatch_expires_at",
        ),
    )

    task_type: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(79), nullable=False)
    task_definition_hash: Mapped[str] = mapped_column(String(79), nullable=False)

    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(64), nullable=False)

    consent_snapshot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_sensitivity: Mapped[SensitivityLevel] = mapped_column(
        _enum_column(SensitivityLevel, name="model_run_sensitivity", length=32), nullable=False
    )
    data_residency: Mapped[str] = mapped_column(String(128), nullable=False)
    retention_policy: Mapped[RetentionPolicy] = mapped_column(
        _enum_column(RetentionPolicy, name="model_run_retention_policy", length=32),
        nullable=False,
    )

    state: Mapped[ModelRunState] = mapped_column(
        _enum_column(ModelRunState, name="model_run_state", length=16),
        nullable=False,
        default=ModelRunState.AUTHORIZED,
        server_default=ModelRunState.AUTHORIZED.value,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    dispatch_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    provider_request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    safe_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)

    authorized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    dispatch_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dispatch_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    io_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @validates("request_hash", "task_definition_hash")
    def _validate_vault_hmac(self, _key: str, value: str) -> str:
        if not isinstance(value, VaultRequestFingerprint):
            raise InvalidRequestHashError("model run fingerprint must be minted by the vault HMAC")
        validate_request_hash(value)
        return value


class ModelRunInput(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    """Append-only technical reference to an authorized model input object."""

    __tablename__ = "model_run_input"
    __table_args__ = (
        ForeignKeyConstraint(
            ["vault_id", "model_run_id"],
            ["model_run.vault_id", "model_run.id"],
            name="fk_model_run_input_vault_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "vault_id",
            "model_run_id",
            "kind",
            "object_id",
            name="uq_model_run_input_object",
        ),
        UniqueConstraint(
            "vault_id",
            "model_run_id",
            "ordinal",
            name="uq_model_run_input_ordinal",
        ),
        CheckConstraint("ordinal >= 0", name="model_run_input_ordinal_nonnegative"),
        CheckConstraint(
            "length(content_fingerprint) = 79 AND content_fingerprint LIKE 'hmac-sha256:v1:%'",
            name="model_run_input_content_fingerprint_vault_hmac",
        ),
        _postgres_pattern(
            "content_fingerprint",
            r"^hmac-sha256:v1:[0-9a-f]{64}$",
            name="model_run_input_content_fingerprint_hex",
        ),
    )

    model_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[ModelInputKind] = mapped_column(
        _enum_column(ModelInputKind, name="model_run_input_kind", length=32), nullable=False
    )
    object_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(79), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    @validates("content_fingerprint")
    def _validate_content_fingerprint(self, _key: str, value: str) -> str:
        if not isinstance(value, VaultRequestFingerprint):
            raise InvalidRequestHashError("input fingerprint must be minted by the vault HMAC")
        validate_request_hash(value)
        return value


class ModelRunArtifact(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    """Append-only pointer from one successful run to its durable artifact.

    The row contains identifiers only. Candidate text remains owned by Knowledge,
    while this table gives idempotent model-run replay a content-free projection.
    """

    __tablename__ = "model_run_artifact"
    __table_args__ = (
        UniqueConstraint(
            "vault_id",
            "id",
            name="uq_model_run_artifact_vault_id_id",
        ),
        UniqueConstraint(
            "vault_id",
            "model_run_id",
            name="uq_model_run_artifact_vault_run",
        ),
        UniqueConstraint(
            "vault_id",
            "derived_object_id",
            name="uq_model_run_artifact_vault_derived",
        ),
        UniqueConstraint(
            "vault_id",
            "action_id",
            name="uq_model_run_artifact_vault_action",
        ),
        ForeignKeyConstraint(
            ["vault_id", "model_run_id"],
            ["model_run.vault_id", "model_run.id"],
            name="fk_model_run_artifact_vault_run",
        ),
        ForeignKeyConstraint(
            ["vault_id", "derived_object_id"],
            ["derived_object.vault_id", "derived_object.id"],
            name="fk_model_run_artifact_vault_derived",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["vault_id", "memory_claim_id"],
            ["memory_claim.vault_id", "memory_claim.id"],
            name="fk_model_run_artifact_vault_memory_claim",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["vault_id", "action_id"],
            ["reversible_action.vault_id", "reversible_action.id"],
            name="fk_model_run_artifact_vault_action",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "(artifact_kind = 'knowledge' AND derived_object_id IS NOT NULL "
            "AND memory_claim_id IS NOT NULL AND action_id IS NULL) OR "
            "(artifact_kind = 'action' AND action_id IS NOT NULL "
            "AND derived_object_id IS NULL AND memory_claim_id IS NULL)",
            name="model_run_artifact_kind_shape",
        ),
    )

    model_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="knowledge",
        server_default="knowledge",
    )
    derived_object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    memory_claim_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


@event.listens_for(ModelRun, "before_insert")
def _validate_model_run_fingerprint_vault(
    _mapper: Mapper[ModelRun],
    _connection: Connection,
    target: ModelRun,
) -> None:
    vault_id = cast(uuid.UUID, target.vault_id)
    validate_vault_request_fingerprint(target.request_hash, vault_id=vault_id)
    validate_vault_request_fingerprint(target.task_definition_hash, vault_id=vault_id)


@event.listens_for(ModelRunInput, "before_insert")
def _validate_model_run_input_fingerprint_vault(
    _mapper: Mapper[ModelRunInput],
    _connection: Connection,
    target: ModelRunInput,
) -> None:
    validate_vault_request_fingerprint(
        target.content_fingerprint,
        vault_id=cast(uuid.UUID, target.vault_id),
    )


__all__ = ["ModelRun", "ModelRunArtifact", "ModelRunInput", "ModelRunState"]
