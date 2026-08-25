"""Transactional service for one deterministic reversible micro-experiment."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from life_coach.modules.knowledge.enums import (
    ClaimVersionOrigin,
    LifecycleState,
    VerdictType,
)
from life_coach.modules.knowledge.models import (
    ClaimVersion,
    DerivedObject,
    MemoryClaim,
    UserVerdict,
)
from life_coach.modules.knowledge.reducer import reduce_verdicts
from life_coach.shared.database import utc_now

from .lifecycle import (
    ActionIdempotencyConflictError,
    ActionNotFoundError,
    ActionRevisionConflictError,
    MemoryNotEligibleForActionError,
    ReversibleActionPage,
    ReversibleActionState,
    ReversibleActionVerdict,
    ReversibleActionVerdictOutcome,
    ReversibleActionView,
    transition_action,
)
from .models import ActionCommandReceipt, ActionVerdict, ReversibleAction

_TEMPLATE_VERSION = "memory-observation-v1"
_ACTION_KIND = "reversible_experiment"
_TITLE = "观察一个具体例子"
_DESCRIPTION = (
    "用 10 分钟，写下一个近期具体情境：发生了什么、你注意到什么，以及它支持还是反驳这条认识。"  # noqa: RUF001
)
_RATIONALE = "用一次低成本观察检验已由你确认或纠正的认识。"
_EXIT_PLAN = "随时停止；不创建外部任务、不通知他人，也不会自动安排后续行动。"  # noqa: RUF001


def _fingerprint(kind: str, payload: dict[str, str | int]) -> str:
    encoded = json.dumps(
        {"kind": kind, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def make_action_etag(action_id: uuid.UUID, revision: int) -> str:
    return f'"action:{action_id}:{revision}"'


class AsyncReversibleActionOperations(Protocol):
    async def create_for_memory(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        idempotency_key: uuid.UUID,
    ) -> ReversibleActionView: ...

    async def get(self, *, vault_id: uuid.UUID, action_id: uuid.UUID) -> ReversibleActionView: ...

    async def list(
        self, *, vault_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> ReversibleActionPage: ...

    async def record_verdict(
        self,
        *,
        vault_id: uuid.UUID,
        action_id: uuid.UUID,
        verdict: ReversibleActionVerdict,
        expected_revision: int,
        idempotency_key: uuid.UUID,
    ) -> ReversibleActionVerdictOutcome: ...


class ReversibleActionService:
    """Synchronous unit-of-work implementation used inside a scoped transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_for_memory(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        idempotency_key: uuid.UUID,
    ) -> ReversibleActionView:
        command_fingerprint = _fingerprint(
            "create-action-v1",
            {
                "vault_id": str(vault_id),
                "memory_id": str(memory_id),
                "template_version": _TEMPLATE_VERSION,
            },
        )
        with self._session.begin_nested():
            replay = self._receipt(vault_id=vault_id, idempotency_key=idempotency_key)
            if replay is not None:
                self._require_replay(
                    replay,
                    command_kind="create",
                    command_fingerprint=command_fingerprint,
                )
                return self._view(
                    self._locked_action(vault_id=vault_id, action_id=replay.action_id)
                )

            claim = self._session.scalar(
                select(MemoryClaim)
                .where(
                    MemoryClaim.vault_id == vault_id,
                    MemoryClaim.id == memory_id,
                    MemoryClaim.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if claim is None:
                raise MemoryNotEligibleForActionError(
                    "the current Memory is unavailable or not eligible"
                )
            version = self._current_eligible_version(vault_id=vault_id, claim=claim)
            existing = self._session.scalar(
                select(ReversibleAction).where(
                    ReversibleAction.vault_id == vault_id,
                    ReversibleAction.source_derived_object_id == version.derived_object_id,
                    ReversibleAction.template_version == _TEMPLATE_VERSION,
                )
            )
            now = utc_now()
            if existing is None:
                existing = ReversibleAction(
                    id=uuid.uuid4(),
                    vault_id=vault_id,
                    memory_claim_id=claim.id,
                    source_derived_object_id=version.derived_object_id,
                    source_version_no=version.version_no,
                    model_run_id=None,
                    template_version=_TEMPLATE_VERSION,
                    kind=_ACTION_KIND,
                    title=_TITLE,
                    description=_DESCRIPTION,
                    rationale=_RATIONALE,
                    exit_plan=_EXIT_PLAN,
                    estimated_minutes=10,
                    is_reversible=True,
                    state=ReversibleActionState.PROPOSED,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
                self._session.add(existing)
                self._session.flush([existing])
            self._session.add(
                ActionCommandReceipt(
                    id=uuid.uuid4(),
                    vault_id=vault_id,
                    idempotency_key=idempotency_key,
                    command_kind="create",
                    command_fingerprint=command_fingerprint,
                    action_id=existing.id,
                    verdict_id=None,
                    created_at=now,
                )
            )
            self._session.flush()
            return self._view(existing)

    def create_generated_for_memory(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        model_run_id: uuid.UUID,
        idempotency_key: uuid.UUID,
        template_version: str,
        title: str,
        description: str,
        rationale: str,
        exit_plan: str,
        estimated_minutes: int,
    ) -> ReversibleActionView:
        """Persist one validated model draft under server-owned action invariants."""

        payload: dict[str, str | int] = {
            "vault_id": str(vault_id),
            "memory_id": str(memory_id),
            "model_run_id": str(model_run_id),
            "template_version": template_version,
            "title": title,
            "description": description,
            "rationale": rationale,
            "exit_plan": exit_plan,
            "estimated_minutes": estimated_minutes,
        }
        command_fingerprint = _fingerprint("create-action-ai-v1", payload)
        with self._session.begin_nested():
            replay = self._receipt(vault_id=vault_id, idempotency_key=idempotency_key)
            if replay is not None:
                self._require_replay(
                    replay,
                    command_kind="create",
                    command_fingerprint=command_fingerprint,
                )
                return self._view(
                    self._locked_action(vault_id=vault_id, action_id=replay.action_id)
                )

            claim = self._session.scalar(
                select(MemoryClaim)
                .where(
                    MemoryClaim.vault_id == vault_id,
                    MemoryClaim.id == memory_id,
                    MemoryClaim.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if claim is None:
                raise MemoryNotEligibleForActionError(
                    "the current Memory is unavailable or not eligible"
                )
            version = self._current_eligible_version(vault_id=vault_id, claim=claim)
            existing = self._session.scalar(
                select(ReversibleAction).where(
                    ReversibleAction.vault_id == vault_id,
                    ReversibleAction.source_derived_object_id == version.derived_object_id,
                    ReversibleAction.template_version == template_version,
                )
            )
            if existing is not None and existing.model_run_id != model_run_id:
                raise ActionIdempotencyConflictError(
                    "the current Memory already has a different generated action"
                )
            now = utc_now()
            if existing is None:
                existing = ReversibleAction(
                    id=uuid.uuid4(),
                    vault_id=vault_id,
                    memory_claim_id=claim.id,
                    source_derived_object_id=version.derived_object_id,
                    source_version_no=version.version_no,
                    model_run_id=model_run_id,
                    template_version=template_version,
                    kind=_ACTION_KIND,
                    title=title,
                    description=description,
                    rationale=rationale,
                    exit_plan=exit_plan,
                    estimated_minutes=estimated_minutes,
                    is_reversible=True,
                    state=ReversibleActionState.PROPOSED,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
                self._session.add(existing)
                self._session.flush([existing])
            self._session.add(
                ActionCommandReceipt(
                    id=uuid.uuid4(),
                    vault_id=vault_id,
                    idempotency_key=idempotency_key,
                    command_kind="create",
                    command_fingerprint=command_fingerprint,
                    action_id=existing.id,
                    verdict_id=None,
                    created_at=now,
                )
            )
            self._session.flush()
            return self._view(existing)

    def get(self, *, vault_id: uuid.UUID, action_id: uuid.UUID) -> ReversibleActionView:
        action = self._session.scalar(self._action_query(vault_id=vault_id, action_id=action_id))
        if action is None:
            raise ActionNotFoundError("action is unavailable")
        return self._view(action)

    def list(
        self, *, vault_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> ReversibleActionPage:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        offset = self._decode_cursor(cursor)
        actions = list(
            self._session.scalars(
                select(ReversibleAction)
                .where(ReversibleAction.vault_id == vault_id)
                .order_by(ReversibleAction.created_at.desc(), ReversibleAction.id.desc())
            )
        )
        page_actions = actions[offset : offset + limit]
        next_offset = offset + len(page_actions)
        return ReversibleActionPage(
            items=tuple(self._view(action) for action in page_actions),
            next_cursor=(self._encode_cursor(next_offset) if next_offset < len(actions) else None),
        )

    def record_verdict(
        self,
        *,
        vault_id: uuid.UUID,
        action_id: uuid.UUID,
        verdict: ReversibleActionVerdict,
        expected_revision: int,
        idempotency_key: uuid.UUID,
    ) -> ReversibleActionVerdictOutcome:
        command_fingerprint = _fingerprint(
            "action-verdict-v1",
            {
                "vault_id": str(vault_id),
                "action_id": str(action_id),
                "verdict": verdict.value,
                "expected_revision": expected_revision,
            },
        )
        with self._session.begin_nested():
            replay = self._receipt(vault_id=vault_id, idempotency_key=idempotency_key)
            if replay is not None:
                self._require_replay(
                    replay,
                    command_kind="verdict",
                    command_fingerprint=command_fingerprint,
                )
                if replay.verdict_id is None:  # protected by the database constraint
                    raise ActionIdempotencyConflictError("idempotency receipt is invalid")
                action = self._locked_action(vault_id=vault_id, action_id=replay.action_id)
                return ReversibleActionVerdictOutcome(
                    verdict_id=replay.verdict_id,
                    action=self._view(action),
                )

            action = self._locked_action(vault_id=vault_id, action_id=action_id)
            if action.revision != expected_revision:
                raise ActionRevisionConflictError(action.revision)
            next_state = transition_action(action.state, verdict)
            now = utc_now()
            next_revision = action.revision + 1
            event = ActionVerdict(
                id=uuid.uuid4(),
                vault_id=vault_id,
                action_id=action.id,
                sequence_no=next_revision - 1,
                verdict=verdict,
                resulting_state=next_state,
                resulting_revision=next_revision,
                created_at=now,
            )
            action.state = next_state
            action.revision = next_revision
            action.updated_at = now
            self._session.add(event)
            self._session.flush([action, event])
            self._session.add(
                ActionCommandReceipt(
                    id=uuid.uuid4(),
                    vault_id=vault_id,
                    idempotency_key=idempotency_key,
                    command_kind="verdict",
                    command_fingerprint=command_fingerprint,
                    action_id=action.id,
                    verdict_id=event.id,
                    created_at=now,
                )
            )
            self._session.flush()
            return ReversibleActionVerdictOutcome(
                verdict_id=event.id,
                action=self._view(action),
            )

    def _current_eligible_version(self, *, vault_id: uuid.UUID, claim: MemoryClaim) -> ClaimVersion:
        version = self._session.scalar(
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
            .with_for_update()
        )
        if version is None or version.lifecycle_state not in {
            LifecycleState.CANDIDATE,
            LifecycleState.ACTIVE,
        }:
            raise MemoryNotEligibleForActionError(
                "the current Memory is unavailable or not eligible"
            )
        events = self._session.scalars(
            select(UserVerdict).where(
                UserVerdict.vault_id == vault_id,
                UserVerdict.target_derived_object_id == version.derived_object_id,
            )
        ).all()
        reduced = reduce_verdicts(version.initial_lifecycle_state, events, can_activate=False)
        user_confirmed = reduced.last_decisive_verdict is VerdictType.CONFIRM
        user_corrected = (
            version.origin is ClaimVersionOrigin.USER_CORRECTION
            and version.origin_verdict_id is not None
        )
        if not (user_confirmed or user_corrected):
            raise MemoryNotEligibleForActionError(
                "the current Memory is unavailable or not eligible"
            )
        return version

    @staticmethod
    def _action_query(
        *, vault_id: uuid.UUID, action_id: uuid.UUID
    ) -> Select[tuple[ReversibleAction]]:
        return select(ReversibleAction).where(
            ReversibleAction.vault_id == vault_id,
            ReversibleAction.id == action_id,
        )

    def _locked_action(self, *, vault_id: uuid.UUID, action_id: uuid.UUID) -> ReversibleAction:
        action = self._session.scalar(
            self._action_query(vault_id=vault_id, action_id=action_id).with_for_update()
        )
        if action is None:
            raise ActionNotFoundError("action is unavailable")
        return action

    def _receipt(
        self, *, vault_id: uuid.UUID, idempotency_key: uuid.UUID
    ) -> ActionCommandReceipt | None:
        return self._session.scalar(
            select(ActionCommandReceipt).where(
                ActionCommandReceipt.vault_id == vault_id,
                ActionCommandReceipt.idempotency_key == idempotency_key,
            )
        )

    @staticmethod
    def _encode_cursor(offset: int) -> str:
        return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            padding = "=" * (-len(cursor) % 4)
            value = base64.urlsafe_b64decode((cursor + padding).encode()).decode()
            offset = int(value)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("invalid action cursor") from exc
        if offset < 0 or str(offset) != value:
            raise ValueError("invalid action cursor")
        return offset

    @staticmethod
    def _require_replay(
        receipt: ActionCommandReceipt,
        *,
        command_kind: str,
        command_fingerprint: str,
    ) -> None:
        if (
            receipt.command_kind != command_kind
            or receipt.command_fingerprint != command_fingerprint
        ):
            raise ActionIdempotencyConflictError(
                "idempotency key is already bound to a different command"
            )

    @staticmethod
    def _view(action: ReversibleAction) -> ReversibleActionView:
        return ReversibleActionView(
            action_id=action.id,
            memory_id=action.memory_claim_id,
            source_derived_object_id=action.source_derived_object_id,
            model_run_id=action.model_run_id,
            state=action.state,
            revision=action.revision,
            kind=action.kind,
            title=action.title,
            description=action.description,
            rationale=action.rationale,
            exit_plan=action.exit_plan,
            estimated_minutes=action.estimated_minutes,
            is_reversible=action.is_reversible,
            template_version=action.template_version,
            created_at=_utc(action.created_at),
            updated_at=_utc(action.updated_at),
        )


class AsyncReversibleActionService:
    """Async adapter retaining the caller's authenticated Vault transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_for_memory(
        self,
        *,
        vault_id: uuid.UUID,
        memory_id: uuid.UUID,
        idempotency_key: uuid.UUID,
    ) -> ReversibleActionView:
        return await self._session.run_sync(
            lambda session: ReversibleActionService(session).create_for_memory(
                vault_id=vault_id,
                memory_id=memory_id,
                idempotency_key=idempotency_key,
            )
        )

    async def get(self, *, vault_id: uuid.UUID, action_id: uuid.UUID) -> ReversibleActionView:
        return await self._session.run_sync(
            lambda session: ReversibleActionService(session).get(
                vault_id=vault_id,
                action_id=action_id,
            )
        )

    async def list(
        self, *, vault_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> ReversibleActionPage:
        return await self._session.run_sync(
            lambda session: ReversibleActionService(session).list(
                vault_id=vault_id, limit=limit, cursor=cursor
            )
        )

    async def record_verdict(
        self,
        *,
        vault_id: uuid.UUID,
        action_id: uuid.UUID,
        verdict: ReversibleActionVerdict,
        expected_revision: int,
        idempotency_key: uuid.UUID,
    ) -> ReversibleActionVerdictOutcome:
        return await self._session.run_sync(
            lambda session: ReversibleActionService(session).record_verdict(
                vault_id=vault_id,
                action_id=action_id,
                verdict=verdict,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        )


__all__ = [
    "AsyncReversibleActionOperations",
    "AsyncReversibleActionService",
    "ReversibleActionService",
    "make_action_etag",
]
