"""PostgreSQL jobs, outbox, and outbound-operation execution fences."""

from life_coach.jobs.contracts import (
    AuthoritativeFenceReader,
    DispatchLease,
    FenceSnapshot,
    IdempotencyConflict,
    JobExecutionContext,
    JobSpec,
    OutboundOperationSpec,
    OutboxEventSpec,
)
from life_coach.jobs.enums import (
    CompletionStatus,
    FailureClass,
    FenceCheckpoint,
    JobQueue,
    JobState,
    OutboundOperationState,
)
from life_coach.jobs.models import Job, OutboundOperation, OutboxEvent
from life_coach.jobs.repository import (
    GlobalDispatcherRepository,
    OutboundOperationRepository,
    VaultJobRepository,
    VaultProcessorRepository,
)

__all__ = [
    "AuthoritativeFenceReader",
    "CompletionStatus",
    "DispatchLease",
    "FailureClass",
    "FenceCheckpoint",
    "FenceSnapshot",
    "GlobalDispatcherRepository",
    "IdempotencyConflict",
    "Job",
    "JobExecutionContext",
    "JobQueue",
    "JobSpec",
    "JobState",
    "OutboundOperation",
    "OutboundOperationRepository",
    "OutboundOperationSpec",
    "OutboundOperationState",
    "OutboxEvent",
    "OutboxEventSpec",
    "VaultJobRepository",
    "VaultProcessorRepository",
]
