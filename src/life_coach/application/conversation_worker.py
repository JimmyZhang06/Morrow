"""Queued chat work: never automatically resend a dispatched/unknown request."""

import asyncio
from contextlib import suppress
from datetime import timedelta
from functools import partial

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from life_coach.ai.chat_stream import stream_turn
from life_coach.ai.provider import ProviderExecutionError, StructuredOutputValidationError
from life_coach.application.conversations import CHAT_TASK, preserve_interrupted_answer
from life_coach.application.model_runtime import (
    GovernedModelRuntime,
    ModelResultRejected,
    ModelRunProviderOutcomeUnknown,
)
from life_coach.application.source_entries import SourceContentProtector
from life_coach.modules.conversations import ConversationTurn
from life_coach.platform.auth import AuthenticatedPrincipal, ProductionSessionFactory
from life_coach.shared.database import utc_now


class ConversationWorker:
    def __init__(
        self,
        *,
        dispatcher: async_sessionmaker[AsyncSession],
        sessions: ProductionSessionFactory,
        runtime: GovernedModelRuntime,
        binding: str,
        protector: SourceContentProtector,
    ) -> None:
        self.dispatcher, self.sessions, self.runtime, self.binding = (
            dispatcher,
            sessions,
            runtime,
            binding,
        )
        self.task: asyncio.Task[None] | None = None
        self.protector = protector

    def start(self) -> None:
        self.task = asyncio.create_task(self.run(), name="conversation-worker")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def run(self) -> None:
        while True:
            job = None
            state = "failed"
            try:
                async with self.dispatcher() as db, db.begin():
                    job = (
                        await db.execute(
                            text("SELECT * FROM life_coach_private.claim_conversation_turn()")
                        )
                    ).one_or_none()
                if job:
                    principal = AuthenticatedPrincipal(
                        job.principal_id, utc_now() + timedelta(minutes=5)
                    )
                    if job.model_binding != self.binding:
                        state = "canceled"
                        raise ValueError("model binding changed")
                    with stream_turn(job.vault_id, job.id) as stream:
                        try:
                            await self.runtime.run_for_principal(
                                principal=principal,
                                vault_id=job.vault_id,
                                task_type=CHAT_TASK,
                                fragment_ids=(),
                                context_id=job.id,
                                idempotency_key=str(job.id),
                                expected_membership_generation=job.membership_generation,
                            )
                        except (
                            ModelRunProviderOutcomeUnknown,
                            ProviderExecutionError,
                            StructuredOutputValidationError,
                            ModelResultRejected,
                        ):
                            with suppress(Exception):
                                async with self.sessions.open_for_principal(
                                    principal=principal,
                                    vault_id=job.vault_id,
                                    expected_membership_generation=job.membership_generation,
                                ) as context:
                                    await context.session.run_sync(
                                        partial(
                                            preserve_interrupted_answer,
                                            vault_id=job.vault_id,
                                            turn_id=job.id,
                                            protector=self.protector,
                                            stream=stream,
                                        )
                                    )
                            raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, ModelRunProviderOutcomeUnknown):
                    state = "unknown"
                if job:
                    with suppress(Exception):
                        async with self.sessions.open_for_principal(
                            principal=AuthenticatedPrincipal(
                                job.principal_id, utc_now() + timedelta(minutes=1)
                            ),
                            vault_id=job.vault_id,
                            expected_membership_generation=job.membership_generation,
                        ) as context:
                            turn = await context.session.scalar(
                                select(ConversationTurn)
                                .where(
                                    ConversationTurn.vault_id == job.vault_id,
                                    ConversationTurn.id == job.id,
                                )
                                .with_for_update()
                            )
                            if turn and turn.state == "running":
                                turn.state = state
            await asyncio.sleep(0.5 if job else 2)
