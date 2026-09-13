"""Local-only resumable backfill cursor; no diary text or vendor credentials."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from life_coach.shared.database import Base, TimestampMixin, UUIDPrimaryKeyMixin, VaultScopedMixin


class DiaryIndexJob(UUIDPrimaryKeyMixin, VaultScopedMixin, TimestampMixin, Base):
    __tablename__ = "diary_index_job"
    __table_args__ = (
        ForeignKeyConstraint(["vault_id"], ["vault.id"]),
        ForeignKeyConstraint(["principal_id"], ["principal.id"]),
        CheckConstraint("state IN ('queued', 'completed', 'canceled', 'failed')", name="state"),
        CheckConstraint("processed >= 0 AND indexed >= 0", name="counts"),
        CheckConstraint("membership_generation > 0", name="membership"),
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    membership_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    cursor: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    indexed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
