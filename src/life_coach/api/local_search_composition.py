"""Authenticated local-search routes; enabling search never authorizes online AI."""

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from life_coach.application.local_search import SearchResults, search_diaries
from life_coach.application.source_entries import SourceContentProtector
from life_coach.modules.consent import ConsentAction, ConsentPurpose
from life_coach.modules.consent.service import (
    UserConsentCommand,
    grant_consent,
    resolve_consent,
    revoke_consent,
)
from life_coach.modules.identity import get_vault
from life_coach.modules.identity.models import Vault
from life_coach.modules.sources.index_jobs import DiaryIndexJob
from life_coach.platform.auth import (
    AuthenticationDenied,
    AuthorizedVaultSession,
    ProductionSessionFactory,
    VaultMembershipDenied,
)
from life_coach.platform.errors import ProblemError, problem_type
from life_coach.shared.database import utc_now


class LocalSearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500, repr=False)
    limit: int = Field(default=10, ge=1, le=50)


class LocalSearchPermission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    expected_policy_epoch: int = Field(ge=0)


class LocalSearchStatus(BaseModel):
    enabled: bool
    policy_epoch: int
    mode: str = "local_lexical"


class IndexJobStatus(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    state: str
    processed: int
    indexed: int


def build_local_search_router(
    *, sessions: ProductionSessionFactory, protector: SourceContentProtector,
) -> APIRouter:
    router = APIRouter(prefix="/v1/local-search", tags=["local-search"])

    async def authorized(
        response: Response,
        vault_id: Annotated[uuid.UUID, Header(alias="X-Vault-ID")],
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> AsyncIterator[AuthorizedVaultSession]:
        response.headers["Cache-Control"] = "private, no-store"
        try:
            async with sessions.open(authorization=authorization, vault_id=vault_id) as value:
                yield value
        except AuthenticationDenied:
            raise ProblemError(
                type=problem_type("authentication-required"), title="身份验证失败",
                status=401, code="AUTHENTICATION_REQUIRED", safe_detail="请检查本地服务。",
            ) from None
        except VaultMembershipDenied:
            raise ProblemError(
                type=problem_type("vault-unavailable"), title="空间不可用",
                status=404, code="VAULT_UNAVAILABLE", safe_detail="请求的空间不可用。",
            ) from None

    @router.get("/status", response_model=LocalSearchStatus)
    async def status(
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> LocalSearchStatus:
        return await context.session.run_sync(lambda session: LocalSearchStatus(
            enabled=resolve_consent(
                session, vault_id=context.context.vault_id, purpose=ConsentPurpose.SEARCH,
            ).allowed,
            policy_epoch=get_vault(session, context.context.vault_id).policy_epoch,
        ))

    @router.post("/permission", response_model=LocalSearchStatus)
    async def permission(
        body: LocalSearchPermission,
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> LocalSearchStatus:
        def update_permission(session: Session) -> LocalSearchStatus:
            vault_id = context.context.vault_id
            session.execute(select(Vault.id).where(Vault.id == vault_id).with_for_update())
            vault = get_vault(session, vault_id)
            current = resolve_consent(session, vault_id=vault_id, purpose=ConsentPurpose.SEARCH)
            if current.allowed == body.enabled:
                return LocalSearchStatus(enabled=current.allowed, policy_epoch=vault.policy_epoch)
            if vault.policy_epoch != body.expected_policy_epoch:
                raise ProblemError(
                    type=problem_type("policy-conflict"), title="设置已变化", status=409,
                    code="POLICY_CONFLICT", safe_detail="请刷新设置后重试。",
                )
            now = utc_now()
            command = UserConsentCommand(
                vault_id=vault_id, principal_id=context.context.principal_id,
                purpose=ConsentPurpose.SEARCH,
                action=ConsentAction.GRANT if body.enabled else ConsentAction.REVOKE,
                interaction_id=uuid.uuid4(), issued_at=now,
                expires_at=now + timedelta(minutes=1),
            )
            record = (grant_consent if body.enabled else revoke_consent)(session, command=command)
            if not body.enabled:
                session.execute(update(DiaryIndexJob).where(
                    DiaryIndexJob.vault_id == vault_id, DiaryIndexJob.state == "queued",
                ).values(state="canceled", finished_at=now))
            return LocalSearchStatus(enabled=body.enabled, policy_epoch=record.policy_epoch)

        return await context.session.run_sync(update_permission)

    @router.post("/query", response_model=SearchResults)
    async def query(
        body: LocalSearchQuery,
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> SearchResults:
        return await context.session.run_sync(lambda session: search_diaries(
            session, vault_id=context.context.vault_id,
            query=body.query, limit=body.limit, protector=protector,
        ))

    @router.post("/rebuild", response_model=SearchResults)
    async def rebuild(
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> SearchResults:
        return await context.session.run_sync(lambda session: search_diaries(
            session, vault_id=context.context.vault_id, query="",
            protector=protector, rebuild=True,
        ))

    @router.get("/index-job", response_model=IndexJobStatus | None)
    async def job_status(
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> IndexJobStatus | None:
        def latest(session: Session) -> IndexJobStatus | None:
            job = session.scalar(select(DiaryIndexJob).where(
                DiaryIndexJob.vault_id == context.context.vault_id,
            ).order_by(DiaryIndexJob.created_at.desc(), DiaryIndexJob.id.desc()).limit(1))
            return IndexJobStatus.model_validate(job) if job else None
        return await context.session.run_sync(latest)

    @router.post("/index-job", response_model=IndexJobStatus, status_code=202)
    async def start_job(
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> IndexJobStatus:
        def enqueue(session: Session) -> IndexJobStatus:
            vault_id = context.context.vault_id
            session.execute(select(Vault.id).where(Vault.id == vault_id).with_for_update())
            consent = resolve_consent(session, vault_id=vault_id, purpose=ConsentPurpose.SEARCH)
            if not consent.allowed:
                raise ProblemError(type=problem_type("search-disabled"), title="搜索未开启",
                                   status=409, code="SEARCH_DISABLED",
                                   safe_detail="请先开启本地搜索。")
            job = session.scalar(select(DiaryIndexJob).where(
                DiaryIndexJob.vault_id == vault_id, DiaryIndexJob.state == "queued",
            ))
            if job is None:
                job = DiaryIndexJob(
                    vault_id=vault_id, principal_id=context.context.principal_id,
                    membership_generation=context.context.membership_generation,
                )
                session.add(job)
                session.flush()
            return IndexJobStatus.model_validate(job)
        return await context.session.run_sync(enqueue)

    @router.post("/index-job/{job_id}/cancel", status_code=204)
    async def cancel_job(
        job_id: uuid.UUID,
        context: Annotated[AuthorizedVaultSession, Depends(authorized, scope="function")],
    ) -> None:
        def cancel(session: Session) -> None:
            session.execute(select(Vault.id).where(
                Vault.id == context.context.vault_id).with_for_update())
            session.execute(update(DiaryIndexJob).where(
                DiaryIndexJob.vault_id == context.context.vault_id,
                DiaryIndexJob.id == job_id, DiaryIndexJob.state == "queued",
            ).values(state="canceled", finished_at=utc_now()))
        await context.session.run_sync(cancel)

    return router
