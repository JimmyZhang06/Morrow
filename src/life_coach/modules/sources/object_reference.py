"""Vault-bound technical references to externally stored source payloads."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from .exceptions import InvalidSourceData

_MAX_OBJECT_KEY_LENGTH = 1024
_MAX_SEGMENTS = 32
_TECHNICAL_SEGMENT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?")
_OBJECT_KEY = re.compile(r"vaults/[0-9a-f]{32}/objects/(.+)")


def _validate_object_key_shape(value: object) -> str:
    if not isinstance(value, str) or len(value) > _MAX_OBJECT_KEY_LENGTH:
        raise InvalidSourceData("object_ref.object_key is invalid")
    match = _OBJECT_KEY.fullmatch(value)
    if match is None:
        raise InvalidSourceData("object_ref.object_key must use the canonical namespace")
    segments = match.group(1).split("/")
    if len(segments) > _MAX_SEGMENTS or any(
        segment in {"", ".", ".."} or _TECHNICAL_SEGMENT.fullmatch(segment) is None
        for segment in segments
    ):
        raise InvalidSourceData("object_ref.object_key must use canonical technical segments")
    return value


@dataclass(frozen=True, slots=True)
class VaultObjectReference:
    """A content-free object-store key cryptographically outside this module.

    The canonical key embeds the owning vault.  This is an authorization binding, not
    an encryption claim: the object-store adapter must still enforce access controls.
    """

    vault_id: UUID
    object_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.vault_id, UUID):
            raise InvalidSourceData("object_ref.vault_id must be a UUID")
        object_key = _validate_object_key_shape(self.object_key)
        prefix = self.prefix_for(self.vault_id)
        if not object_key.startswith(prefix):
            raise InvalidSourceData("object_ref.object_key is not bound to its vault")

    @staticmethod
    def prefix_for(vault_id: UUID) -> str:
        """Return the only accepted object namespace for ``vault_id``."""

        return f"vaults/{vault_id.hex}/objects/"

    @classmethod
    def validated(cls, value: VaultObjectReference) -> VaultObjectReference:
        """Reconstruct an instance so post-init validation cannot be bypassed."""

        if not isinstance(value, cls):
            raise InvalidSourceData("object_ref must be a VaultObjectReference")
        return cls(vault_id=value.vault_id, object_key=value.object_key)


class VaultObjectKeyType(TypeDecorator[str]):
    """Reject malformed object keys on SQLAlchemy ORM and Core bind paths."""

    impl = String(1024)
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        del dialect
        if value is None:
            return None
        return _validate_object_key_shape(value)
