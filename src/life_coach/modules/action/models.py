"""Vault-scoped persistence for the reversible small-action loop."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import Mapped, Session, mapped_column

from life_coach.modules.knowledge.exceptions import AppendOnlyViolationError
from life_coach.shared.database import Base, TimestampMixin, UUIDPrimaryKeyMixin, VaultScopedMixin

from .lifecycle import ReversibleActionState, ReversibleActionVerdict


def _persisted_enum(
    enum_class: type[ReversibleActionState] | type[ReversibleActionVerdict],
    name: str,
) -> SqlEnum:
    return SqlEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class ReversibleAction(UUIDPrimaryKeyMixin, VaultScopedMixin, TimestampMixin, Base):
    """Current projection of a local experiment with no external side effect."""

    __tablename__ = "reversible_action"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_reversible_action_vault_id_id"),
        ForeignKeyConstraint(
            ["vault_id", "memory_claim_id"],
            ["memory_claim.vault_id", "memory_claim.id"],
            name="fk_reversible_action_vault_memory_claim",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["vault_id", "source_derived_object_id"],
            ["derived_object.vault_id", "derived_object.id"],
            name="fk_reversible_action_vault_source_derived",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["vault_id", "model_run_id"],
            ["model_run.vault_id", "model_run.id"],
            name="fk_reversible_action_vault_model_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "vault_id",
            "source_derived_object_id",
            "template_version",
            name="uq_reversible_action_source_template",
        ),
        CheckConstraint("source_version_no > 0", name="reversible_action_source_version_positive"),
        CheckConstraint("revision > 0", name="reversible_action_revision_positive"),
        CheckConstraint(
            "estimated_minutes BETWEEN 1 AND 15",
            name="reversible_action_duration_small",
        ),
        CheckConstraint("is_reversible", name="reversible_action_must_be_reversible"),
        Index("ix_reversible_action_vault_model_run", "vault_id", "model_run_id"),
    )

    memory_claim_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_derived_object_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    model_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    exit_plan: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    is_reversible: Mapped[bool] = mapped_column(nullable=False)
    state: Mapped[ReversibleActionState] = mapped_column(
        _persisted_enum(ReversibleActionState, "reversible_action_state"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)


class ActionVerdict(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    """Immutable user command event; idempotency is enforced at the database boundary."""

    __tablename__ = "action_verdict"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_action_verdict_vault_id_id"),
        ForeignKeyConstraint(
            ["vault_id", "action_id"],
            ["reversible_action.vault_id", "reversible_action.id"],
            name="fk_action_verdict_vault_action",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("vault_id", "action_id", "sequence_no", name="uq_action_verdict_sequence"),
        CheckConstraint("sequence_no > 0", name="action_verdict_sequence_positive"),
        CheckConstraint("resulting_revision > 1", name="action_verdict_revision_valid"),
        Index("ix_action_verdict_stream", "vault_id", "action_id", "sequence_no"),
    )

    action_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[ReversibleActionVerdict] = mapped_column(
        _persisted_enum(ReversibleActionVerdict, "reversible_action_verdict"), nullable=False
    )
    resulting_state: Mapped[ReversibleActionState] = mapped_column(
        _persisted_enum(ReversibleActionState, "action_verdict_resulting_state"), nullable=False
    )
    resulting_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ActionCommandReceipt(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    """Content-free immutable receipt for every mutating HTTP command."""

    __tablename__ = "action_command_receipt"
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_action_command_receipt_vault_id_id"),
        UniqueConstraint(
            "vault_id", "idempotency_key", name="uq_action_command_receipt_idempotency"
        ),
        ForeignKeyConstraint(
            ["vault_id", "action_id"],
            ["reversible_action.vault_id", "reversible_action.id"],
            name="fk_action_command_receipt_vault_action",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["vault_id", "verdict_id"],
            ["action_verdict.vault_id", "action_verdict.id"],
            name="fk_action_command_receipt_vault_verdict",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(command_fingerprint) = 64",
            name="action_command_receipt_fingerprint_sha256",
        ),
        CheckConstraint(
            "(command_kind = 'create' AND verdict_id IS NULL) OR "
            "(command_kind = 'verdict' AND verdict_id IS NOT NULL)",
            name="action_command_receipt_result_shape",
        ),
    )

    idempotency_key: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    command_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    command_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    action_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    verdict_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@event.listens_for(ActionVerdict, "before_update", propagate=True)
@event.listens_for(ActionVerdict, "before_delete", propagate=True)
@event.listens_for(ActionCommandReceipt, "before_update", propagate=True)
@event.listens_for(ActionCommandReceipt, "before_delete", propagate=True)
def _reject_action_verdict_mutation(*_: object) -> None:
    raise AppendOnlyViolationError("ActionVerdict rows are append-only")


@event.listens_for(Session, "do_orm_execute")
def _reject_action_verdict_bulk_mutation(execute_state: Any) -> None:
    if not (execute_state.is_update or execute_state.is_delete):
        return
    mapper = getattr(execute_state, "bind_mapper", None)
    table = getattr(execute_state.statement, "table", None)
    immutable_classes = {ActionVerdict, ActionCommandReceipt}
    immutable_tables = {ActionVerdict.__tablename__, ActionCommandReceipt.__tablename__}
    if (mapper is not None and getattr(mapper, "class_", None) in immutable_classes) or (
        table is not None
        and getattr(table, "name", None) in immutable_tables
        and getattr(table, "schema", None) is None
    ):
        raise AppendOnlyViolationError("ActionVerdict rows are append-only")


__all__ = ["ActionCommandReceipt", "ActionVerdict", "ReversibleAction"]
