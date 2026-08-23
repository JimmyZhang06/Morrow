"""Database primitives shared by isolated feature modules.

Feature modules may import this file, but this file must never import a feature module.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, MetaData, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utc_now() -> datetime:
    """Return an aware UTC timestamp for Python-side defaults."""

    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Single SQLAlchemy declarative registry for the application."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """Application-generated UUID primary key.

    UUIDv4 is the portable baseline. A future UUIDv7 migration must preserve this type contract.
    """

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class VaultScopedMixin:
    """Marks a row as belonging to exactly one privacy boundary."""

    @declared_attr.directive
    def vault_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(Uuid(as_uuid=True), nullable=False, index=True)


class TimestampMixin:
    """Common creation/update timestamps; immutable models may override updated_at."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

