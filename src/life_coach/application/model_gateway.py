"""The production model exit and its authoritative Source/Consent adapters.

Callers select only a registered task and Source fragment identifiers. Provider,
region, retention, sensitivity, fences, consent identity, and input references are
derived from server configuration and current database state. The lower-level
``ModelGateway`` remains the provider-neutral validation kernel; this module is the
only production composition allowed to mint its ``ModelRunSpec``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from life_coach.ai.contracts import (
    ModelInputKind,
    ModelInputRef,
    ModelRunSpec,
    ModelTaskPolicy,
    RetentionPolicy,
    SchemaRef,
    SensitivityLevel,
)
from life_coach.ai.memory import EvidenceSource
from life_coach.ai.provider import ModelGateway, UntrustedModelInput
from life_coach.modules.consent.exceptions import ConsentDenied
from life_coach.modules.consent.models import ConsentPurpose
from life_coach.modules.consent.provider_policy import (
    TRUSTED_PROVIDER_REGISTRY,
    ProviderPolicy,
)
from life_coach.modules.consent.service import ConsentResolution, require_consent
from life_coach.modules.identity.exceptions import StaleVaultSnapshot
from life_coach.modules.identity.models import DataClass, Vault
from life_coach.modules.identity.service import (
    VaultSnapshot,
    capture_vault_snapshot,
    require_current_snapshot,
)
from life_coach.modules.knowledge.contracts import (
    AuthorizationSnapshot,
    EvidenceAnchor,
    EvidenceSourceReference,
    EvidenceSourceState,
    VerifiedEvidenceAnchor,
)
from life_coach.modules.knowledge.enums import AuthorizationPurpose, SourceEvidenceStatus
from life_coach.modules.knowledge.enums import DataClass as KnowledgeDataClass
from life_coach.modules.sources.models import SourceDocument, SourceFragment, SourceRevision

_MAX_FRAGMENTS_PER_RUN = 32
_DATA_CLASS_RANK = {
    DataClass.NORMAL: 0,
    DataClass.SENSITIVE: 1,
    DataClass.HIGHLY_SENSITIVE: 2,
}
_KNOWLEDGE_CONSENT_PURPOSE = {
    AuthorizationPurpose.MEMORY_CREATE: ConsentPurpose.LONG_TERM_INFERENCE,
    AuthorizationPurpose.MEMORY_REVIEW: ConsentPurpose.PASSIVE_QA,
    AuthorizationPurpose.MEMORY_HISTORY: ConsentPurpose.PASSIVE_QA,
    AuthorizationPurpose.PROACTIVE_RESURFACING: ConsentPurpose.PROACTIVE_RESURFACING,
}


class SourceAuthorityUnavailable(RuntimeError):
    """A requested Source or its current authorization is unavailable."""


class SourceIntegrityViolation(RuntimeError):
    """Decrypted Source text does not match its authoritative content fingerprint."""


class ModelInvocationDenied(RuntimeError):
    """Current consent or server provider configuration denies a model call."""


class SourceFragmentPlaintextReader(Protocol):
    """Minimal KMS/object-store boundary for decrypting one authoritative fragment."""

    def read_text(
        self,
        *,
        vault_id: uuid.UUID,
        document_id: uuid.UUID,
        revision_id: uuid.UUID,
        revision_no: int,
        fragment_id: uuid.UUID,
        ciphertext: bytes,
    ) -> str:
        """Return authenticated plaintext or raise without exposing content."""
        ...


@dataclass(frozen=True, slots=True)
class AuthorizedSourceFragment:
    """Current Source row plus plaintext released by the decryption authority."""

    document_id: uuid.UUID
    revision_id: uuid.UUID
    fragment_id: uuid.UUID
    recorded_at: datetime
    data_class: DataClass
    text: str = field(repr=False)
    text_hash: str

    def evidence_source(self, *, vault_id: uuid.UUID, source_generation: int) -> EvidenceSource:
        return EvidenceSource(
            vault_id=str(vault_id),
            source_document_id=str(self.document_id),
            source_revision_id=str(self.revision_id),
            source_fragment_id=str(self.fragment_id),
            source_generation=source_generation,
            text=self.text,
            text_hash=self.text_hash,
            consent_allowed=True,
            deleted=False,
        )


@dataclass(frozen=True, slots=True)
class SourceAuthoritySnapshot:
    """Reproducible Source/Consent facts used to mint one model run."""

    vault: VaultSnapshot
    purpose: ConsentPurpose
    consent_snapshot_id: str
    consent_snapshot_uuid: uuid.UUID
    consent_record_ids: tuple[uuid.UUID, ...]
    provider_policy: ProviderPolicy
    fragments: tuple[AuthorizedSourceFragment, ...]
    actual_sensitivity: SensitivityLevel

    @property
    def document_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(dict.fromkeys(fragment.document_id for fragment in self.fragments))

    @property
    def evidence_sources(self) -> tuple[EvidenceSource, ...]:
        return tuple(
            fragment.evidence_source(
                vault_id=self.vault.vault_id,
                source_generation=self.vault.source_generation,
            )
            for fragment in self.fragments
        )


@dataclass(frozen=True, slots=True)
class _ConsentFacts:
    vault: VaultSnapshot
    record_ids: tuple[uuid.UUID, ...]
    provider_policy: ProviderPolicy
    snapshot_id: str
    snapshot_uuid: uuid.UUID


class SourceConsentAuthority:
    """Load only live current Source fragments under current purpose consent."""

    def __init__(self, plaintext_reader: SourceFragmentPlaintextReader) -> None:
        self._plaintext_reader = plaintext_reader

    def prepare(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        fragment_ids: Iterable[uuid.UUID],
        purpose: ConsentPurpose,
    ) -> SourceAuthoritySnapshot:
        requested_ids = tuple(fragment_ids)
        if (
            not requested_ids
            or len(requested_ids) > _MAX_FRAGMENTS_PER_RUN
            or len(set(requested_ids)) != len(requested_ids)
        ):
            raise SourceAuthorityUnavailable("Source selection is unavailable")
        if any(
            not isinstance(fragment_id, uuid.UUID) or fragment_id.int == 0
            for fragment_id in requested_ids
        ):
            raise SourceAuthorityUnavailable("Source selection is unavailable")

        rows = session.execute(
            select(SourceFragment, SourceRevision, SourceDocument)
            .join(
                SourceRevision,
                (SourceRevision.vault_id == SourceFragment.vault_id)
                & (SourceRevision.id == SourceFragment.revision_id),
            )
            .join(
                SourceDocument,
                (SourceDocument.vault_id == SourceRevision.vault_id)
                & (SourceDocument.id == SourceRevision.document_id),
            )
            .join(Vault, Vault.id == SourceFragment.vault_id)
            .where(
                SourceFragment.vault_id == vault_id,
                SourceFragment.id.in_(requested_ids),
                SourceFragment.deleted_at.is_(None),
                SourceRevision.deleted_at.is_(None),
                SourceDocument.deleted_at.is_(None),
                Vault.deleted_at.is_(None),
                SourceDocument.current_revision_id == SourceRevision.id,
            )
        ).all()
        by_id = {
            fragment.id: (fragment, revision, document) for fragment, revision, document in rows
        }
        if len(by_id) != len(requested_ids):
            raise SourceAuthorityUnavailable("Source selection is unavailable")

        document_ids = tuple(dict.fromkeys(by_id[item][2].id for item in requested_ids))
        consent = self._resolve_consent(
            session=session,
            vault_id=vault_id,
            document_ids=document_ids,
            purpose=purpose,
        )
        fragments: list[AuthorizedSourceFragment] = []
        for fragment_id in requested_ids:
            fragment, revision, document = by_id[fragment_id]
            try:
                plaintext = self._plaintext_reader.read_text(
                    vault_id=vault_id,
                    document_id=document.id,
                    revision_id=revision.id,
                    revision_no=revision.revision_no,
                    fragment_id=fragment.id,
                    ciphertext=fragment.text_ciphertext,
                )
            except Exception:
                raise SourceAuthorityUnavailable("Source plaintext is unavailable") from None
            if not isinstance(plaintext, str) or not plaintext:
                raise SourceIntegrityViolation("Source plaintext failed integrity verification")
            digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
            if fragment.text_hash != digest:
                raise SourceIntegrityViolation("Source plaintext failed integrity verification")
            effective_class = max(
                (document.data_class, revision.data_class, fragment.data_class),
                key=_DATA_CLASS_RANK.__getitem__,
            )
            fragments.append(
                AuthorizedSourceFragment(
                    document_id=document.id,
                    revision_id=revision.id,
                    fragment_id=fragment.id,
                    recorded_at=revision.created_at,
                    data_class=effective_class,
                    text=plaintext,
                    text_hash=digest,
                )
            )

        actual_sensitivity = SensitivityLevel(
            max((item.data_class for item in fragments), key=_DATA_CLASS_RANK.__getitem__).value
        )
        try:
            require_current_snapshot(session, consent.vault)
        except StaleVaultSnapshot:
            raise SourceAuthorityUnavailable("Source authorization changed during read") from None
        return SourceAuthoritySnapshot(
            vault=consent.vault,
            purpose=purpose,
            consent_snapshot_id=consent.snapshot_id,
            consent_snapshot_uuid=consent.snapshot_uuid,
            consent_record_ids=consent.record_ids,
            provider_policy=consent.provider_policy,
            fragments=tuple(fragments),
            actual_sensitivity=actual_sensitivity,
        )

    def assert_current(self, *, session: Session, snapshot: SourceAuthoritySnapshot) -> None:
        """Re-resolve consent and fences immediately before provider or persistence I/O."""

        try:
            require_current_snapshot(session, snapshot.vault)
        except StaleVaultSnapshot:
            raise ModelInvocationDenied("Source authorization is no longer current") from None
        current = self._resolve_consent(
            session=session,
            vault_id=snapshot.vault.vault_id,
            document_ids=snapshot.document_ids,
            purpose=snapshot.purpose,
        )
        if (
            current.snapshot_id != snapshot.consent_snapshot_id
            or current.record_ids != snapshot.consent_record_ids
            or current.provider_policy != snapshot.provider_policy
            or current.vault != snapshot.vault
        ):
            raise ModelInvocationDenied("Source authorization is no longer current")

    @staticmethod
    def _resolve_consent(
        *,
        session: Session,
        vault_id: uuid.UUID,
        document_ids: tuple[uuid.UUID, ...],
        purpose: ConsentPurpose,
    ) -> _ConsentFacts:
        vault = capture_vault_snapshot(session, vault_id)
        resolutions: list[ConsentResolution] = []
        try:
            for document_id in document_ids:
                resolutions.append(
                    require_consent(
                        session,
                        vault_id=vault_id,
                        purpose=purpose,
                        source_document_id=document_id,
                    )
                )
        except ConsentDenied:
            raise SourceAuthorityUnavailable("Source authorization is unavailable") from None
        record_ids = tuple(
            resolution.record_id for resolution in resolutions if resolution.record_id is not None
        )
        if len(record_ids) != len(resolutions):
            raise SourceAuthorityUnavailable("Source authorization is unavailable")
        provider_policy = ProviderPolicy.meet(
            *(resolution.provider_policy for resolution in resolutions)
        )
        canonical = json.dumps(
            {
                "vault_id": str(vault_id),
                "purpose": purpose.value,
                "document_ids": [str(value) for value in document_ids],
                "record_ids": [str(value) for value in record_ids],
                "policy_epoch": vault.policy_epoch,
                "source_generation": vault.source_generation,
                "provider_policy": provider_policy.to_json(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        snapshot_id = f"consent:{digest}"
        return _ConsentFacts(
            vault=vault,
            record_ids=record_ids,
            provider_policy=provider_policy,
            snapshot_id=snapshot_id,
            snapshot_uuid=uuid.uuid5(uuid.NAMESPACE_URL, snapshot_id),
        )


@dataclass(frozen=True, slots=True)
class ModelTaskDefinition:
    """Server-owned model task and provider data-handling configuration."""

    task_type: str
    consent_purpose: ConsentPurpose
    provider: str
    model: str
    model_revision: str
    prompt_template_version: str
    schema_version: str
    pipeline_version: str
    required_capabilities: frozenset[str]
    data_residency: str
    retention_policy: RetentionPolicy
    provider_retention_days: int | None
    provider_training_use_enabled: bool
    max_sensitivity: SensitivityLevel
    output_type: type[BaseModel]
    latency_budget_ms: int = 10_000
    cost_budget: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class PreparedModelInvocation:
    """In-memory authorized input that must never cross a durable boundary."""

    task: ModelTaskDefinition
    snapshot: SourceAuthoritySnapshot = field(repr=False)
    input_refs: tuple[ModelInputRef, ...]
    model_input: UntrustedModelInput = field(repr=False)


class GovernedModelGateway:
    """Prepare authority-bound inputs and invoke the sole provider kernel.

    Transaction lifetime belongs to ``GovernedModelRuntime``. This class never
    opens or commits a transaction and no longer exposes a session-spanning
    convenience ``run`` method.
    """

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        source_authority: SourceConsentAuthority,
        tasks: Iterable[ModelTaskDefinition],
    ) -> None:
        self._gateway = gateway
        self._source_authority = source_authority
        self._tasks: dict[str, ModelTaskDefinition] = {}
        for task in tasks:
            if task.task_type in self._tasks:
                raise ValueError("duplicate governed model task")
            self._validate_task_definition(task)
            # Constructing a policy now validates every technical identifier and
            # output schema before the task can enter the runtime registry.
            self._policy_for(task)
            self._tasks[task.task_type] = task

    @staticmethod
    def _validate_task_definition(task: ModelTaskDefinition) -> None:
        if task.provider_retention_days is not None and task.provider_retention_days < 0:
            raise ValueError("provider retention days must be non-negative")
        if type(task.provider_training_use_enabled) is not bool:
            raise ValueError("provider training-use configuration must be boolean")
        if (
            task.retention_policy is RetentionPolicy.ZERO_RETENTION
            and task.provider_retention_days != 0
        ):
            raise ValueError("zero-retention tasks must configure zero provider days")
        supported_regions = TRUSTED_PROVIDER_REGISTRY.get(task.provider)
        if supported_regions is None or task.data_residency not in supported_regions:
            raise ValueError("task provider/region pair is not registered")
        if task.output_type.model_config.get("extra") != "forbid":
            raise ValueError("governed output models must forbid extra fields")

    def prepare(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        task_type: str,
        fragment_ids: Iterable[uuid.UUID],
    ) -> PreparedModelInvocation:
        task = self._tasks.get(task_type)
        if task is None:
            raise ModelInvocationDenied("model task is unavailable")
        snapshot = self._source_authority.prepare(
            session=session,
            vault_id=vault_id,
            fragment_ids=fragment_ids,
            purpose=task.consent_purpose,
        )
        self._authorize_provider(task, snapshot.provider_policy)
        if snapshot.actual_sensitivity.rank > task.max_sensitivity.rank:
            raise ModelInvocationDenied("model task sensitivity is unavailable")

        input_refs = tuple(
            ModelInputRef(
                vault_id=str(vault_id),
                kind=ModelInputKind.SOURCE_FRAGMENT,
                object_id=str(fragment.fragment_id),
            )
            for fragment in snapshot.fragments
        )
        model_input = UntrustedModelInput(
            data={
                "fragments": [
                    {
                        "source_fragment_id": str(fragment.fragment_id),
                        "text": fragment.text,
                    }
                    for fragment in snapshot.fragments
                ]
            },
            source_refs=tuple(str(fragment.fragment_id) for fragment in snapshot.fragments),
        )
        return PreparedModelInvocation(
            task=task,
            snapshot=snapshot,
            input_refs=input_refs,
            model_input=model_input,
        )

    def assert_current(
        self,
        *,
        session: Session,
        prepared: PreparedModelInvocation,
    ) -> None:
        """Revalidate the exact prepared authority inside the dispatch transaction."""

        self._source_authority.assert_current(session=session, snapshot=prepared.snapshot)
        self._authorize_provider(prepared.task, prepared.snapshot.provider_policy)

    def invoke(
        self,
        *,
        prepared: PreparedModelInvocation,
        run_id: uuid.UUID,
    ) -> BaseModel:
        """Perform provider I/O for a committed receipt, with no database session."""

        task = prepared.task
        snapshot = prepared.snapshot
        spec = ModelRunSpec(
            run_id=str(run_id),
            vault_id=str(snapshot.vault.vault_id),
            policy=self._policy_for(task),
            provider=task.provider,
            model=task.model,
            model_revision=task.model_revision,
            prompt_template_version=task.prompt_template_version,
            schema_version=task.schema_version,
            pipeline_version=task.pipeline_version,
            consent_snapshot_id=snapshot.consent_snapshot_id,
            policy_epoch=snapshot.vault.policy_epoch,
            source_generation=snapshot.vault.source_generation,
            actual_sensitivity=snapshot.actual_sensitivity,
            data_residency=task.data_residency,
            retention_policy=task.retention_policy,
            input_refs=prepared.input_refs,
        )
        return self._gateway.run(spec, prepared.model_input, task.output_type)

    @staticmethod
    def _policy_for(task: ModelTaskDefinition) -> ModelTaskPolicy:
        return ModelTaskPolicy(
            task_type=task.task_type,
            required_capabilities=task.required_capabilities,
            allowed_providers=frozenset({task.provider}),
            data_residency=frozenset({task.data_residency}),
            retention_policy=task.retention_policy,
            max_sensitivity=task.max_sensitivity,
            input_schema=SchemaRef(
                name=UntrustedModelInput.__name__,
                version="1",
                json_schema=UntrustedModelInput.model_json_schema(),
            ),
            output_schema=SchemaRef(
                name=task.output_type.__name__,
                version=task.schema_version,
                json_schema=task.output_type.model_json_schema(),
            ),
            latency_budget_ms=task.latency_budget_ms,
            cost_budget=task.cost_budget,
        )

    @staticmethod
    def _authorize_provider(task: ModelTaskDefinition, policy: ProviderPolicy) -> None:
        supported_regions = TRUSTED_PROVIDER_REGISTRY.get(task.provider)
        if (
            task.provider not in policy.allowed_providers
            or task.data_residency not in policy.processing_regions
            or supported_regions is None
            or task.data_residency not in supported_regions
        ):
            raise ModelInvocationDenied("provider or region is not authorized")
        if not policy.training_use_allowed and task.provider_training_use_enabled:
            raise ModelInvocationDenied("provider training use is not authorized")
        if policy.zero_retention_required and (
            task.retention_policy is not RetentionPolicy.ZERO_RETENTION
            or task.provider_retention_days != 0
        ):
            raise ModelInvocationDenied("zero retention is required")
        if policy.max_retention_days is not None and (
            task.provider_retention_days is None
            or task.provider_retention_days > policy.max_retention_days
        ):
            raise ModelInvocationDenied("provider retention exceeds consent")


class KnowledgeEvidenceAuthorityAdapter:
    """Implement Knowledge's evidence port from the same Source/Consent authority."""

    def __init__(self, source_authority: SourceConsentAuthority) -> None:
        self._source_authority = source_authority

    def verify(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        anchor: EvidenceAnchor,
        purpose: AuthorizationPurpose,
        at: datetime,
    ) -> VerifiedEvidenceAnchor:
        checked_at = _aware_utc(at)
        snapshot = self._source_authority.prepare(
            session=session,
            vault_id=vault_id,
            fragment_ids=(anchor.source_fragment_id,),
            purpose=_KNOWLEDGE_CONSENT_PURPOSE[purpose],
        )
        fragment = snapshot.fragments[0]
        if not 0 <= anchor.quote_start < anchor.quote_end <= len(fragment.text):
            raise SourceIntegrityViolation("evidence span failed Source verification")
        quote = fragment.text[anchor.quote_start : anchor.quote_end]
        quote_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()
        if quote_hash != anchor.quote_hash:
            raise SourceIntegrityViolation("evidence span failed Source verification")
        self._source_authority.assert_current(session=session, snapshot=snapshot)
        return VerifiedEvidenceAnchor(
            vault_id=vault_id,
            source_document_id=fragment.document_id,
            source_revision_id=fragment.revision_id,
            source_fragment_id=fragment.fragment_id,
            relation=anchor.relation,
            quote_start=anchor.quote_start,
            quote_end=anchor.quote_end,
            quote_hash=quote_hash,
            extractor_reason=anchor.extractor_reason,
            strength_band=anchor.strength_band,
            source_recorded_at=fragment.recorded_at,
            source_data_class=KnowledgeDataClass(fragment.data_class.value),
            source_content_fingerprint=fragment.text_hash,
            authorization_snapshot_id=snapshot.consent_snapshot_uuid,
            policy_epoch=snapshot.vault.policy_epoch,
            source_generation=snapshot.vault.source_generation,
            verified_at=checked_at,
            created_by=anchor.created_by,
            model_run_id=anchor.model_run_id,
        )

    def resolve_current(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        references: tuple[EvidenceSourceReference, ...],
        purpose: AuthorizationPurpose,
        at: datetime,
    ) -> tuple[EvidenceSourceState, ...]:
        checked_at = _aware_utc(at)
        states: list[EvidenceSourceState] = []
        for reference in references:
            status = SourceEvidenceStatus.UNKNOWN
            snapshot: SourceAuthoritySnapshot | None = None
            try:
                snapshot = self._source_authority.prepare(
                    session=session,
                    vault_id=vault_id,
                    fragment_ids=(reference.source_fragment_id,),
                    purpose=_KNOWLEDGE_CONSENT_PURPOSE[purpose],
                )
                fragment = snapshot.fragments[0]
                identity_matches = (
                    fragment.document_id == reference.source_document_id
                    and fragment.revision_id == reference.source_revision_id
                )
                fence_matches = (
                    snapshot.vault.policy_epoch == reference.policy_epoch
                    and snapshot.vault.source_generation == reference.source_generation
                    and snapshot.consent_snapshot_uuid == reference.authorization_snapshot_id
                )
                status = (
                    SourceEvidenceStatus.LIVE
                    if identity_matches and fence_matches
                    else SourceEvidenceStatus.STALE
                )
            except (SourceAuthorityUnavailable, SourceIntegrityViolation):
                pass
            states.append(
                EvidenceSourceState(
                    evidence_id=reference.evidence_id,
                    status=status,
                    authorization_snapshot_id=(
                        snapshot.consent_snapshot_uuid
                        if snapshot is not None
                        else reference.authorization_snapshot_id
                    ),
                    policy_epoch=(
                        snapshot.vault.policy_epoch
                        if snapshot is not None
                        else reference.policy_epoch
                    ),
                    source_generation=(
                        snapshot.vault.source_generation
                        if snapshot is not None
                        else reference.source_generation
                    ),
                    data_class=(
                        KnowledgeDataClass(snapshot.fragments[0].data_class.value)
                        if snapshot is not None
                        else KnowledgeDataClass.HIGHLY_SENSITIVE
                    ),
                    checked_at=checked_at,
                )
            )
        return tuple(states)


