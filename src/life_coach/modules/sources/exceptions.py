"""Safe domain exceptions for the source vault.

Exception messages intentionally contain identifiers and version numbers only.  Source
content, hashes, object keys, and index payloads must never be copied into API errors.
"""

from __future__ import annotations


class SourceError(Exception):
    """Base class for expected source-vault failures."""


class SourceNotFound(SourceError):
    """The requested source is unavailable inside the caller's vault boundary."""


class SourceDeleted(SourceError):
    """The source has been tombstoned and cannot be read or changed."""


class RevisionConflict(SourceError):
    """An optimistic revision or source-generation precondition failed."""

    def __init__(self, *, expected_revision: int, current_revision: int) -> None:
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        super().__init__(
            f"expected revision {expected_revision}, current revision is {current_revision}"
        )


class ImmutableRevisionError(SourceError):
    """An existing SourceRevision was changed instead of superseded."""


class InvalidSourceData(SourceError, ValueError):
    """A source payload violates a structural or privacy invariant."""
