"""Append-only purpose-consent event model."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    UniqueConstraint,
    event,
)
from sqlalchemy import (
    Enum as SqlEnum,
)
from sqlalchemy.orm import Mapped, Mapper, ORMExecuteState, Session, mapped_column

from life_coach.modules.consent.exceptions import (
    ConsentRecordImmutable,
    InvalidConsentActor,
)
from life_coach.modules.consent.provider_policy import ProviderPolicy, ProviderPolicyType
from life_coach.modules.identity.models import (
    CreatedBy,
    DataClass,
    created_by_type,
    data_class_type,
)
from life_coach.shared.database import (
    Base,
    UUIDPrimaryKeyMixin,
    VaultScopedMixin,
    utc_now,
)


class ConsentAction(StrEnum):
    """The meaning of an immutable consent event."""

    GRANT = "grant"
    REVOKE = "revoke"


class ConsentScope(StrEnum):
    """Whether an event applies vault-wide or to one Source document."""

    VAULT = "vault"
    SOURCE_DOCUMENT = "source_document"


class ConsentPurpose(StrEnum):
    """The bounded processing purposes a user may authorize independently."""

    SEARCH = "search"
    PASSIVE_QA = "passive_qa"
    CROSS_RECORD_ANALYSIS = "cross_record_analysis"
    PROACTIVE_RESURFACING = "proactive_resurfacing"
    NARRATIVE = "narrative"
    THIRD_PARTY_INTEGRATION = "third_party_integration"
    LONG_TERM_INFERENCE = "long_term_inference"


def _enum_type(enum_type: type[StrEnum], name: str, length: int) -> SqlEnum:
    return SqlEnum(
        enum_type,
        name=name,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda enum: [member.value for member in enum],
        length=length,
    )


class ConsentRecord(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    """An immutable grant/revoke event, never the mutable current state."""

    __tablename__ = "consent_record"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_consent_record_vault_id_id"),
        UniqueConstraint(
            "vault_id", "policy_epoch", name="uq_consent_record_vault_id_policy_epoch"
        ),
        UniqueConstraint(
            "vault_id", "interaction_id", name="uq_consent_record_vault_id_interaction_id"
        ),
        ForeignKeyConstraint(
            ["vault_id"],
            ["vault.id"],
            name="fk_consent_record_vault_id_vault",
        ),
        ForeignKeyConstraint(
            ["vault_id", "source_document_id"],
            ["source_document.vault_id", "source_document.id"],
            name="fk_consent_record_vault_source_document",
        ),
        CheckConstraint("length(trim(purpose)) > 0", name="purpose_nonempty"),
        CheckConstraint("policy_epoch > 0", name="policy_epoch_positive"),
        CheckConstraint("created_by = 'user'", name="consent_actor_is_user"),
        CheckConstraint("expires_at > issued_at", name="interaction_time_window_valid"),
        CheckConstraint(
            "(scope = 'vault' AND source_document_id IS NULL) OR "
            "(scope = 'source_document' AND source_document_id IS NOT NULL)",
            name="scope_matches_source_document",
        ),
        Index(
            "ix_consent_record_vault_purpose_source_epoch",
            "vault_id",
            "purpose",
            "source_document_id",
            "policy_epoch",
        ),
    )

    purpose: Mapped[ConsentPurpose] = mapped_column(
        _enum_type(ConsentPurpose, "consent_purpose", 32), nullable=False
    )
    action: Mapped[ConsentAction] = mapped_column(
        _enum_type(ConsentAction, "consent_action", 16), nullable=False
    )
    scope: Mapped[ConsentScope] = mapped_column(
        _enum_type(ConsentScope, "consent_scope", 24), nullable=False
    )
    source_document_id: Mapped[UUID | None] = mapped_column(nullable=True)
    principal_id: Mapped[UUID] = mapped_column(nullable=False)
    interaction_id: Mapped[UUID] = mapped_column(nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_policy: Mapped[ProviderPolicy] = mapped_column(
        ProviderPolicyType(), nullable=False, default=ProviderPolicy
    )
    policy_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    created_by: Mapped[CreatedBy] = mapped_column(created_by_type(), nullable=False)
    data_class: Mapped[DataClass] = mapped_column(
        data_class_type(), nullable=False, default=DataClass.SENSITIVE
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def granted(self) -> bool:
        """Compatibility-friendly view of the event action."""

        return self.action is ConsentAction.GRANT


def require_user_consent_actor(value: CreatedBy | str) -> CreatedBy:
    """Fail closed unless the consent event was explicitly authored by the user."""

    try:
        actor = CreatedBy(value)
    except ValueError as exc:
        raise InvalidConsentActor("consent actor must be user") from exc
    if actor is not CreatedBy.USER:
        raise InvalidConsentActor("consent actor must be user")
    return actor


@event.listens_for(ConsentRecord, "before_insert")
def _validate_consent_insert(
    mapper: Mapper[ConsentRecord], connection: Any, target: ConsentRecord
) -> None:
    del mapper, connection
    target.created_by = require_user_consent_actor(target.created_by)
    target.provider_policy = ProviderPolicy.from_value(target.provider_policy)


@event.listens_for(ConsentRecord, "before_update")
def _reject_consent_update(
    mapper: Mapper[ConsentRecord], connection: Any, target: ConsentRecord
) -> None:
    del mapper, connection, target
    raise ConsentRecordImmutable("consent records are append-only")


@event.listens_for(ConsentRecord, "before_delete")
def _reject_consent_delete(
    mapper: Mapper[ConsentRecord], connection: Any, target: ConsentRecord
) -> None:
    del mapper, connection, target
    raise ConsentRecordImmutable("consent records are append-only")


@event.listens_for(Session, "do_orm_execute")
def _reject_consent_bulk_mutation(execute_state: ORMExecuteState) -> None:
    """Reject SQLAlchemy bulk mutation paths that bypass mapper events."""

    target_table = getattr(execute_state.statement, "table", None)
    is_consent_target = (
        execute_state.bind_mapper is ConsentRecord.__mapper__
        or target_table is ConsentRecord.__table__
        or getattr(target_table, "original", None) is ConsentRecord.__table__
    )
    if (execute_state.is_update or execute_state.is_delete) and is_consent_target:
        raise ConsentRecordImmutable("consent records are append-only")
