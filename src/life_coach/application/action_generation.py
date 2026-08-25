"""Governed AI generation for one user-approved reversible micro-action."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Annotated, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from life_coach.ai.contracts import ModelInputKind, ModelInputRef
from life_coach.ai.provider import ModelGatewayError
from life_coach.application.model_gateway import (
    ModelInvocationDenied,
    ModelTaskContextSnapshot,
)
from life_coach.application.model_runtime import (
    ModelResultContext,
    ModelResultRejected,
    ModelRuntimeError,
)
from life_coach.modules.action.lifecycle import (
    ActionAuthenticationRequiredError,
    ActionGenerationUnavailableError,
    ActionVaultUnavailableError,
    MemoryNotEligibleForActionError,
    ReversibleActionView,
)
from life_coach.modules.action.service import (
    AsyncReversibleActionService,
    ReversibleActionService,
)
from life_coach.modules.knowledge.enums import (
    ClaimVersionOrigin,
    EvidenceRelation,
    LifecycleState,
    VerdictType,
)
from life_coach.modules.knowledge.models import (
    ClaimVersion,
    DerivedObject,
    EvidenceLink,
    MemoryClaim,
    UserVerdict,
)
from life_coach.modules.knowledge.reducer import reduce_verdicts
from life_coach.modules.model_runs.contracts import (
    ModelRunArtifactKind,
    ModelRunArtifactRef,
    ModelRunArtifactSpec,
)
from life_coach.platform.auth import (
    AuthenticationDenied,
    ProductionSessionFactory,
    VaultMembershipDenied,
)
from life_coach.platform.database import VaultAsyncSession

REVERSIBLE_ACTION_TASK_TYPE = "reversible_action"
ACTION_PROMPT_TEMPLATE_VERSION = "reversible-action-v1"
ACTION_TEMPLATE_VERSION = "ai-micro-action-v1"

_Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
_Description = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=320),
]
_Rationale = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=240),
]
_ExitPlan = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=240),
]


class ReversibleActionOutput(BaseModel):
    """Only the bounded copy and duration may be supplied by the model."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    title: _Title
    description: _Description
    rationale: _Rationale
    exit_plan: _ExitPlan
    estimated_minutes: int = Field(ge=1, le=15)

    @model_validator(mode="after")
    def reject_external_side_effects(self) -> ReversibleActionOutput:
        instruction = f"{self.title} {self.description}".casefold()
        for negated in (
            "不发送",
            "暂不发送",
            "不要发送",
            "无需发送",
            "不联系",
            "不要联系",
            "不购买",
            "不发布",
            "不上传",
            "do not send",
            "don't send",
            "without sending",
            "do not contact",
            "without contacting",
        ):
            instruction = instruction.replace(negated, "")
        forbidden_phrases = (
            "发送给",
            "发给",
            "拿给同事看",
            "联系他人",
            "联系对方",
            "购买",
            "付款",
            "下单",
            "发布到",
            "上传到",
            "注册账号",
            "创建日历",
            "预约",
            "邀请他人",
        )
        forbidden_words = re.compile(
            r"\b(send|email|message|contact|call|buy|purchase|publish|post|upload|"
            r"book|schedule|invite|register)\b"
        )
        if any(phrase in instruction for phrase in forbidden_phrases) or forbidden_words.search(
            instruction
        ):
            raise ValueError("reversible action cannot require an external side effect")
        return self


class ReversibleActionRuntime(Protocol):
    async def run(
        self,
        *,
        authorization: str | None,
        vault_id: uuid.UUID,
        task_type: str,
        fragment_ids: tuple[uuid.UUID, ...],
        idempotency_key: str,
        context_id: uuid.UUID | None = None,
    ) -> ModelRunArtifactRef: ...


