"""Short-transaction orchestration for governed model execution.

The runtime owns three independent authorized database transactions. Provider
I/O occurs between dispatch commit and finalization, while no Session or
connection is open. Plaintext exists only in the in-memory prepared invocation.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol, cast

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from life_coach.ai.provider import (
    GatewayConfigurationError,
    ModelPolicyViolation,
    ProviderExecutionError,
    ProviderOutputDecodeError,
    ProviderUnavailableBeforeDispatch,
    StructuredOutputValidationError,
    ToolDirectiveRejected,
)
from life_coach.application.model_gateway import (
    GovernedModelGateway,
    ModelInvocationDenied,
    PreparedModelInvocation,
    SourceAuthorityUnavailable,
)
from life_coach.jobs.payloads import JsonValue, VaultRequestFingerprint, canonical_request_hash
from life_coach.modules.model_runs.contracts import (
    ModelRunArtifactRef,
    ModelRunArtifactSpec,
    ModelRunArtifactWrite,
    ModelRunDispatchTicket,
    ModelRunInputSpec,
    ModelRunProjection,
    ModelRunReceiptSpec,
    ModelRunWrite,
)
from life_coach.modules.model_runs.models import ModelRunState
from life_coach.modules.model_runs.repository import ModelRunRepository
from life_coach.platform.auth import (
    AuthenticatedPrincipal,
    AuthenticationDenied,
    AuthorizedVaultContext,
    AuthorizedVaultSession,
    VaultMembershipDenied,
)
from life_coach.platform.database import VaultAsyncSession


class ModelRuntimeError(RuntimeError):
    """Base for stable application-level model runtime failures."""


class ModelRunDispatchConflict(ModelRuntimeError):
    """A receipt was no longer authorized and claimable for this invocation."""


class ModelRunProviderOutcomeUnknown(ModelRuntimeError):
    """A provider call may have occurred but no trustworthy result is available."""


class ModelRunProviderUnavailable(ModelRuntimeError):
    """The provider was unreachable before any inference request was dispatched."""


class ModelRunTimeout(ModelRunProviderOutcomeUnknown):
    """The overall governed task budget elapsed after dispatch commit."""


class ModelRunFinalizationConflict(ModelRuntimeError):
    """The exact dispatch generation could not accept the proposed outcome."""


class ModelRunResultDiscarded(ModelRuntimeError):
    """A valid provider output was discarded because authority changed."""


class ModelRunResultPersistenceError(ModelRuntimeError):
    """The result sink failed and its transaction was rolled back."""


class ModelRunReplayInProgress(ModelRuntimeError):
    """An idempotent replay found a run whose provider outcome is unsettled."""

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        super().__init__("model run is still processing")


class ModelRunReplayTerminal(ModelRuntimeError):
    """An idempotent replay found a stable non-success terminal state."""

    def __init__(self, run_id: uuid.UUID, state: ModelRunState) -> None:
        self.run_id = run_id
        self.state = state
        super().__init__(f"model run is in terminal state {state.value}")


class ModelRunReplayArtifactMissing(ModelRuntimeError):
    """A succeeded receipt has no durable artifact identity and cannot be replayed."""

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        super().__init__("succeeded model run has no durable artifact")


class ModelResultRejected(ModelRuntimeError):
    """A task-specific sink deterministically rejected a model result."""


class ModelRunOutcomeBookkeeper(Protocol):
    """Narrow capability for settling a call after user authority disappears."""

    async def mark_failed(
        self,
        ticket: ModelRunDispatchTicket,
        *,
        safe_error_code: str,
    ) -> bool: ...

    async def mark_unknown(
        self,
        ticket: ModelRunDispatchTicket,
        *,
        safe_error_code: str,
    ) -> bool: ...


class AuthorizedVaultSessionOpener(Protocol):
    """ProductionSessionFactory-compatible repeated transaction capability."""

    async def authenticate(self, authorization: str | None) -> AuthenticatedPrincipal: ...

    def open_for_principal(
        self,
        *,
        principal: AuthenticatedPrincipal,
        vault_id: uuid.UUID | str,
        expected_membership_generation: int | None = None,
    ) -> AbstractAsyncContextManager[AuthorizedVaultSession]: ...

    def open_for_model_run_bookkeeping(
        self,
        *,
        vault_id: uuid.UUID | str,
    ) -> AbstractAsyncContextManager[ModelRunOutcomeBookkeeper]: ...


class ModelRunReceiptPort(Protocol):
    async def prepare(
        self,
        spec: ModelRunReceiptSpec,
        inputs: tuple[ModelRunInputSpec, ...],
    ) -> ModelRunWrite: ...

    async def claim_dispatch(
        self,
        run_id: uuid.UUID,
        *,
        lease_for: timedelta,
    ) -> ModelRunDispatchTicket | None: ...

    async def mark_succeeded(
        self,
        ticket: ModelRunDispatchTicket,
        *,
        provider_request_id: str | None = None,
    ) -> bool: ...

    async def mark_failed(
        self,
        ticket: ModelRunDispatchTicket,
        *,
        safe_error_code: str,
        provider_request_id: str | None = None,
    ) -> bool: ...

    async def mark_unknown(
        self,
        ticket: ModelRunDispatchTicket,
        *,
        safe_error_code: str,
        provider_request_id: str | None = None,
    ) -> bool: ...

    async def mark_denied(self, run_id: uuid.UUID, *, safe_error_code: str) -> bool: ...

    async def attach_artifact(
        self,
        ticket: ModelRunDispatchTicket,
        artifact: ModelRunArtifactSpec,
    ) -> ModelRunArtifactWrite: ...

    async def get_projection(self, run_id: uuid.UUID) -> ModelRunProjection | None: ...


type ModelRunReceiptFactory = Callable[
    [AsyncSession, uuid.UUID],
    ModelRunReceiptPort,
]


@dataclass(frozen=True, slots=True)
class ModelResultContext:
    """Trusted in-memory context supplied to one task-specific result sink."""

    run_id: uuid.UUID
    vault_id: uuid.UUID
    principal_id: uuid.UUID
    membership_generation: int
    prepared: PreparedModelInvocation = field(repr=False)


class ModelResultPersister(Protocol):
    """Persist a task result in the caller's finalization transaction.

    Implementations must not commit, roll back, call external services, or trust
    model-supplied Source/run identifiers. They must bind every derived row to
    ``context.run_id`` and the allow-list in ``context.prepared``.
    """

    async def persist(
        self,
        session: VaultAsyncSession,
        *,
        context: ModelResultContext,
        result: BaseModel,
    ) -> ModelRunArtifactSpec | None: ...


class ModelRunFingerprintFactory:
    """Mint domain-separated, Vault-bound receipts from one secret key."""

    __slots__ = ("_hmac_key",)

    def __init__(self, hmac_key: bytes) -> None:
        if not isinstance(hmac_key, bytes) or len(hmac_key) < 32:
            raise ValueError("model run fingerprint key must contain at least 32 bytes")
        self._hmac_key = bytes(hmac_key)

    def task_definition(
        self,
        *,
        vault_id: uuid.UUID,
        prepared: PreparedModelInvocation,
    ) -> VaultRequestFingerprint:
        task = prepared.task
        return self._mint(
            vault_id,
            {
                "domain": "model_run.task_definition.v1",
                "task_type": task.task_type,
                "provider": task.provider,
                "model": task.model,
                "model_revision": task.model_revision,
                "prompt_template_version": task.prompt_template_version,
                "schema_version": task.schema_version,
                "pipeline_version": task.pipeline_version,
                "required_capabilities": cast(
                    JsonValue,
                    sorted(task.required_capabilities),
                ),
                "data_residency": task.data_residency,
                "retention_policy": task.retention_policy.value,
                "provider_retention_days": task.provider_retention_days,
                "provider_training_use_enabled": task.provider_training_use_enabled,
                "max_sensitivity": task.max_sensitivity.value,
                "latency_budget_ms": task.latency_budget_ms,
                "cost_budget": str(task.cost_budget),
                "output_schema": cast(JsonValue, task.output_type.model_json_schema()),
            },
        )

    def input_content(
        self,
        *,
        vault_id: uuid.UUID,
        fragment_id: uuid.UUID,
        text_hash: str,
    ) -> VaultRequestFingerprint:
        return self._mint(
            vault_id,
            {
                "domain": "model_run.input_content.v1",
                "kind": "source_fragment",
                "object_id": str(fragment_id),
                "authoritative_text_hash": text_hash,
            },
        )

    def request(
        self,
        *,
        vault_id: uuid.UUID,
        idempotency_key: str,
        prepared: PreparedModelInvocation,
        task_definition_hash: VaultRequestFingerprint,
        inputs: tuple[ModelRunInputSpec, ...],
    ) -> VaultRequestFingerprint:
        snapshot = prepared.snapshot
        return self._mint(
            vault_id,
            {
                "domain": "model_run.request.v1",
                "idempotency_key": idempotency_key,
                "task_definition_hash": str(task_definition_hash),
                "consent_snapshot_id": snapshot.consent_snapshot_id,
                "policy_epoch": snapshot.vault.policy_epoch,
                "source_generation": snapshot.vault.source_generation,
                "inputs": [
                    {
                        "kind": item.kind.value,
                        "object_id": str(item.object_id),
                        "content_fingerprint": str(item.content_fingerprint),
                        "ordinal": item.ordinal,
                    }
                    for item in inputs
                ],
            },
        )

    def _mint(self, vault_id: uuid.UUID, payload: JsonValue) -> VaultRequestFingerprint:
        return canonical_request_hash(payload, vault_id=vault_id, hmac_key=self._hmac_key)


def _default_receipt_factory(
    session: AsyncSession,
    vault_id: uuid.UUID,
) -> ModelRunReceiptPort:
    return ModelRunRepository(session, vault_id)


class GovernedModelRuntime:
    """Execute one governed model task across three committed short transactions."""

    def __init__(
        self,
        *,
        sessions: AuthorizedVaultSessionOpener,
        gateway: GovernedModelGateway,
        fingerprints: ModelRunFingerprintFactory,
        result_persister: ModelResultPersister,
        receipt_factory: ModelRunReceiptFactory = _default_receipt_factory,
        finalize_grace: timedelta = timedelta(seconds=5),
    ) -> None:
        if finalize_grace <= timedelta(0):
            raise ValueError("model runtime finalize grace must be positive")
        self._sessions = sessions
        self._gateway = gateway
        self._fingerprints = fingerprints
        self._result_persister = result_persister
        self._receipt_factory = receipt_factory
        self._finalize_grace = finalize_grace

    async def run(
        self,
        *,
        authorization: str | None,
        vault_id: uuid.UUID,
        task_type: str,
        fragment_ids: Iterable[uuid.UUID],
        idempotency_key: str,
    ) -> ModelRunArtifactRef:
        """Return only a durable artifact identity after an atomic successful commit."""

        principal = await self._sessions.authenticate(authorization)
        selected_fragments = tuple(fragment_ids)
        prepared, write, membership, replay = await self._prepare(
            principal=principal,
            vault_id=vault_id,
            task_type=task_type,
            fragment_ids=selected_fragments,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            replayed = self._resolve_replay(replay)
            if replayed is not None:
                return replayed
        ticket = await self._dispatch(
            principal=principal,
            membership=membership,
            prepared=prepared,
            run_id=write.run_id,
        )
        result = await self._invoke(prepared=prepared, ticket=ticket, principal=principal)
        return await self._finalize_success(
            principal=principal,
            membership=membership,
            prepared=prepared,
            ticket=ticket,
            result=result,
        )

    async def _prepare(
        self,
        *,
        principal: AuthenticatedPrincipal,
        vault_id: uuid.UUID,
        task_type: str,
        fragment_ids: tuple[uuid.UUID, ...],
        idempotency_key: str,
    ) -> tuple[
        PreparedModelInvocation,
        ModelRunWrite,
        AuthorizedVaultContext,
        ModelRunProjection | None,
    ]:
        async with self._sessions.open_for_principal(
            principal=principal,
            vault_id=vault_id,
        ) as authorized:
            prepared = await authorized.session.run_sync(
                lambda session: self._gateway.prepare(
                    session=session,
                    vault_id=vault_id,
                    task_type=task_type,
                    fragment_ids=fragment_ids,
                )
            )
            receipt_spec, input_specs = self._receipt_contracts(
                prepared=prepared,
                idempotency_key=idempotency_key,
            )
            repository = self._receipt_factory(authorized.session, vault_id)
            write = await repository.prepare(receipt_spec, input_specs)
            replay = None
            if not write.created:
                replay = await repository.get_projection(write.run_id)
                if replay is None:
                    raise ModelRunDispatchConflict("existing model run projection is unavailable")
            return prepared, write, authorized.context, replay

    @staticmethod
    def _resolve_replay(projection: ModelRunProjection) -> ModelRunArtifactRef | None:
        if projection.state is ModelRunState.AUTHORIZED:
            return None
        if projection.state is ModelRunState.SUCCEEDED:
            if projection.artifact is None:
                raise ModelRunReplayArtifactMissing(projection.run_id)
            return projection.artifact
        if projection.state is ModelRunState.DISPATCHING:
            raise ModelRunReplayInProgress(projection.run_id)
        raise ModelRunReplayTerminal(projection.run_id, projection.state)

    async def _dispatch(
        self,
        *,
        principal: AuthenticatedPrincipal,
        membership: AuthorizedVaultContext,
        prepared: PreparedModelInvocation,
        run_id: uuid.UUID,
    ) -> ModelRunDispatchTicket:
        denied = False
        ticket: ModelRunDispatchTicket | None = None
        async with self._sessions.open_for_principal(
            principal=principal,
            vault_id=membership.vault_id,
            expected_membership_generation=membership.membership_generation,
        ) as authorized:
            repository = self._receipt_factory(authorized.session, membership.vault_id)
            try:
                await authorized.session.run_sync(
                    lambda session: self._gateway.assert_current(
                        session=session,
                        prepared=prepared,
                    )
                )
            except (ModelInvocationDenied, SourceAuthorityUnavailable):
                if not await repository.mark_denied(
                    run_id,
                    safe_error_code="authorization.changed_before_dispatch",
                ):
                    raise ModelRunDispatchConflict(
                        "model run changed before authorization denial"
                    ) from None
                denied = True
            else:
                ticket = await repository.claim_dispatch(
                    run_id,
                    lease_for=timedelta(milliseconds=prepared.task.latency_budget_ms)
                    + self._finalize_grace,
                )
                if ticket is None:
                    raise ModelRunDispatchConflict("model run is not dispatchable")
        if denied:
            raise ModelInvocationDenied("Source authorization changed before model dispatch")
        if ticket is None:  # pragma: no cover - fail-closed guard for future edits
            raise ModelRunDispatchConflict("model run dispatch ticket is unavailable")
        return ticket

    async def _invoke(
        self,
        *,
        prepared: PreparedModelInvocation,
        ticket: ModelRunDispatchTicket,
        principal: AuthenticatedPrincipal,
    ) -> BaseModel:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._gateway.invoke,
                    prepared=prepared,
                    run_id=ticket.run_id,
                ),
                timeout=prepared.task.latency_budget_ms / 1_000,
            )
        except asyncio.CancelledError:
            # Lease recovery is the final fail-safe if cancellation races the
            # bookkeeping transaction.
            with suppress(Exception):
                await asyncio.shield(
                    self._settle_unknown(
                        principal=principal,
                        prepared=prepared,
                        ticket=ticket,
                        error_code="caller.canceled_after_dispatch",
                    )
                )
            raise
        except TimeoutError:
            await self._settle_unknown(
                principal=principal,
                prepared=prepared,
                ticket=ticket,
                error_code="provider.timeout",
            )
            raise ModelRunTimeout("model provider outcome is unknown after timeout") from None
        except ProviderUnavailableBeforeDispatch:
            await self._settle_failed(
                principal=principal,
                prepared=prepared,
                ticket=ticket,
                error_code="provider.unavailable_before_dispatch",
            )
            raise ModelRunProviderUnavailable(
                "model provider was unavailable before dispatch"
            ) from None
        except ProviderExecutionError:
            await self._settle_unknown(
                principal=principal,
                prepared=prepared,
                ticket=ticket,
                error_code="provider.execution_unknown",
            )
            raise ModelRunProviderOutcomeUnknown(
                "model provider outcome could not be determined"
            ) from None
        except StructuredOutputValidationError:
            await self._settle_failed(
                principal=principal,
                prepared=prepared,
                ticket=ticket,
                error_code="output.schema_rejected",
            )
            raise
        except ToolDirectiveRejected:
            await self._settle_failed(
                principal=principal,
                prepared=prepared,
                ticket=ticket,
                error_code="output.tool_directive_rejected",
            )
            raise
        except (GatewayConfigurationError, ModelPolicyViolation, ProviderOutputDecodeError):
            await self._settle_failed(
                principal=principal,
                prepared=prepared,
                ticket=ticket,
                error_code="gateway.deterministic_rejection",
            )
            raise
        except Exception:
            await self._settle_unknown(
                principal=principal,
                prepared=prepared,
                ticket=ticket,
                error_code="runtime.execution_unknown",
            )
            raise ModelRunProviderOutcomeUnknown(
                "model runtime outcome could not be determined"
            ) from None

    async def _settle_failed(
        self,
        *,
        principal: AuthenticatedPrincipal,
        prepared: PreparedModelInvocation,
        ticket: ModelRunDispatchTicket,
        error_code: str,
    ) -> None:
        await self._settle_provider_outcome(
            principal=principal,
            prepared=prepared,
            ticket=ticket,
            error_code=error_code,
            unknown=False,
        )

    async def _settle_unknown(
        self,
        *,
        principal: AuthenticatedPrincipal,
        prepared: PreparedModelInvocation,
        ticket: ModelRunDispatchTicket,
        error_code: str,
    ) -> None:
        await self._settle_provider_outcome(
            principal=principal,
            prepared=prepared,
            ticket=ticket,
            error_code=error_code,
            unknown=True,
        )

    async def _settle_provider_outcome(
        self,
        *,
        principal: AuthenticatedPrincipal,
        prepared: PreparedModelInvocation,
        ticket: ModelRunDispatchTicket,
        error_code: str,
        unknown: bool,
    ) -> None:
        snapshot = prepared.snapshot
        async with self._sessions.open_for_model_run_bookkeeping(
            vault_id=snapshot.vault.vault_id,
        ) as bookkeeper:
            if unknown:
                updated = await bookkeeper.mark_unknown(
                    ticket,
                    safe_error_code=error_code,
                )
            else:
                updated = await bookkeeper.mark_failed(
                    ticket,
                    safe_error_code=error_code,
                )
            if not updated:
                raise ModelRunFinalizationConflict(
                    "model run provider outcome could not be finalized"
                )

    async def _finalize_success(
        self,
        *,
        principal: AuthenticatedPrincipal,
        membership: AuthorizedVaultContext,
        prepared: PreparedModelInvocation,
        ticket: ModelRunDispatchTicket,
        result: BaseModel,
    ) -> ModelRunArtifactRef:
        authority_changed = False
        result_rejected = False
        artifact_ref: ModelRunArtifactRef | None = None
        try:
            async with self._sessions.open_for_principal(
                principal=principal,
                vault_id=membership.vault_id,
                expected_membership_generation=membership.membership_generation,
            ) as authorized:
                repository = self._receipt_factory(authorized.session, membership.vault_id)
                try:
                    await authorized.session.run_sync(
                        lambda session: self._gateway.assert_current(
                            session=session,
                            prepared=prepared,
                        )
                    )
                except (ModelInvocationDenied, SourceAuthorityUnavailable):
                    if not await repository.mark_failed(
                        ticket,
                        safe_error_code="authorization.changed_after_dispatch",
                    ):
                        raise ModelRunFinalizationConflict(
                            "model run changed before result authorization rejection"
                        ) from None
                    authority_changed = True
                else:
                    persistence_failed = False
                    try:
                        artifact = await self._result_persister.persist(
                            authorized.session,
                            context=ModelResultContext(
                                run_id=ticket.run_id,
                                vault_id=membership.vault_id,
                                principal_id=membership.principal_id,
                                membership_generation=membership.membership_generation,
                                prepared=prepared,
                            ),
                            result=result,
                        )
                        if artifact is None:
                            raise ModelRunResultPersistenceError(
                                "model result sink returned no durable artifact"
                            )
                        artifact_write = await repository.attach_artifact(ticket, artifact)
                        artifact_ref = artifact_write.artifact
                    except ModelResultRejected:
                        if not await repository.mark_failed(
                            ticket,
                            safe_error_code="output.persistence_rejected",
                        ):
                            raise ModelRunFinalizationConflict(
                                "model run changed before result rejection"
                            ) from None
                        result_rejected = True
                    except Exception:
                        persistence_failed = True
                    if persistence_failed:
                        raise ModelRunResultPersistenceError(
                            "model result could not be persisted"
                        ) from None
                    if not result_rejected and not await repository.mark_succeeded(ticket):
                        raise ModelRunFinalizationConflict(
                            "model run changed before result finalization"
                        )
        except (AuthenticationDenied, VaultMembershipDenied):
            await self._settle_failed(
                principal=principal,
                prepared=prepared,
                ticket=ticket,
                error_code="authorization.changed_after_dispatch",
            )
            authority_changed = True
        if authority_changed:
            raise ModelRunResultDiscarded(
                "model result was discarded because authorization changed"
            )
        if result_rejected:
            raise ModelRunResultDiscarded("model result was rejected by the durable sink")
        if artifact_ref is None:  # pragma: no cover - fail-closed future edit guard
            raise ModelRunResultPersistenceError("model result durable artifact is unavailable")
        return artifact_ref

    def _receipt_contracts(
        self,
        *,
        prepared: PreparedModelInvocation,
        idempotency_key: str,
    ) -> tuple[ModelRunReceiptSpec, tuple[ModelRunInputSpec, ...]]:
        snapshot = prepared.snapshot
        vault_id = snapshot.vault.vault_id
        inputs = tuple(
            ModelRunInputSpec(
                vault_id=vault_id,
                kind=prepared.input_refs[ordinal].kind,
                object_id=fragment.fragment_id,
                content_fingerprint=self._fingerprints.input_content(
                    vault_id=vault_id,
                    fragment_id=fragment.fragment_id,
                    text_hash=fragment.text_hash,
                ),
                ordinal=ordinal,
            )
            for ordinal, fragment in enumerate(snapshot.fragments)
        )
        task_hash = self._fingerprints.task_definition(
            vault_id=vault_id,
            prepared=prepared,
        )
        request_hash = self._fingerprints.request(
            vault_id=vault_id,
            idempotency_key=idempotency_key,
            prepared=prepared,
            task_definition_hash=task_hash,
            inputs=inputs,
        )
        task = prepared.task
        return (
            ModelRunReceiptSpec(
                vault_id=vault_id,
                task_type=task.task_type,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                task_definition_hash=task_hash,
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
            ),
            inputs,
        )


__all__ = [
    "AuthorizedVaultSessionOpener",
    "GovernedModelRuntime",
    "ModelResultContext",
    "ModelResultPersister",
    "ModelResultRejected",
    "ModelRunDispatchConflict",
    "ModelRunFinalizationConflict",
    "ModelRunFingerprintFactory",
    "ModelRunOutcomeBookkeeper",
    "ModelRunProviderOutcomeUnknown",
    "ModelRunProviderUnavailable",
    "ModelRunReplayArtifactMissing",
    "ModelRunReplayInProgress",
    "ModelRunReplayTerminal",
    "ModelRunResultDiscarded",
    "ModelRunResultPersistenceError",
    "ModelRunTimeout",
    "ModelRuntimeError",
]
