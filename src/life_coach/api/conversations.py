"""Explicitly scoped saved conversations; no renderer-controlled provider prompts."""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from life_coach.ai.chat_stream import get_stream
from life_coach.application.conversations import (
    ConversationContextLimit,
    ConversationTitleConflict,
    conversation_view,
    delete_conversation,
    enqueue_turn,
    lock_chat,
    new_conversation,
    rename_conversation,
)
from life_coach.application.model_gateway import ModelInvocationDenied
from life_coach.application.source_entries import SourceContentProtector
from life_coach.modules.consent.exceptions import ConsentDenied
from life_coach.modules.conversations import Conversation, ConversationTurn
from life_coach.platform.auth import (
    AuthenticationDenied,
    AuthorizedVaultSession,
    ProductionSessionFactory,
    VaultMembershipDenied,
)
from life_coach.platform.errors import ProblemError, problem_type


class NewConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_ids: list[uuid.UUID] = Field(default_factory=list, max_length=8)
    allow_history: bool = False
    include_reviewed_memories: bool = False


class RenameConversation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=60)
    expected_revision: int = Field(ge=0)


class CarePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    presentation: Literal["gentle", "inbox"]
    include_private_diaries: bool = False


class CareResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["dismiss", "pause", "resume"]
    letter_id: uuid.UUID | None = None


class NewTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    question: str = Field(min_length=1, max_length=3000, repr=False)
    auto_retrieve: bool = False
    experience: Literal["conversation", "past_letter"] = "conversation"


