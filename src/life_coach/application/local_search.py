"""Local-only, current-revision diary indexing behind explicit SEARCH consent.

Projection build fences are audit metadata. Every read checks live source identity
and current consent instead of treating a later unrelated diary as index expiry.
The legacy projection reader and model-execution fences remain unchanged.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from life_coach.ai.diary_chunks import CHUNK_VERSION, split_diary
from life_coach.ai.retrieval import tokenize_lexical
from life_coach.modules.consent import ConsentPurpose
from life_coach.modules.consent.service import resolve_consent, resolve_source_consents
from life_coach.modules.identity import DataClass, capture_snapshot, get_vault
from life_coach.modules.identity.models import Vault
from life_coach.modules.sources.models import (
    IndexPolicy,
    SearchProjection,
    SourceDocument,
    SourceFragment,
    SourceRevision,
    SourceType,
)
from life_coach.modules.sources.service import create_search_projection

if TYPE_CHECKING:
    from life_coach.application.source_entries import SourceContentProtector

TOKENIZER_VERSION = CHUNK_VERSION
MAX_SCAN = 1000
MAX_INDEX_CHARACTERS = 100_000
REBUILD_BATCH = 50


class SearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)
    entry_id: uuid.UUID
    revision: int
    fragment_id: uuid.UUID
    excerpt: str
    quote_start: int
    quote_end: int
    passage_start: int = 0
    passage_end: int = 0


class SearchResults(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: list[SearchHit]
    scanned: int
    indexed: int
    truncated: bool
    rebuilt: int = 0
    pending: int = 0
    mode: str = "local_lexical"


def index_diary_fragment(
    session: Session,
    *,
    vault_id: uuid.UUID,
    fragment_id: uuid.UUID,
    plaintext: str,
) -> bool:
    """Index one newly written fragment in its caller's transaction; never call AI."""
    row = session.execute(
        _sources(vault_id).where(SourceFragment.id == fragment_id)
    ).one_or_none()
    if row is None:
        return False
    document, _, fragment = row
    consent = resolve_consent(
        session, vault_id=vault_id, purpose=ConsentPurpose.SEARCH,
        source_document_id=document.id,
    )
    if not consent.allowed or not _eligible(document, fragment, plaintext):
        return False
    projection = create_search_projection(
        session,
        vault_id=vault_id,
        source_fragment_id=fragment.id,
        index_policy=IndexPolicy.LEXICAL,
        lexical_terms=sorted(set(tokenize_lexical(plaintext))),
        tokenizer_version=TOKENIZER_VERSION,
        snapshot=capture_snapshot(session, vault_id),
    )
    projection.lexical_passages = [asdict(passage) for passage in split_diary(plaintext)]
    session.flush()
    return True


def _eligible(document: SourceDocument, fragment: SourceFragment, plaintext: str) -> bool:
    return (
        document.data_class is not DataClass.HIGHLY_SENSITIVE
        and fragment.data_class is not DataClass.HIGHLY_SENSITIVE
        and len(plaintext) <= MAX_INDEX_CHARACTERS
        and hashlib.sha256(plaintext.encode("utf-8")).hexdigest() == fragment.text_hash
    )


def _sources(vault_id: uuid.UUID) -> Select[tuple[SourceDocument, SourceRevision, SourceFragment]]:
    return (
        select(SourceDocument, SourceRevision, SourceFragment)
        .join(SourceRevision, (SourceRevision.vault_id == SourceDocument.vault_id)
              & (SourceRevision.id == SourceDocument.current_revision_id))
        .join(SourceFragment, (SourceFragment.vault_id == SourceRevision.vault_id)
              & (SourceFragment.revision_id == SourceRevision.id))
        .where(
            SourceDocument.vault_id == vault_id,
            SourceDocument.deleted_at.is_(None),
            SourceRevision.deleted_at.is_(None),
            SourceFragment.deleted_at.is_(None),
            SourceDocument.source_type == SourceType.NOTE,
        )
    )


