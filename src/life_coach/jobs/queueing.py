"""Static, body-free routing metadata for worker pools."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from life_coach.jobs.enums import JobQueue


@dataclass(frozen=True, slots=True)
class QueueMetadata:
    default_priority: int
    dedicated_worker_pool: bool = False
    reserved_database_connections: bool = False
    escalation_alerts: bool = False


QUEUE_METADATA = MappingProxyType(
    {
        JobQueue.INTERACTIVE: QueueMetadata(default_priority=80),
        JobQueue.INGEST_TEXT: QueueMetadata(default_priority=50),
        JobQueue.MEDIA: QueueMetadata(default_priority=40),
        JobQueue.REFLECTION: QueueMetadata(default_priority=20),
        JobQueue.NARRATIVE: QueueMetadata(default_priority=10),
        JobQueue.PRIVACY_CRITICAL: QueueMetadata(
            default_priority=100,
            dedicated_worker_pool=True,
            reserved_database_connections=True,
            escalation_alerts=True,
        ),
        JobQueue.INTEGRATION: QueueMetadata(default_priority=45),
    }
)


def queue_metadata(queue: JobQueue) -> QueueMetadata:
    return QUEUE_METADATA[queue]