def build_conversations_router(
    *,
    sessions: ProductionSessionFactory,
    protector: SourceContentProtector,
    model_binding: str | None,
) -> APIRouter:
    router = APIRouter(prefix="/v1/conversations", tags=["conversations"])

    async def authorized(
        response: Response,
        vault_id: Annotated[uuid.UUID, Header(alias="X-Vault-ID")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> AsyncIterator[AuthorizedVaultSession]:
        response.headers["Cache-Control"] = "private, no-store"
        try:
            async with sessions.open(authorization=authorization, vault_id=vault_id) as context:
                yield context
        except (
            AuthenticationDenied,
            VaultMembershipDenied,
            ModelInvocationDenied,
            ConsentDenied,
        ) as exc:
            if isinstance(exc, ConversationContextLimit):
                raise ProblemError(
                    type=problem_type("conversation-context-limit"),
                    title="本次对话已达到上下文上限",
                    status=409,
                    code="CONVERSATION_CONTEXT_LIMIT",
                    safe_detail="为避免丢失前文，本次没有发送。请新建对话并带上需要继续讨论的信息。",  # noqa: RUF001
                ) from None
            status = (
                401
                if isinstance(exc, AuthenticationDenied)
                else 404
                if isinstance(exc, VaultMembershipDenied)
                else 409
            )
            raise ProblemError(
                type=problem_type("conversation-unavailable"),
                title="对话暂不可用",
                status=status,
                code="CONVERSATION_UNAVAILABLE",
                safe_detail="请检查模型与授权、所选日记是否变化，或新建对话重试。",  # noqa: RUF001
            ) from None

    @router.get("")
    async def listing(
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> dict[str, object]:
        return await context.session.run_sync(
            lambda db: {
                "items": [
                    conversation_view(
                        db,
                        vault_id=context.context.vault_id,
                        conversation_id=row.id,
                        protector=protector,
                        summary_only=True,
                    )
                    for row in db.scalars(
                        select(Conversation)
                        .where(
                            Conversation.vault_id == context.context.vault_id,
                            Conversation.deleted_at.is_(None),
                            Conversation.care_origin.is_(False),
                        )
                        .order_by(Conversation.created_at.desc())
                        .limit(100)
                    )
                ],
                "ready": model_binding is not None,
            }
        )

    @router.post("", status_code=201)
    async def create(
        body: NewConversation,
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> dict[str, str]:
        if model_binding is None:
            raise ModelInvocationDenied("model not configured")
        row = await context.session.run_sync(
            lambda db: new_conversation(
                db,
                vault_id=context.context.vault_id,
                principal_id=context.context.principal_id,
                entry_ids=list(dict.fromkeys(body.entry_ids)),
                allow_history=body.allow_history,
                include_reviewed_memories=body.include_reviewed_memories,
            )
        )
        return {"id": str(row.id)}

    @router.get("/care/state")
    async def care_state(
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> dict[str, object]:
        from life_coach.application.care_letters import view

        return await context.session.run_sync(
            lambda db: view(db, vault_id=context.context.vault_id, protector=protector)
        )

    @router.put("/care/preferences")
    async def care_preferences(
        body: CarePreferences,
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> dict[str, bool]:
        from life_coach.application.care_letters import configure

        if body.enabled and model_binding is None:
            raise ModelInvocationDenied("model not configured")
        await context.session.run_sync(
            lambda db: configure(
                db,
                vault_id=context.context.vault_id,
                principal_id=context.context.principal_id,
                enabled=body.enabled,
                presentation=body.presentation,
                include_private_diaries=body.include_private_diaries,
            )
        )
        return {"ok": True}

    @router.post("/care/respond")
    async def care_respond(
        body: CareResponse,
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> dict[str, bool]:
        from life_coach.application.care_letters import respond

        await context.session.run_sync(
            lambda db: respond(
                db,
                vault_id=context.context.vault_id,
                action=body.action,
                letter_id=str(body.letter_id) if body.letter_id else None,
            )
        )
        return {"ok": True}

    @router.post("/care/check")
    async def care_check(
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> dict[str, bool]:
        from life_coach.application.care_letters import check_diaries

        if model_binding is not None:
            await context.session.run_sync(
                lambda db: check_diaries(
                    db,
                    vault_id=context.context.vault_id,
                    principal_id=context.context.principal_id,
                    membership_generation=context.context.membership_generation,
                    model_binding=model_binding,
                    protector=protector,
                )
            )
        return {"ok": True}

    @router.get("/{conversation_id}")
    async def detail(
        conversation_id: uuid.UUID,
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> dict[str, object]:
        return await context.session.run_sync(
            lambda db: conversation_view(
                db,
                vault_id=context.context.vault_id,
                conversation_id=conversation_id,
                protector=protector,
            )
        )

    @router.patch("/{conversation_id}/title")
    async def rename(
        conversation_id: uuid.UUID,
        body: RenameConversation,
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> dict[str, object]:
        def save_title(db: Session) -> dict[str, object]:
            rename_conversation(
                db,
                vault_id=context.context.vault_id,
                conversation_id=conversation_id,
                title=body.title,
                expected_revision=body.expected_revision,
                protector=protector,
            )
            return conversation_view(
                db,
                vault_id=context.context.vault_id,
                conversation_id=conversation_id,
                protector=protector,
                summary_only=True,
            )

        try:
            return await context.session.run_sync(save_title)
        except ConversationTitleConflict:
            raise ProblemError(
                type=problem_type("conversation-title-conflict"),
                title="名称已更新",
                status=409,
                code="CONVERSATION_TITLE_CONFLICT",
                safe_detail="名称已被修改，请刷新后重试。",  # noqa: RUF001
            ) from None

    @router.post("/{conversation_id}/turns", status_code=202)
    async def send(
        conversation_id: uuid.UUID,
        body: NewTurn,
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> dict[str, str]:
        if model_binding is None or not body.question.strip():
            raise ModelInvocationDenied("model not configured")
        row = await context.session.run_sync(
            lambda db: enqueue_turn(
                db,
                vault_id=context.context.vault_id,
                conversation_id=conversation_id,
                principal_id=context.context.principal_id,
                membership_generation=context.context.membership_generation,
                request_id=body.request_id,
                question=body.question,
                auto_retrieve=body.auto_retrieve,
                experience=body.experience,
                model_binding=model_binding,
                protector=protector,
            )
        )
        return {"id": str(row.id), "state": row.state}

    @router.delete("/{conversation_id}", status_code=204, response_class=Response)
    async def delete(
        conversation_id: uuid.UUID,
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> None:
        await context.session.run_sync(
            lambda db: delete_conversation(
                db, vault_id=context.context.vault_id, conversation_id=conversation_id
            )
        )

    @router.post("/{conversation_id}/cancel", status_code=204, response_class=Response)
    async def cancel(
        conversation_id: uuid.UUID,
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> None:
        def stop(db: Session) -> None:
            lock_chat(db, context.context.vault_id, conversation_id)
            for turn in db.scalars(
                select(ConversationTurn).where(
                    ConversationTurn.vault_id == context.context.vault_id,
                    ConversationTurn.conversation_id == conversation_id,
                    ConversationTurn.state.in_(["queued", "running"]),
                )
            ):
                turn.state = "canceled"
                stream = get_stream(context.context.vault_id, turn.id)
                if stream is not None:
                    stream.close()

        await context.session.run_sync(stop)

    return router