class KnowledgeAuthorizationSnapshotAdapter:
    """Implement Knowledge's authorization port from live vault-wide consent.

    Source-specific grants remain conservative default-deny here because the
    existing Knowledge authorization port contains no Source identifiers. Memory
    creation still receives its effective data class and fences from independently
    verified Source anchors before this adapter is called.
    """

    def authorize(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        purpose: AuthorizationPurpose,
        data_class: KnowledgeDataClass,
        policy_epoch: int,
        source_generation: int,
        at: datetime,
    ) -> AuthorizationSnapshot:
        _aware_utc(at)
        if not isinstance(purpose, AuthorizationPurpose) or not isinstance(
            data_class, KnowledgeDataClass
        ):
            raise SourceAuthorityUnavailable("Knowledge authorization is unavailable")
        current = capture_vault_snapshot(session, vault_id)
        if current.policy_epoch != policy_epoch or current.source_generation != source_generation:
            raise SourceAuthorityUnavailable("Knowledge authorization is stale")
        consent_purpose = _KNOWLEDGE_CONSENT_PURPOSE[purpose]
        try:
            resolution = require_consent(
                session,
                vault_id=vault_id,
                purpose=consent_purpose,
                source_document_id=None,
            )
        except ConsentDenied:
            raise SourceAuthorityUnavailable("Knowledge authorization is unavailable") from None
        if resolution.record_id is None:
            raise SourceAuthorityUnavailable("Knowledge authorization is unavailable")
        canonical = (
            f"knowledge-authorization-v1\0{vault_id}\0{purpose.value}\0"
            f"{data_class.value}\0{policy_epoch}\0{source_generation}\0"
            f"{resolution.record_id}"
        )
        return AuthorizationSnapshot(
            snapshot_id=uuid.uuid5(uuid.NAMESPACE_URL, canonical),
            vault_id=vault_id,
            purpose=purpose,
            policy_epoch=policy_epoch,
            source_generation=source_generation,
            data_class=data_class,
            allows_read=True,
            allows_proactive=(purpose is AuthorizationPurpose.PROACTIVE_RESURFACING),
        )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("authorization time must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "GovernedModelGateway",
    "KnowledgeAuthorizationSnapshotAdapter",
    "KnowledgeEvidenceAuthorityAdapter",
    "ModelInvocationDenied",
    "ModelTaskDefinition",
    "PreparedModelInvocation",
    "SourceAuthoritySnapshot",
    "SourceAuthorityUnavailable",
    "SourceConsentAuthority",
    "SourceFragmentPlaintextReader",
    "SourceIntegrityViolation",
]
