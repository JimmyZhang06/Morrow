"""Framework-free value objects returned by source services.

These contracts contain source identifiers and lifecycle metadata only.  In particular,
deletion plans never copy ciphertext, hashes, object keys, lexical terms, or embeddings.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .models import SourceDocument, SourceRevision


class DeletionSink(StrEnum):
    """Every storage boundary that a complete deletion workflow must visit."""

    POSTGRES_SOURCE = "postgres_source"
    POSTGRES_DERIVED = "postgres_derived"
    FTS = "fts"
    VECTOR = "vector"
    OBJECT_STORE = "object_store"
    CACHE = "cache"
    GRAPH_PROJECTION = "graph_projection"
    MODEL_ARTIFACTS = "model_artifacts"
    INTEGRATION_COPY = "integration_copy"
    BACKUP_TOMBSTONE = "backup_tombstone"


class DeletionStepState(StrEnum):
    """Initial state for a sink-specific deletion step."""

    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class DeletionStep:
    """A content-free instruction to purge one storage sink."""

    sink: DeletionSink
    state: DeletionStepState = DeletionStepState.PENDING


@dataclass(frozen=True, slots=True)
class SourceDeletionPlan:
    """Content-free plan handed to the asynchronous privacy-critical workflow."""

    vault_id: uuid.UUID
    document_id: uuid.UUID
    tombstoned_at: datetime
    policy_epoch: int
    source_generation: int
    steps: tuple[DeletionStep, ...]


@dataclass(frozen=True, slots=True)
class SourceWriteResult:
    """Rows produced by an atomic document/revision write plus its generation."""

    document: SourceDocument
    revision: SourceRevision
    source_generation: int


@dataclass(frozen=True, slots=True)
class SourceReadResult:
    """A vault-scoped document and its current immutable revision."""

    document: SourceDocument
    revision: SourceRevision