def search_diaries(
    session: Session,
    *,
    vault_id: uuid.UUID,
    query: str,
    protector: SourceContentProtector,
    limit: int = 10,
    rebuild: bool = False,
) -> SearchResults:
    """Bounded local search/rebuild. No body is decrypted without current consent.

    Results intentionally expose coverage: this baseline scans at most 1000
    current fragments. It never claims exhaustive history or semantic recall.
    """
    get_vault(session, vault_id)
    # Serialize with source/consent mutations until this short local read commits.
    session.execute(select(Vault.id).where(Vault.id == vault_id).with_for_update())
    rows = session.execute(
        _sources(vault_id)
        .order_by(SourceDocument.created_at.desc(), SourceDocument.id, SourceFragment.ordinal)
        .limit(MAX_SCAN + 1)
    ).all()
    truncated = len(rows) > MAX_SCAN
    rows = rows[:MAX_SCAN]
    consents = resolve_source_consents(
        session, vault_id=vault_id, purpose=ConsentPurpose.SEARCH,
        source_document_ids={row[0].id for row in rows},
    )
    projections = {
        projection.source_fragment_id: projection
        for projection in session.scalars(
            select(SearchProjection).where(
                SearchProjection.vault_id == vault_id,
                SearchProjection.source_fragment_id.in_([row[2].id for row in rows]),
                SearchProjection.deleted_at.is_(None),
            )
        )
    }
    query_terms = set(tokenize_lexical(query))
    ranked: list[tuple[float, int, SearchHit]] = []
    indexed = 0
    rebuilt = pending = 0
    for ordinal, (document, revision, fragment) in enumerate(rows):
        consent = consents.get(document.id)
        if consent is None or not consent.allowed:
            continue
        if document.data_class is DataClass.HIGHLY_SENSITIVE:
            continue
        if fragment.data_class is DataClass.HIGHLY_SENSITIVE:
            continue
        projection = projections.get(fragment.id)
        current = (
            projection is not None
            and projection.consent_record_id == consent.record_id
            and projection.tokenizer_version == TOKENIZER_VERSION
            and projection.index_policy in {IndexPolicy.LEXICAL, IndexPolicy.BOTH}
            and projection.lexical_passages is not None
        )
        plaintext: str | None = None
        if rebuild and not current:
            if fragment.char_end is not None and fragment.char_end > MAX_INDEX_CHARACTERS:
                continue
            if rebuilt >= REBUILD_BATCH:
                pending += 1
                continue
            plaintext = protector.open(
                vault_id=vault_id, document_id=document.id, object_id=fragment.id,
                revision_no=revision.revision_no, kind="fragment",
                ciphertext=fragment.text_ciphertext,
            )
            if index_diary_fragment(
                session, vault_id=vault_id, fragment_id=fragment.id, plaintext=plaintext,
            ):
                indexed += 1
                rebuilt += 1
            continue
        if not current or projection is None:
            if not rebuild:
                pending += 1
            continue
        indexed += 1
        terms = set(projection.lexical_terms or ())
        overlap = query_terms & terms
        if not overlap:
            continue
        plaintext = protector.open(
            vault_id=vault_id, document_id=document.id, object_id=fragment.id,
            revision_no=revision.revision_no, kind="fragment",
            ciphertext=fragment.text_ciphertext,
        )
        if not _eligible(document, fragment, plaintext):
            continue
        # Verify an index hit against the original, including corrupted projections.
        overlap &= set(tokenize_lexical(plaintext))
        if not overlap:
            continue
        # Rebuild deterministic boundaries from verified original text. Stored offsets
        # are derived data, never trusted as a substitute for the original evidence.
        passages = split_diary(plaintext)
        scored = [(sum(2 if len(term) > 1 else 1 for term in query_terms & set(p.terms)), p)
                  for p in passages]
        weight, passage = max(scored, key=lambda item: item[0])
        if not weight:
            continue
        positions = [plaintext.find(term, passage.start, passage.end) for term in overlap]
        positions = [position for position in positions if position >= 0]
        start = max(passage.start, min(positions, default=passage.start) - 40)
        end = min(passage.end, start + 240)
        score = weight / max(1, len(query_terms))
        ranked.append((score, ordinal, SearchHit(
            entry_id=document.id, revision=revision.revision_no, fragment_id=fragment.id,
            excerpt=plaintext[start:end], quote_start=start, quote_end=end,
            passage_start=passage.start, passage_end=passage.end,
        )))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return SearchResults(
        items=[item[2] for item in ranked[:max(1, min(limit, 50))]],
        scanned=len(rows), indexed=indexed, truncated=truncated, rebuilt=rebuilt, pending=pending,
    )
