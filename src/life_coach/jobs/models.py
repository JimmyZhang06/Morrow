"""SQLAlchemy 2 persistence models for PostgreSQL-backed jobs and side effects."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Connection,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    event,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Mapper, mapped_column, validates

from life_coach.jobs.enums import JobQueue, JobState, OutboundOperationState
from life_coach.jobs.payloads import (
    InvalidRequestHashError,
    SafePayload,
    UnsafePayloadError,
    VaultRequestFingerprint,
    validate_provider_identifier,
    validate_request_hash,
    validate_resource_version_identifier,
    validate_routing_name,
    validate_safe_payload,
    validate_subscriber_job_types,
    validate_technical_identifier,
    validate_vault_request_fingerprint,
)
from life_coach.shared.database import Base, UUIDPrimaryKeyMixin, VaultScopedMixin, utc_now

SAFE_JSON = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
_TECHNICAL_IDENTIFIER_SQL_PATTERN = (
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
    r"[a-z][a-z0-9_.-]{0,31}:([0-9]{1,20}|[0-9a-f]{16,64}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))$"
)


def _technical_identifier_constraint(
    column: str,
    name: str,
    *,
    nullable: bool = False,
) -> CheckConstraint:
    """Emit the same opaque-token contract at the PostgreSQL persistence boundary."""

    match = f"{column} ~ '{_TECHNICAL_IDENTIFIER_SQL_PATTERN}'"
    expression = f"({column} IS NULL OR {match})" if nullable else match
    return CheckConstraint(
        expression,
        name=name,
    ).ddl_if(dialect="postgresql")


def _request_fingerprint_constraint(name: str) -> CheckConstraint:
    return CheckConstraint(
        r"request_hash ~ '^hmac-sha256:v1:[0-9a-f]{64}$'",
        name=name,
    ).ddl_if(dialect="postgresql")


def _string_enum(
    enum_type: type[JobState] | type[JobQueue] | type[OutboundOperationState],
) -> SAEnum:
    return SAEnum(
        enum_type,
        native_enum=False,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
        create_constraint=True,
        name=f"{enum_type.__name__.lower()}_values",
    )


class OutboxEvent(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    """A body-free domain event awaiting transactional subscriber materialization."""

    __tablename__ = "outbox_event"
    __table_args__ = (
        UniqueConstraint("id", "vault_id", name="uq_outbox_event_id_vault"),
        UniqueConstraint(
            "vault_id",
            "event_type",
            "idempotency_key",
            name="uq_outbox_event_vault_type_idempotency",
        ),
        _technical_identifier_constraint(
            "idempotency_key", "outbox_event_idempotency_key_technical"
        ),
        CheckConstraint(
            "length(request_hash) = 79 AND request_hash LIKE 'hmac-sha256:v1:%'",
            name="outbox_event_request_hash_vault_hmac",
        ),
        _request_fingerprint_constraint("outbox_event_request_hash_hex"),
    )

    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    resource_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    pipeline_version: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(79), nullable=False)
    payload: Mapped[SafePayload] = mapped_column(SAFE_JSON, nullable=False, default=dict)
    expected_job_types: Mapped[list[str]] = mapped_column(SAFE_JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("payload")
    def _validate_payload(self, _key: str, value: Mapping[str, object]) -> SafePayload:
        return validate_safe_payload(value)

    @validates("request_hash")
    def _validate_request_hash(self, _key: str, value: str) -> str:
        if not isinstance(value, VaultRequestFingerprint):
            raise InvalidRequestHashError("request fingerprint must be minted by the vault HMAC")
        validate_request_hash(value)
        return value

    @validates("event_type")
    def _validate_event_type(self, _key: str, value: str) -> str:
        return validate_routing_name(value)

    @validates("idempotency_key")
    def _validate_idempotency_key(self, _key: str, value: str) -> str:
        return validate_technical_identifier(value)

    @validates("expected_job_types")
    def _validate_expected_job_types(self, _key: str, value: list[str]) -> list[str]:
        return list(validate_subscriber_job_types(value))


class Job(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    """At-least-once internal work item with lease-generation fencing."""

    __tablename__ = "job"
    __table_args__ = (
        ForeignKeyConstraint(
            ("outbox_event_id", "vault_id"),
            ("outbox_event.id", "outbox_event.vault_id"),
            name="fk_job_outbox_event_vault",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "vault_id",
            "job_type",
            "idempotency_key",
            name="uq_job_vault_type_idempotency",
        ),
        UniqueConstraint(
            "vault_id",
            "job_type",
            "resource_revision_id",
            "pipeline_version",
            name="uq_job_vault_type_revision_pipeline",
        ),
        Index(
            "uq_job_outbox_event_type",
            "outbox_event_id",
            "job_type",
            unique=True,
            postgresql_where=text("outbox_event_id IS NOT NULL"),
            sqlite_where=text("outbox_event_id IS NOT NULL"),
        ),
        Index(
            "ix_job_claimable",
            "queue",
            "state",
            "run_after",
            "priority",
        ),
        CheckConstraint("attempts >= 0", name="job_attempts_nonnegative"),
        CheckConstraint("max_attempts >= 1", name="job_max_attempts_positive"),
        CheckConstraint("lease_generation >= 0", name="job_lease_generation_nonnegative"),
        CheckConstraint("policy_epoch >= 0", name="job_policy_epoch_nonnegative"),
        CheckConstraint("source_generation >= 0", name="job_source_generation_nonnegative"),
        _technical_identifier_constraint("idempotency_key", "job_idempotency_key_technical"),
        _technical_identifier_constraint("lease_owner", "job_lease_owner_technical", nullable=True),
        CheckConstraint(
            "length(request_hash) = 79 AND request_hash LIKE 'hmac-sha256:v1:%'",
            name="job_request_hash_vault_hmac",
        ),
        _request_fingerprint_constraint("job_request_hash_hex"),
        CheckConstraint(
            "state != 'running' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="job_running_has_lease",
        ),
        CheckConstraint(
            "state NOT IN ('done', 'dead', 'canceled') OR completed_at IS NOT NULL",
            name="job_terminal_has_completion_time",
        ),
    )

    job_type: Mapped[str] = mapped_column(String(100), nullable=False)
    queue: Mapped[JobQueue] = mapped_column(
        _string_enum(JobQueue), nullable=False, default=JobQueue.INGEST_TEXT
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    resource_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    pipeline_version: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(79), nullable=False)
    outbox_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    payload: Mapped[SafePayload] = mapped_column(SAFE_JSON, nullable=False, default=dict)

    state: Mapped[JobState] = mapped_column(
        _string_enum(JobState), nullable=False, default=JobState.QUEUED
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )

    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    last_error_class: Mapped[str | None] = mapped_column(String(100))
    safe_error_message: Mapped[str | None] = mapped_column(String(500))
    consent_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    policy_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("payload")
    def _validate_payload(self, _key: str, value: Mapping[str, object]) -> SafePayload:
        return validate_safe_payload(value)

    @validates("request_hash")
    def _validate_request_hash(self, _key: str, value: str) -> str:
        if not isinstance(value, VaultRequestFingerprint):
            raise InvalidRequestHashError("request fingerprint must be minted by the vault HMAC")
        validate_request_hash(value)
        return value

    @validates("job_type")
    def _validate_job_type(self, _key: str, value: str) -> str:
        return validate_routing_name(value)

    @validates("idempotency_key")
    def _validate_idempotency_key(self, _key: str, value: str) -> str:
        return validate_technical_identifier(value)

    @validates("lease_owner")
    def _validate_lease_owner(self, _key: str, value: str | None) -> str | None:
        return None if value is None else validate_technical_identifier(value)


class OutboundOperation(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    """Idempotency and reconciliation ledger for confirmed external side effects."""

    __tablename__ = "outbound_operation"
    __table_args__ = (
        UniqueConstraint(
            "vault_id",
            "connector",
            "operation",
            "local_resource_version",
            name="uq_outbound_operation_scope",
        ),
        UniqueConstraint(
            "vault_id",
            "connector",
            "provider_idempotency_key",
            name="uq_outbound_provider_idempotency",
        ),
        UniqueConstraint(
            "vault_id",
            "authorization_id",
            "authorization_generation",
            name="uq_outbound_exact_authorization",
        ),
        _technical_identifier_constraint(
            "local_resource_version", "outbound_local_resource_version_technical"
        ),
        CheckConstraint(
            "local_resource_version ~ "
            "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'",
            name="outbound_local_resource_version_uuid",
        ).ddl_if(dialect="postgresql"),
        _technical_identifier_constraint(
            "provider_idempotency_key", "outbound_provider_idempotency_key_technical"
        ),
        CheckConstraint(
            "external_id IS NULL OR external_id ~ '^[A-Za-z0-9][A-Za-z0-9._~:/+=@-]{0,254}$'",
            name="outbound_external_id_provider_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "authorization_generation >= 0",
            name="outbound_authorization_generation_nonnegative",
        ),
        CheckConstraint("policy_epoch >= 0", name="outbound_policy_epoch_nonnegative"),
        CheckConstraint("source_generation >= 0", name="outbound_source_generation_nonnegative"),
        CheckConstraint(
            "initial_tombstoned = false", name="outbound_initial_source_not_tombstoned"
        ),
        CheckConstraint(
            "length(request_hash) = 79 AND request_hash LIKE 'hmac-sha256:v1:%'",
            name="outbound_request_hash_vault_hmac",
        ),
        _request_fingerprint_constraint("outbound_request_hash_hex"),
        CheckConstraint("attempts >= 0", name="outbound_attempts_nonnegative"),
        CheckConstraint("max_attempts >= 1", name="outbound_max_attempts_positive"),
        CheckConstraint(
            "max_reconciliation_attempts >= 1",
            name="outbound_max_reconciliation_attempts_positive",
        ),
        CheckConstraint(
            "execution_generation >= 0", name="outbound_execution_generation_nonnegative"
        ),
        CheckConstraint(
            "reconciliation_generation >= 0",
            name="outbound_reconciliation_generation_nonnegative",
        ),
        CheckConstraint(
            "state != 'executing' OR execution_expires_at IS NOT NULL",
            name="outbound_executing_has_expiry",
        ),
        CheckConstraint(
            "state != 'reconciling' OR reconciliation_expires_at IS NOT NULL",
            name="outbound_reconciling_has_expiry",
        ),
        CheckConstraint(
            "state != 'succeeded' OR external_id IS NOT NULL",
            name="outbound_success_has_external_id",
        ),
    )

    connector: Mapped[str] = mapped_column(String(100), nullable=False)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    local_resource_version: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(79), nullable=False)
    provider_idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    authorization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    authorization_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_tombstoned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    external_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[OutboundOperationState] = mapped_column(
        _string_enum(OutboundOperationState),
        nullable=False,
        default=OutboundOperationState.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_reconciliation_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    execution_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    execution_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciliation_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reconciliation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciliation_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_class: Mapped[str | None] = mapped_column(String(100))
    safe_error_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
        onupdate=utc_now,
    )
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("request_hash")
    def _validate_request_hash(self, _key: str, value: str) -> str:
        if not isinstance(value, VaultRequestFingerprint):
            raise InvalidRequestHashError("request fingerprint must be minted by the vault HMAC")
        validate_request_hash(value)
        return value

    @validates("connector", "operation")
    def _validate_routing_name(self, _key: str, value: str) -> str:
        return validate_routing_name(value)

    @validates("local_resource_version")
    def _validate_resource_version(self, _key: str, value: str) -> str:
        return validate_resource_version_identifier(value)

    @validates("provider_idempotency_key")
    def _validate_technical_identifier(self, _key: str, value: str) -> str:
        return validate_technical_identifier(value)

    @validates("external_id")
    def _validate_external_id(self, _key: str, value: str | None) -> str | None:
        return None if value is None else validate_provider_identifier(value)


def _canonical_row_payload(
    vault_id: uuid.UUID, resource_id: uuid.UUID, pipeline_version: str
) -> SafePayload:
    return validate_safe_payload(
        {
            "vault_id": str(vault_id),
            "resource_id": str(resource_id),
            "pipeline_version": pipeline_version,
        }
    )


@event.listens_for(Job, "before_insert")
def _canonicalize_job_payload(_mapper: Mapper[Job], _connection: Connection, target: Job) -> None:
    validate_vault_request_fingerprint(
        target.request_hash,
        vault_id=cast(uuid.UUID, target.vault_id),
    )
    canonical = _canonical_row_payload(
        cast(uuid.UUID, target.vault_id), target.resource_id, target.pipeline_version
    )
    if target.payload and target.payload != canonical:
        raise UnsafePayloadError("payload routing metadata does not match typed columns")
    target.payload = canonical


@event.listens_for(OutboxEvent, "before_insert")
def _canonicalize_outbox_payload(
    _mapper: Mapper[OutboxEvent], _connection: Connection, target: OutboxEvent
) -> None:
    validate_vault_request_fingerprint(
        target.request_hash,
        vault_id=cast(uuid.UUID, target.vault_id),
    )
    canonical = _canonical_row_payload(
        cast(uuid.UUID, target.vault_id), target.resource_id, target.pipeline_version
    )
    if target.payload and target.payload != canonical:
        raise UnsafePayloadError("payload routing metadata does not match typed columns")
    target.payload = canonical


@event.listens_for(OutboundOperation, "before_insert")
def _validate_outbound_fingerprint(
    _mapper: Mapper[OutboundOperation],
    _connection: Connection,
    target: OutboundOperation,
) -> None:
    validate_vault_request_fingerprint(
        target.request_hash,
        vault_id=cast(uuid.UUID, target.vault_id),
    )
