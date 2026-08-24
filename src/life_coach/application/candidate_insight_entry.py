"""Resolve a user-facing Source entry into governed candidate inputs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from life_coach.application.candidate_insight_command import (
    CandidateInsightGenerationResult,
    CandidateInsightRuntime,
    GenerateCandidateInsight,
)
from life_coach.application.model_runtime import AuthorizedVaultSessionOpener
from life_coach.modules.sources.models import SourceDocument, SourceFragment, SourceRevision


class EntryCandidateSourceUnavailable(RuntimeError):
    """The entry has no current, model-eligible fragment in this Vault."""


@dataclass(frozen=True, slots=True)
class EntryCandidateRevisionConflict(RuntimeError):
    current_revision: int


class GenerateCandidateInsightForEntry:
    """Translate a public entry ID into current internal fragment IDs server-side."""

    def __init__(
        self,
        *,
        sessions: AuthorizedVaultSessionOpener,
        runtime: CandidateInsightRuntime,
    ) -> None:
        self._sessions = sessions
        self._generate = GenerateCandidateInsight(runtime)

    async def execute(
        self,
        *,
        authorization: str | None,
        vault_id: uuid.UUID,
        entry_id: uuid.UUID,
        expected_revision: int,
        idempotency_key: uuid.UUID,
    ) -> CandidateInsightGenerationResult:
        principal = await self._sessions.authenticate(authorization)
        async with self._sessions.open_for_principal(
            principal=principal,
            vault_id=vault_id,
        ) as authorized:
            revision_no, fragment_ids = await authorized.session.run_sync(
                lambda session: self._resolve_entry(
                    session,
                    vault_id=vault_id,
                    entry_id=entry_id,
                )
            )
            if revision_no != expected_revision:
                raise EntryCandidateRevisionConflict(current_revision=revision_no)
        return await self._generate.execute(
            authorization=authorization,
            vault_id=vault_id,
            fragment_ids=fragment_ids,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def _resolve_entry(
        session: object,
        *,
        vault_id: uuid.UUID,
        entry_id: uuid.UUID,
    ) -> tuple[int, tuple[uuid.UUID, ...]]:
        from sqlalchemy.orm import Session

        if not isinstance(session, Session):
            raise EntryCandidateSourceUnavailable("entry is unavailable")
        row = session.execute(
            select(SourceRevision.id, SourceRevision.revision_no)
            .join(
                SourceDocument,
                (SourceDocument.vault_id == SourceRevision.vault_id)
                & (SourceDocument.id == SourceRevision.document_id),
            )
            .where(
                SourceDocument.vault_id == vault_id,
                SourceDocument.id == entry_id,
                SourceDocument.deleted_at.is_(None),
                SourceRevision.deleted_at.is_(None),
                SourceDocument.current_revision_id == SourceRevision.id,
            )
        ).one_or_none()
        if row is None:
            raise EntryCandidateSourceUnavailable("entry is unavailable")
        revision_id, revision_no = row
        fragment_ids = tuple(
            session.scalars(
                select(SourceFragment.id)
                .where(
                    SourceFragment.vault_id == vault_id,
                    SourceFragment.revision_id == revision_id,
                    SourceFragment.deleted_at.is_(None),
                )
                .order_by(SourceFragment.ordinal)
            )
        )
        if not fragment_ids:
            raise EntryCandidateSourceUnavailable("entry is unavailable")
        return revision_no, fragment_ids


__all__ = [
    "EntryCandidateRevisionConflict",
    "EntryCandidateSourceUnavailable",
    "GenerateCandidateInsightForEntry",
]
