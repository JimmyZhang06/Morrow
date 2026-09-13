"""Small transactional background batches; crash rolls back both indexes and cursor."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from life_coach.application.local_search import _sources, index_diary_fragment
from life_coach.modules.consent import ConsentPurpose
from life_coach.modules.consent.service import resolve_consent
from life_coach.modules.identity.models import Vault
from life_coach.modules.sources.index_jobs import DiaryIndexJob
from life_coach.modules.sources.models import SourceDocument, SourceFragment
from life_coach.platform.auth import AuthenticatedPrincipal, ProductionSessionFactory
from life_coach.shared.database import utc_now

if TYPE_CHECKING:
    from life_coach.application.source_entries import SourceContentProtector

BATCH_SIZE = 25


def run_index_batch(
    session: Session, *, vault_id: uuid.UUID, job_id: uuid.UUID,
    protector: SourceContentProtector,
) -> None:
    # Same order as API/source writes; no lease is needed across local transactions.
    session.execute(select(Vault.id).where(Vault.id == vault_id).with_for_update())
    job = session.scalar(select(DiaryIndexJob).where(
        DiaryIndexJob.vault_id == vault_id, DiaryIndexJob.id == job_id,
    ).with_for_update())
    if job is None or job.state != "queued":
        return
    if not resolve_consent(session, vault_id=vault_id, purpose=ConsentPurpose.SEARCH).allowed:
        job.state = "canceled"
        job.finished_at = utc_now()
        return
    statement = _sources(vault_id).where(SourceDocument.created_at <= job.created_at)
    if job.cursor is not None:
        statement = statement.where(SourceFragment.id > job.cursor)
    rows = session.execute(statement.order_by(SourceFragment.id).limit(BATCH_SIZE)).all()
    for document, revision, fragment in rows:
        # Source opt-outs are checked before any protected content is opened.
        if resolve_consent(session, vault_id=vault_id, purpose=ConsentPurpose.SEARCH,
                           source_document_id=document.id).allowed:
            from life_coach.modules.identity import DataClass
            if (document.data_class is not DataClass.HIGHLY_SENSITIVE
                    and fragment.data_class is not DataClass.HIGHLY_SENSITIVE):
                plaintext = protector.open(
                    vault_id=vault_id, document_id=document.id, object_id=fragment.id,
                    revision_no=revision.revision_no, kind="fragment",
                    ciphertext=fragment.text_ciphertext,
                )
                job.indexed += int(index_diary_fragment(
                    session, vault_id=vault_id, fragment_id=fragment.id, plaintext=plaintext,
                ))
        job.processed += 1
        job.cursor = fragment.id
    job.updated_at = utc_now()
    if len(rows) < BATCH_SIZE:
        job.state = "completed"
        job.finished_at = utc_now()


def fail_index_job(session: Session, *, vault_id: uuid.UUID, job_id: uuid.UUID) -> None:
    session.execute(select(Vault.id).where(Vault.id == vault_id).with_for_update())
    row = session.scalar(select(DiaryIndexJob).where(
        DiaryIndexJob.vault_id == vault_id, DiaryIndexJob.id == job_id,
    ))
    if row is not None and row.state == "queued":
        row.state = "failed"
        row.finished_at = utc_now()


class DiaryIndexWorker:
    def __init__(self, *, dispatcher: async_sessionmaker[AsyncSession],
                 sessions: ProductionSessionFactory, protector: SourceContentProtector) -> None:
        self.dispatcher = dispatcher
        self.sessions = sessions
        self.protector = protector
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self.run(), name="diary-index-worker")

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def run(self) -> None:
        while True:
            job = None
            try:
                async with self.dispatcher() as session, session.begin():
                    job = (await session.execute(text(
                        "SELECT * FROM life_coach_private.next_diary_index_job()"
                    ))).one_or_none()
                if job is not None:
                    async with self.sessions.open_for_principal(
                        principal=AuthenticatedPrincipal(job.principal_id,
                                                         utc_now() + timedelta(minutes=1)),
                        vault_id=job.vault_id,
                        expected_membership_generation=job.membership_generation,
                    ) as authorized:
                        await authorized.session.run_sync(partial(run_index_batch,
                            vault_id=job.vault_id, job_id=job.id, protector=self.protector,
                        ))
            except asyncio.CancelledError:
                raise
            except Exception:
                # No content, queries, identifiers or raw database diagnostics in logs.
                logging.getLogger(__name__).warning("diary_index_batch_failed")
                if job is not None:
                    with suppress(Exception):
                        async with self.sessions.open_for_principal(
                            principal=AuthenticatedPrincipal(job.principal_id,
                                                             utc_now() + timedelta(minutes=1)),
                            vault_id=job.vault_id,
                            expected_membership_generation=job.membership_generation,
                        ) as authorized:
                            await authorized.session.run_sync(partial(fail_index_job,
                                vault_id=job.vault_id, job_id=job.id,
                            ))
            await asyncio.sleep(0.5 if job is not None else 2)
