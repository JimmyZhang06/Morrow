"""Saved conversations: immutable source identities and encrypted generated replies."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from life_coach.shared.database import Base, TimestampMixin, UUIDPrimaryKeyMixin, VaultScopedMixin


def clear_conversation_answers(
    session: Session, *, vault_id: uuid.UUID, document_id: uuid.UUID | None = None
) -> None:
    from sqlalchemy import select, update

    from life_coach.modules.sources.models import SourceFragment, SourceRevision

    statement = update(ConversationTurn).where(ConversationTurn.vault_id == vault_id)
    if document_id is not None:
        fragments = (
            select(SourceFragment.id)
            .join(
                SourceRevision,
                (SourceRevision.id == SourceFragment.revision_id)
                & (SourceRevision.vault_id == SourceFragment.vault_id),
            )
            .where(SourceFragment.vault_id == vault_id, SourceRevision.document_id == document_id)
        )
        material_chats = select(ConversationMaterial.conversation_id).where(
            ConversationMaterial.vault_id == vault_id,
            ConversationMaterial.fragment_id.in_(fragments),
        )
        question_chats = select(ConversationTurn.conversation_id).where(
            ConversationTurn.vault_id == vault_id,
            ConversationTurn.question_fragment_id.in_(fragments),
        )
        affected_fragments = {str(fragment_id) for fragment_id in session.scalars(fragments)}
        statement = statement.where(
            ConversationTurn.conversation_id.in_(material_chats.union(question_chats))
            | ConversationTurn.conversation_id.in_(
                {
                    turn.conversation_id
                    for turn in session.scalars(
                        select(ConversationTurn).where(
                            ConversationTurn.vault_id == vault_id,
                            ConversationTurn.retrieval.is_not(None),
                        )
                    )
                    if set((turn.retrieval or {}).get("fragment_ids", [])) & affected_fragments
                }
            )
        )
    session.execute(statement.values(answer_ciphertext=None, citations=None, state="canceled"))


class Conversation(UUIDPrimaryKeyMixin, VaultScopedMixin, TimestampMixin, Base):
    __tablename__ = "conversation"
    __table_args__ = (
        UniqueConstraint("vault_id", "id"),
        ForeignKeyConstraint(["vault_id"], ["vault.id"]),
    )
    include_reviewed_memories: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    care_origin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    title_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    title_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationMaterial(UUIDPrimaryKeyMixin, VaultScopedMixin, Base):
    __tablename__ = "conversation_material"
    __table_args__ = (
        ForeignKeyConstraint(
            ["vault_id", "conversation_id"], ["conversation.vault_id", "conversation.id"]
        ),
        ForeignKeyConstraint(
            ["vault_id", "fragment_id"], ["source_fragment.vault_id", "source_fragment.id"]
        ),
        UniqueConstraint("vault_id", "conversation_id", "fragment_id"),
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    fragment_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class ConversationTurn(UUIDPrimaryKeyMixin, VaultScopedMixin, TimestampMixin, Base):
    __tablename__ = "conversation_turn"
    retrieval: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("vault_id", "id", name="uq_conversation_turn_vault_id_id"),
        UniqueConstraint(
            "vault_id", "conversation_id", "request_id", name="uq_conversation_turn_request"
        ),
        UniqueConstraint(
            "vault_id", "conversation_id", "position", name="uq_conversation_turn_position"
        ),
        ForeignKeyConstraint(
            ["vault_id", "conversation_id"], ["conversation.vault_id", "conversation.id"]
        ),
        ForeignKeyConstraint(
            ["vault_id", "question_fragment_id"], ["source_fragment.vault_id", "source_fragment.id"]
        ),
        ForeignKeyConstraint(["principal_id"], ["principal.id"]),
        CheckConstraint(
            "state IN ('queued','running','completed','canceled','failed','unknown')", name="state"
        ),
        CheckConstraint("position > 0 AND membership_generation > 0", name="positive_versions"),
        Index(
            "uq_conversation_active_turn",
            "vault_id",
            "conversation_id",
            unique=True,
            postgresql_where=text("state IN ('queued','running')"),
            sqlite_where=text("state IN ('queued','running')"),
        ),
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    question_fragment_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    membership_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    model_binding: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answer_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    citations: Mapped[list[dict[str, object]] | None] = mapped_column(JSON(none_as_null=True))
