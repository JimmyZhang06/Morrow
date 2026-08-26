"""Vault-scoped CAS repository for content-free governed-model receipts.

Repositories never commit. The caller controls each short transaction around
prepare, dispatch authorization, or finalization; provider I/O must happen
between those transactions rather than through this class.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import Interval, String, Uuid, bindparam, exists, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from life_coach.jobs.payloads import validate_provider_identifier, validate_routing_name
from life_coach.modules.model_runs.contracts import (
    CrossVaultModelRunError,
    ModelRunArtifactConflict,
    ModelRunArtifactKind,
    ModelRunArtifactRef,
    ModelRunArtifactSpec,
    ModelRunArtifactWrite,
    ModelRunDispatchTicket,
    ModelRunIdempotencyConflict,
    ModelRunInputSpec,
    ModelRunProjection,
    ModelRunReceiptSpec,
    ModelRunWrite,
)
from life_coach.modules.model_runs.models import (
    ModelRun,
    ModelRunArtifact,
    ModelRunInput,
    ModelRunState,
)

_EXPIRED_DISPATCH_ERROR = "dispatch.expired"


def claim_model_run_statement() -> Update:
    """Claim exactly one authorized receipt using database-clock lease fencing."""

    return (
        update(ModelRun)
        .where(
            ModelRun.id == bindparam("p_run_id", type_=Uuid(as_uuid=True)),
            ModelRun.vault_id == bindparam("p_vault_id", type_=Uuid(as_uuid=True)),
            ModelRun.state == ModelRunState.AUTHORIZED,
        )
        .values(
            state=ModelRunState.DISPATCHING,
            attempt=ModelRun.attempt + 1,
            dispatch_generation=ModelRun.dispatch_generation + 1,
            dispatch_started_at=func.clock_timestamp(),
            dispatch_expires_at=func.clock_timestamp() + bindparam("lease_for", type_=Interval()),
            provider_request_id=None,
            safe_error_code=None,
        )
        .returning(ModelRun.id, ModelRun.vault_id, ModelRun.dispatch_generation)
    )


def finalize_model_run_statement(target: ModelRunState) -> Update:
    """Build a generation-exact terminal CAS for one provider outcome."""

    if target not in {
        ModelRunState.SUCCEEDED,
        ModelRunState.FAILED,
        ModelRunState.UNKNOWN,
    }:
        raise ValueError("model run I/O finalization requires an I/O terminal state")
    return (
        update(ModelRun)
        .where(
            ModelRun.id == bindparam("p_run_id", type_=Uuid(as_uuid=True)),
            ModelRun.vault_id == bindparam("p_vault_id", type_=Uuid(as_uuid=True)),
            ModelRun.state == ModelRunState.DISPATCHING,
            ModelRun.dispatch_generation == bindparam("p_dispatch_generation"),
        )
        .values(
            state=target,
            provider_request_id=bindparam("p_provider_request_id", type_=String()),
            safe_error_code=bindparam("p_safe_error_code", type_=String()),
            io_finished_at=func.clock_timestamp(),
            completed_at=func.clock_timestamp(),
        )
        .returning(ModelRun.id)
    )


def recover_expired_model_runs_statement() -> Update:
    """Conservatively mark an expired in-flight call unknown; never redispatch it."""

    return (
        update(ModelRun)
        .where(
            ModelRun.vault_id == bindparam("p_vault_id", type_=Uuid(as_uuid=True)),
            ModelRun.state == ModelRunState.DISPATCHING,
            ModelRun.dispatch_expires_at.is_not(None),
            ModelRun.dispatch_expires_at <= func.clock_timestamp(),
        )
        .values(
            state=ModelRunState.UNKNOWN,
            safe_error_code=_EXPIRED_DISPATCH_ERROR,
            io_finished_at=func.clock_timestamp(),
            completed_at=func.clock_timestamp(),
        )
        .returning(ModelRun.id)
    )


def _immutable_binding(spec: ModelRunReceiptSpec) -> tuple[object, ...]:
    return (
        spec.task_definition_hash,
        spec.provider,
        spec.model,
        spec.model_revision,
        spec.prompt_template_version,
        spec.schema_version,
        spec.pipeline_version,
        spec.consent_snapshot_id,
        spec.policy_epoch,
        spec.source_generation,
        spec.actual_sensitivity,
        spec.data_residency,
        spec.retention_policy,
    )


def _stored_binding(mapping: RowMapping) -> tuple[object, ...]:
    return (
        mapping["task_definition_hash"],
        mapping["provider"],
        mapping["model"],
        mapping["model_revision"],
        mapping["prompt_template_version"],
        mapping["schema_version"],
        mapping["pipeline_version"],
        mapping["consent_snapshot_id"],
        mapping["policy_epoch"],
        mapping["source_generation"],
        mapping["actual_sensitivity"],
        mapping["data_residency"],
        mapping["retention_policy"],
    )


def _ordered_inputs(
    inputs: Sequence[ModelRunInputSpec],
    *,
    vault_id: uuid.UUID,
) -> tuple[ModelRunInputSpec, ...]:
    normalized = tuple(sorted(inputs, key=lambda item: item.ordinal))
    if any(item.vault_id != vault_id for item in normalized):
        raise CrossVaultModelRunError("model run input belongs to another vault")
    if tuple(item.ordinal for item in normalized) != tuple(range(len(normalized))):
        raise ValueError("model run input ordinals must be contiguous from zero")
    identities = {(item.kind, item.object_id) for item in normalized}
    if len(identities) != len(normalized):
        raise ValueError("model run inputs must not repeat an object reference")
    return normalized


def _input_binding(input_spec: ModelRunInputSpec) -> tuple[object, ...]:
    return (
        input_spec.kind,
        input_spec.object_id,
        str(input_spec.content_fingerprint),
        input_spec.ordinal,
    )


class ModelRunRepository:
    """Advance model receipts only inside one caller-owned Vault transaction."""

    def __init__(self, session: AsyncSession, vault_id: uuid.UUID) -> None:
        self._session = session
        self.vault_id = vault_id

    def _require_vault(self, vault_id: uuid.UUID) -> None:
        if vault_id != self.vault_id:
            raise CrossVaultModelRunError("model run belongs to another vault")

    async def prepare(
        self,
        spec: ModelRunReceiptSpec,
        inputs: Sequence[ModelRunInputSpec],
    ) -> ModelRunWrite:
        """Create an authorized receipt or return an identical scoped replay."""

        self._require_vault(spec.vault_id)
        ordered_inputs = _ordered_inputs(inputs, vault_id=self.vault_id)
        run_id = uuid.uuid4()
        insert = (
            pg_insert(ModelRun)
            .values(
                id=run_id,
                vault_id=spec.vault_id,
                task_type=spec.task_type,
                idempotency_key=spec.idempotency_key,
                request_hash=spec.request_hash,
                task_definition_hash=spec.task_definition_hash,
                provider=spec.provider,
                model=spec.model,
                model_revision=spec.model_revision,
                prompt_template_version=spec.prompt_template_version,
                schema_version=spec.schema_version,
                pipeline_version=spec.pipeline_version,
                consent_snapshot_id=spec.consent_snapshot_id,
                policy_epoch=spec.policy_epoch,
                source_generation=spec.source_generation,
                actual_sensitivity=spec.actual_sensitivity,
                data_residency=spec.data_residency,
                retention_policy=spec.retention_policy,
                state=ModelRunState.AUTHORIZED,
                attempt=0,
                dispatch_generation=0,
                authorized_at=func.clock_timestamp(),
            )
            .on_conflict_do_nothing(
                index_elements=[ModelRun.vault_id, ModelRun.task_type, ModelRun.idempotency_key]
            )
            .returning(ModelRun.id)
        )
        inserted = (await self._session.execute(insert)).scalar_one_or_none()
        if inserted is not None:
            if ordered_inputs:
                await self._session.execute(
                    pg_insert(ModelRunInput).values(
                        [
                            {
                                "id": uuid.uuid4(),
                                "vault_id": self.vault_id,
                                "model_run_id": inserted,
                                "kind": item.kind,
                                "object_id": item.object_id,
                                "content_fingerprint": item.content_fingerprint,
                                "ordinal": item.ordinal,
                            }
                            for item in ordered_inputs
                        ]
                    )
                )
            return ModelRunWrite(run_id=inserted, created=True)

        existing_result = await self._session.execute(
            select(
                ModelRun.id,
                ModelRun.request_hash,
                ModelRun.task_definition_hash,
                ModelRun.provider,
                ModelRun.model,
                ModelRun.model_revision,
                ModelRun.prompt_template_version,
                ModelRun.schema_version,
                ModelRun.pipeline_version,
                ModelRun.consent_snapshot_id,
                ModelRun.policy_epoch,
                ModelRun.source_generation,
                ModelRun.actual_sensitivity,
                ModelRun.data_residency,
                ModelRun.retention_policy,
            ).where(
                ModelRun.vault_id == self.vault_id,
                ModelRun.task_type == spec.task_type,
                ModelRun.idempotency_key == spec.idempotency_key,
            )
        )
        existing = existing_result.mappings().one_or_none()
        if existing is None:
            raise ModelRunIdempotencyConflict(
                "model run conflicts with an existing uniqueness scope"
            )
        if not secrets.compare_digest(existing["request_hash"], spec.request_hash):
            raise ModelRunIdempotencyConflict(
                "model run idempotency key was reused with a different request"
            )
        if _stored_binding(existing) != _immutable_binding(spec):
            raise ModelRunIdempotencyConflict(
                "model run idempotency scope has a different immutable binding"
            )
        stored_input_result = await self._session.execute(
            select(
                ModelRunInput.kind,
                ModelRunInput.object_id,
                ModelRunInput.content_fingerprint,
                ModelRunInput.ordinal,
            )
            .where(
                ModelRunInput.vault_id == self.vault_id,
                ModelRunInput.model_run_id == existing["id"],
            )
            .order_by(ModelRunInput.ordinal)
        )
        stored_inputs = tuple(
            (row.kind, row.object_id, row.content_fingerprint, row.ordinal)
            for row in stored_input_result
        )
        expected_inputs = tuple(_input_binding(item) for item in ordered_inputs)
        if stored_inputs != expected_inputs:
            raise ModelRunIdempotencyConflict(
                "model run idempotency scope has different input references"
            )
        return ModelRunWrite(run_id=existing["id"], created=False)

    async def claim_dispatch(
        self,
        run_id: uuid.UUID,
        *,
        lease_for: timedelta,
    ) -> ModelRunDispatchTicket | None:
        """CAS authorized to dispatching; an expired dispatch is never reclaimed."""

        if lease_for <= timedelta(0):
            raise ValueError("model run dispatch lease must be positive")
        result = await self._session.execute(
            claim_model_run_statement(),
            {"p_run_id": run_id, "p_vault_id": self.vault_id, "lease_for": lease_for},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return ModelRunDispatchTicket(
            run_id=row["id"],
            vault_id=row["vault_id"],
            dispatch_generation=row["dispatch_generation"],
        )

    async def get_projection(self, run_id: uuid.UUID) -> ModelRunProjection | None:
        """Read one receipt and its optional durable artifact for replay handling."""

        result = await self._session.execute(
            select(
                ModelRun.id.label("run_id"),
                ModelRun.vault_id,
                ModelRun.state,
                ModelRun.attempt,
                ModelRun.dispatch_generation,
                ModelRun.provider_request_id,
                ModelRun.safe_error_code,
                ModelRunArtifact.id.label("artifact_id"),
                ModelRunArtifact.artifact_kind,
                ModelRunArtifact.derived_object_id,
                ModelRunArtifact.memory_claim_id,
                ModelRunArtifact.action_id,
                ModelRunArtifact.narrative_generation_id,
            )
            .outerjoin(
                ModelRunArtifact,
                (ModelRunArtifact.vault_id == ModelRun.vault_id)
                & (ModelRunArtifact.model_run_id == ModelRun.id),
            )
            .where(ModelRun.vault_id == self.vault_id, ModelRun.id == run_id)
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        artifact = (
            None
            if row["artifact_id"] is None
            else ModelRunArtifactRef(
                artifact_id=row["artifact_id"],
                vault_id=row["vault_id"],
                model_run_id=row["run_id"],
                derived_object_id=row["derived_object_id"],
                memory_claim_id=row["memory_claim_id"],
                action_id=row.get("action_id"),
                narrative_generation_id=row.get("narrative_generation_id"),
                artifact_kind=ModelRunArtifactKind(row.get("artifact_kind", "knowledge")),
            )
        )
        return ModelRunProjection(
            run_id=row["run_id"],
            vault_id=row["vault_id"],
            state=row["state"],
            attempt=row["attempt"],
            dispatch_generation=row["dispatch_generation"],
            provider_request_id=row["provider_request_id"],
            safe_error_code=row["safe_error_code"],
            artifact=artifact,
        )

    async def attach_artifact(
        self,
        ticket: ModelRunDispatchTicket,
        artifact: ModelRunArtifactSpec,
    ) -> ModelRunArtifactWrite:
        """Attach one governed artifact only to the exact in-flight generation."""

        self._require_vault(ticket.vault_id)
        self._require_vault(artifact.vault_id)
        artifact_id = uuid.uuid4()
        authorized_dispatch = exists(
            select(ModelRun.id).where(
                ModelRun.id == ticket.run_id,
                ModelRun.vault_id == self.vault_id,
                ModelRun.state == ModelRunState.DISPATCHING,
                ModelRun.dispatch_generation == ticket.dispatch_generation,
            )
        )
        insert = (
            pg_insert(ModelRunArtifact)
            .from_select(
                (
                    ModelRunArtifact.id,
                    ModelRunArtifact.vault_id,
                    ModelRunArtifact.model_run_id,
                    ModelRunArtifact.artifact_kind,
                    ModelRunArtifact.derived_object_id,
                    ModelRunArtifact.memory_claim_id,
                    ModelRunArtifact.action_id,
                    ModelRunArtifact.narrative_generation_id,
                    ModelRunArtifact.created_at,
                ),
                select(
                    literal(artifact_id),
                    literal(self.vault_id),
                    literal(ticket.run_id),
                    literal(artifact.artifact_kind.value),
                    literal(artifact.derived_object_id),
                    literal(artifact.memory_claim_id),
                    literal(artifact.action_id),
                    literal(artifact.narrative_generation_id),
                    func.clock_timestamp(),
                ).where(authorized_dispatch),
            )
            .on_conflict_do_nothing()
            .returning(
                ModelRunArtifact.id,
                ModelRunArtifact.vault_id,
                ModelRunArtifact.model_run_id,
                ModelRunArtifact.artifact_kind,
                ModelRunArtifact.derived_object_id,
                ModelRunArtifact.memory_claim_id,
                ModelRunArtifact.action_id,
                ModelRunArtifact.narrative_generation_id,
            )
        )
        inserted = (await self._session.execute(insert)).mappings().one_or_none()
        if inserted is not None:
            return ModelRunArtifactWrite(
                artifact=_artifact_ref(inserted),
                created=True,
            )

        existing_result = await self._session.execute(
            select(
                ModelRunArtifact.id,
                ModelRunArtifact.vault_id,
                ModelRunArtifact.model_run_id,
                ModelRunArtifact.artifact_kind,
                ModelRunArtifact.derived_object_id,
                ModelRunArtifact.memory_claim_id,
                ModelRunArtifact.action_id,
                ModelRunArtifact.narrative_generation_id,
            ).where(
                ModelRunArtifact.vault_id == self.vault_id,
                ModelRunArtifact.model_run_id == ticket.run_id,
            )
        )
        existing = existing_result.mappings().one_or_none()
        if existing is None or (
            existing.get("artifact_kind", "knowledge"),
            existing["derived_object_id"],
            existing["memory_claim_id"],
            existing.get("action_id"),
            existing.get("narrative_generation_id"),
        ) != (
            artifact.artifact_kind.value,
            artifact.derived_object_id,
            artifact.memory_claim_id,
            artifact.action_id,
            artifact.narrative_generation_id,
        ):
            raise ModelRunArtifactConflict("model run artifact lineage is unavailable")
        return ModelRunArtifactWrite(artifact=_artifact_ref(existing), created=False)

    async def mark_succeeded(
        self,
        ticket: ModelRunDispatchTicket,
        *,
        provider_request_id: str | None = None,
    ) -> bool:
        return await self._finalize(
            ticket,
            target=ModelRunState.SUCCEEDED,
            safe_error_code=None,
            provider_request_id=provider_request_id,
        )

    async def mark_failed(
        self,
        ticket: ModelRunDispatchTicket,
        *,
        safe_error_code: str,
        provider_request_id: str | None = None,
    ) -> bool:
        return await self._finalize(
            ticket,
            target=ModelRunState.FAILED,
            safe_error_code=safe_error_code,
            provider_request_id=provider_request_id,
        )

    async def mark_unknown(
        self,
        ticket: ModelRunDispatchTicket,
        *,
        safe_error_code: str,
        provider_request_id: str | None = None,
    ) -> bool:
        return await self._finalize(
            ticket,
            target=ModelRunState.UNKNOWN,
            safe_error_code=safe_error_code,
            provider_request_id=provider_request_id,
        )

    async def _finalize(
        self,
        ticket: ModelRunDispatchTicket,
        *,
        target: ModelRunState,
        safe_error_code: str | None,
        provider_request_id: str | None,
    ) -> bool:
        self._require_vault(ticket.vault_id)
        if safe_error_code is not None:
            validate_routing_name(safe_error_code)
        if provider_request_id is not None:
            validate_provider_identifier(provider_request_id)
        result = await self._session.execute(
            finalize_model_run_statement(target),
            {
                "p_run_id": ticket.run_id,
                "p_vault_id": ticket.vault_id,
                "p_dispatch_generation": ticket.dispatch_generation,
                "p_provider_request_id": provider_request_id,
                "p_safe_error_code": safe_error_code,
            },
        )
        return result.scalar_one_or_none() is not None

    async def mark_denied(self, run_id: uuid.UUID, *, safe_error_code: str) -> bool:
        return await self._mark_pre_dispatch_terminal(
            run_id,
            target=ModelRunState.DENIED,
            safe_error_code=safe_error_code,
        )

    async def mark_canceled(self, run_id: uuid.UUID, *, safe_error_code: str) -> bool:
        return await self._mark_pre_dispatch_terminal(
            run_id,
            target=ModelRunState.CANCELED,
            safe_error_code=safe_error_code,
        )

    async def _mark_pre_dispatch_terminal(
        self,
        run_id: uuid.UUID,
        *,
        target: ModelRunState,
        safe_error_code: str,
    ) -> bool:
        validate_routing_name(safe_error_code)
        result = await self._session.execute(
            update(ModelRun)
            .where(
                ModelRun.id == run_id,
                ModelRun.vault_id == self.vault_id,
                ModelRun.state == ModelRunState.AUTHORIZED,
            )
            .values(
                state=target,
                safe_error_code=safe_error_code,
                completed_at=func.clock_timestamp(),
            )
            .returning(ModelRun.id)
        )
        return result.scalar_one_or_none() is not None

    async def recover_expired_dispatches(self) -> tuple[uuid.UUID, ...]:
        result = await self._session.execute(
            recover_expired_model_runs_statement(),
            {"p_vault_id": self.vault_id},
        )
        return tuple(result.scalars())


def _artifact_ref(mapping: RowMapping) -> ModelRunArtifactRef:
    return ModelRunArtifactRef(
        artifact_id=mapping["id"],
        vault_id=mapping["vault_id"],
        model_run_id=mapping["model_run_id"],
        derived_object_id=mapping["derived_object_id"],
        memory_claim_id=mapping["memory_claim_id"],
        action_id=mapping.get("action_id"),
        narrative_generation_id=mapping.get("narrative_generation_id"),
        artifact_kind=ModelRunArtifactKind(mapping.get("artifact_kind", "knowledge")),
    )


__all__ = [
    "ModelRunRepository",
    "claim_model_run_statement",
    "finalize_model_run_statement",
    "recover_expired_model_runs_statement",
]
