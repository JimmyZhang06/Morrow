"""Application-layer composition for authoritative cross-module workflows."""

from .model_gateway import (
    GovernedModelGateway,
    KnowledgeAuthorizationSnapshotAdapter,
    KnowledgeEvidenceAuthorityAdapter,
    ModelTaskDefinition,
    PreparedModelInvocation,
    SourceAuthoritySnapshot,
    SourceConsentAuthority,
    SourceFragmentPlaintextReader,
)
from .model_runtime import (
    GovernedModelRuntime,
    ModelResultContext,
    ModelResultPersister,
    ModelResultRejected,
    ModelRunDispatchConflict,
    ModelRunFinalizationConflict,
    ModelRunFingerprintFactory,
    ModelRunProviderOutcomeUnknown,
    ModelRunResultDiscarded,
    ModelRunResultPersistenceError,
    ModelRuntimeError,
    ModelRunTimeout,
)

__all__ = [
    "GovernedModelGateway",
    "GovernedModelRuntime",
    "KnowledgeAuthorizationSnapshotAdapter",
    "KnowledgeEvidenceAuthorityAdapter",
    "ModelResultContext",
    "ModelResultPersister",
    "ModelResultRejected",
    "ModelRunDispatchConflict",
    "ModelRunFinalizationConflict",
    "ModelRunFingerprintFactory",
    "ModelRunProviderOutcomeUnknown",
    "ModelRunResultDiscarded",
    "ModelRunResultPersistenceError",
    "ModelRunTimeout",
    "ModelRuntimeError",
    "ModelTaskDefinition",
    "PreparedModelInvocation",
    "SourceAuthoritySnapshot",
    "SourceConsentAuthority",
    "SourceFragmentPlaintextReader",
]