class GovernedReversibleActionCreator:
    """HTTP-facing adapter that performs provider I/O outside request transactions."""

    def __init__(
        self,
        *,
        sessions: ProductionSessionFactory,
        runtime: ReversibleActionRuntime,
        authorization: str | None,
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime
        self._authorization = authorization

    async def create_for_memory(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        idempotency_key: uuid.UUID,
    ) -> ReversibleActionView:
        try:
            artifact = await self._runtime.run(
                authorization=self._authorization,
                vault_id=vault_id,
                task_type=REVERSIBLE_ACTION_TASK_TYPE,
                fragment_ids=(),
                idempotency_key=str(idempotency_key),
                context_id=memory_id,
            )
        except AuthenticationDenied:
            raise ActionAuthenticationRequiredError("action authentication is required") from None
        except VaultMembershipDenied:
            raise ActionVaultUnavailableError("action Vault is unavailable") from None
        except ModelInvocationDenied:
            raise MemoryNotEligibleForActionError(
                "the current Memory is unavailable or not eligible"
            ) from None
        except (ModelGatewayError, ModelRuntimeError):
            raise ActionGenerationUnavailableError(
                "the action model result is unavailable"
            ) from None
        if artifact.artifact_kind is not ModelRunArtifactKind.ACTION or artifact.action_id is None:
            raise ActionGenerationUnavailableError("the action artifact is unavailable")
        try:
            async with self._sessions.open(
                authorization=self._authorization,
                vault_id=vault_id,
            ) as authorized:
                return await AsyncReversibleActionService(authorized.session).get(
                    vault_id=vault_id,
                    action_id=artifact.action_id,
                )
        except AuthenticationDenied:
            raise ActionAuthenticationRequiredError("action authentication is required") from None
        except VaultMembershipDenied:
            raise ActionVaultUnavailableError("action Vault is unavailable") from None


class ActionMemoryContextAuthority:
    """Bind the latest confirmed/corrected Memory and its live Source evidence."""

    def prepare(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        context_id: uuid.UUID,
    ) -> ModelTaskContextSnapshot:
        claim = session.scalar(
            select(MemoryClaim).where(
                MemoryClaim.vault_id == vault_id,
                MemoryClaim.id == context_id,
                MemoryClaim.deleted_at.is_(None),
            )
        )
        if claim is None:
            raise ModelInvocationDenied("action Memory is unavailable")
        version = session.scalar(
            select(ClaimVersion)
            .join(
                DerivedObject,
                (DerivedObject.vault_id == ClaimVersion.vault_id)
                & (DerivedObject.id == ClaimVersion.derived_object_id),
            )
            .where(
                ClaimVersion.vault_id == vault_id,
                ClaimVersion.claim_id == claim.id,
                ClaimVersion.system_to.is_(None),
                DerivedObject.deleted_at.is_(None),
            )
        )
        if version is None or version.lifecycle_state not in {
            LifecycleState.CANDIDATE,
            LifecycleState.ACTIVE,
        }:
            raise ModelInvocationDenied("action Memory is unavailable")
        events = session.scalars(
            select(UserVerdict).where(
                UserVerdict.vault_id == vault_id,
                UserVerdict.target_derived_object_id == version.derived_object_id,
            )
        ).all()
        reduced = reduce_verdicts(version.initial_lifecycle_state, events, can_activate=False)
        confirmed = reduced.last_decisive_verdict is VerdictType.CONFIRM
        corrected = (
            version.origin is ClaimVersionOrigin.USER_CORRECTION
            and version.origin_verdict_id is not None
        )
        if not (confirmed or corrected):
            raise ModelInvocationDenied("action Memory has not been approved")

        fragment_ids = tuple(
            dict.fromkeys(
                session.scalars(
                    select(EvidenceLink.source_fragment_id)
                    .where(
                        EvidenceLink.vault_id == vault_id,
                        EvidenceLink.target_derived_object_id == version.derived_object_id,
                        EvidenceLink.relation == EvidenceRelation.SUPPORTS,
                        EvidenceLink.deleted_at.is_(None),
                        EvidenceLink.invalidated_reason.is_(None),
                    )
                    .order_by(EvidenceLink.created_at, EvidenceLink.id)
                ).all()
            )
        )
        if not fragment_ids:
            raise ModelInvocationDenied("action Memory evidence is unavailable")
        data = {
            "memory_kind": claim.kind.value,
            "memory_statement": version.canonical_text,
            "memory_version": version.version_no,
        }
        canonical = json.dumps(
            {
                "derived_object_id": str(version.derived_object_id),
                "fragment_ids": [str(value) for value in fragment_ids],
                "memory_id": str(claim.id),
                **data,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return ModelTaskContextSnapshot(
            context_id=claim.id,
            input_ref=ModelInputRef(
                vault_id=str(vault_id),
                kind=ModelInputKind.DERIVED_OBJECT,
                object_id=str(version.derived_object_id),
            ),
            content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            source_fragment_ids=fragment_ids,
            data=cast(JsonValue, data),
        )

    def assert_current(
        self,
        *,
        session: Session,
        snapshot: ModelTaskContextSnapshot,
    ) -> None:
        try:
            current = self.prepare(
                session=session,
                vault_id=uuid.UUID(snapshot.input_ref.vault_id),
                context_id=snapshot.context_id,
            )
        except (ValueError, ModelInvocationDenied):
            raise ModelInvocationDenied("action Memory changed during generation") from None
        if current != snapshot:
            raise ModelInvocationDenied("action Memory changed during generation")


class ReversibleActionPersister:
    """Persist a model draft while reusing the deterministic action state machine."""

    async def persist(
        self,
        session: VaultAsyncSession,
        *,
        context: ModelResultContext,
        result: BaseModel,
    ) -> ModelRunArtifactSpec:
        if (
            type(result) is not ReversibleActionOutput
            or context.prepared.task.task_type != REVERSIBLE_ACTION_TASK_TYPE
            or context.prepared.task.output_type is not ReversibleActionOutput
            or context.prepared.context_snapshot is None
        ):
            raise ModelResultRejected("action result binding is unavailable")
        output = result
        try:
            command_key = uuid.UUID(context.idempotency_key)
        except (TypeError, ValueError, AttributeError):
            raise ModelResultRejected("action idempotency binding is unavailable") from None
        memory_id = context.prepared.context_snapshot.context_id
        try:
            action = await session.run_sync(
                lambda sync_session: ReversibleActionService(
                    sync_session
                ).create_generated_for_memory(
                    vault_id=context.vault_id,
                    memory_id=memory_id,
                    model_run_id=context.run_id,
                    idempotency_key=command_key,
                    template_version=ACTION_TEMPLATE_VERSION,
                    title=output.title,
                    description=output.description,
                    rationale=output.rationale,
                    exit_plan=output.exit_plan,
                    estimated_minutes=output.estimated_minutes,
                )
            )
        except MemoryNotEligibleForActionError:
            raise ModelResultRejected("action Memory is no longer eligible") from None
        return ModelRunArtifactSpec(
            vault_id=context.vault_id,
            artifact_kind=ModelRunArtifactKind.ACTION,
            action_id=action.action_id,
        )


__all__ = [
    "ACTION_PROMPT_TEMPLATE_VERSION",
    "ACTION_TEMPLATE_VERSION",
    "REVERSIBLE_ACTION_TASK_TYPE",
    "ActionMemoryContextAuthority",
    "GovernedReversibleActionCreator",
    "ReversibleActionOutput",
    "ReversibleActionPersister",
    "ReversibleActionRuntime",
]
