"""Application-layer composition for authoritative cross-module workflows."""

from .model_gateway import (
    GovernedModelGateway,
    KnowledgeAuthorizationSnapshotAdapter,
    KnowledgeEvidenceAuthorityAdapter,
    ModelTaskDefinition,
    SourceAuthoritySnapshot,
    SourceConsentAuthority,
    SourceFragmentPlaintextReader,
)

__all__ = [
    "GovernedModelGateway",
    "KnowledgeAuthorizationSnapshotAdapter",
    "KnowledgeEvidenceAuthorityAdapter",
    "ModelTaskDefinition",
    "SourceAuthoritySnapshot",
    "SourceConsentAuthority",
    "SourceFragmentPlaintextReader",
]
