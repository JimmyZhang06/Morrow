"""SQLAlchemy models for the vault privacy boundary."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Integer, text
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import Mapped, mapped_column

from life_coach.shared.database import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class DataClass(StrEnum):
    """Sensitivity label used to reduce, never expand, allowed processing."""

    NORMAL = "normal"
    SENSITIVE = "sensitive"
    HIGHLY_SENSITIVE = "highly_sensitive"


class CreatedBy(StrEnum):
    """Originator category without copying user content into metadata."""

    USER = "user"
    SYSTEM_COMPONENT = "system_component"
    IMPORT = "import"


def data_class_type() -> SqlEnum:
    """Return a portable enum type which persists public values, not member names."""

    return SqlEnum(
        DataClass,
        name="data_class",
        native_enum=False,
        validate_strings=True,
        values_callable=lambda enum: [member.value for member in enum],
        length=32,
    )


def created_by_type() -> SqlEnum:
    """Return a portable enum type which persists public values, not member names."""

    return SqlEnum(
        CreatedBy,
        name="created_by",
        native_enum=False,
        validate_strings=True,
        values_callable=lambda enum: [member.value for member in enum],
        length=32,
    )


class Vault(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A tenant and privacy boundary with monotonic processing fences."""

    __tablename__ = "vault"
    __table_args__ = (
        CheckConstraint("policy_epoch >= 0", name="policy_epoch_nonnegative"),
        CheckConstraint("source_generation >= 0", name="source_generation_nonnegative"),
    )

    policy_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    source_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[CreatedBy] = mapped_column(
        created_by_type(), nullable=False, default=CreatedBy.USER
    )
    data_class: Mapped[DataClass] = mapped_column(
        data_class_type(), nullable=False, default=DataClass.SENSITIVE
    )
