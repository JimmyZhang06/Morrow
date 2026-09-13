# ruff: noqa: RUF001
"""Conversation authority and encrypted persistence through the governed model runtime."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator
from sqlalchemy import Row, select
from sqlalchemy.orm import Session

from life_coach.ai.chat_stream import ChatStream, get_stream
from life_coach.ai.contracts import ModelInputKind, ModelInputRef
from life_coach.application.conversation_memories import reviewed_context
from life_coach.application.local_search import search_diaries
from life_coach.application.model_gateway import (
    ModelInvocationDenied,
    ModelTaskContextSnapshot,
    SourceConsentAuthority,
)
from life_coach.application.model_runtime import ModelResultContext, ModelResultRejected
from life_coach.application.source_entries import (
    ProtectedSourceFragmentPlaintextReader,
    SourceContentProtector,
)
from life_coach.modules.consent import ConsentAction, ConsentPurpose
from life_coach.modules.consent.exceptions import ConsentDenied
from life_coach.modules.consent.service import (
    UserConsentCommand,
    grant_consent,
    require_consent,
    resolve_consent,
)
from life_coach.modules.conversations import Conversation, ConversationMaterial, ConversationTurn
from life_coach.modules.identity import capture_snapshot
from life_coach.modules.identity.models import DataClass, Vault
from life_coach.modules.model_runs.contracts import ModelRunArtifactKind, ModelRunArtifactSpec
from life_coach.modules.model_runs.models import ModelRun
from life_coach.modules.sources.models import (
    FragmentKind,
    SourceDocument,
    SourceFragment,
    SourceRevision,
    SourceType,
)
from life_coach.modules.sources.service import (
    create_source_document,
    create_source_fragment,
    tombstone_source_document,
)
from life_coach.platform.database import VaultAsyncSession
from life_coach.shared.database import utc_now

CHAT_TASK = "conversation_reply"
MAX_TURNS = 100
MAX_CONTEXT_CHARS = 120000
MAX_CONTEXT_FRAGMENTS = 256


class ConversationContextLimit(ModelInvocationDenied):
    pass


class ReplyCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_fragment_id: uuid.UUID
    quote: str = Field(min_length=1, max_length=500)


class ConversationReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str = Field(min_length=1, max_length=24000)
    uncertainty: str = Field(
        default="AI 回应仅供参考；如有引用，仅显示已核对的原文。", max_length=1000
    )
    citations: list[ReplyCitation] = Field(default_factory=list, max_length=12)
    title: str | None = None
    care_letter: str | None = Field(default=None, max_length=900)

    @field_validator("uncertainty", mode="before")
    @classmethod
    def clean_uncertainty(cls, value: object) -> str:
        return (
            value.strip()[:1000]
            if isinstance(value, str) and value.strip()
            else "AI 回应仅供参考；如有引用，仅显示已核对的原文。"
        )

    @field_validator("citations", mode="before")
    @classmethod
    def clean_citations(cls, value: object) -> list[ReplyCitation]:
        if not isinstance(value, list):
            return []
        result = []
        for item in value[:12]:
            try:
                result.append(ReplyCitation.model_validate(item))
            except ValueError:
                continue
        return result

    @field_validator("care_letter", mode="before")
    @classmethod
    def clean_care_letter(cls, value: object) -> str | None:
        return value.strip() or None if isinstance(value, str) and len(value) <= 900 else None

    @field_validator("title", mode="before")
    @classmethod
    def clean_title(cls, value: object) -> str | None:
        # Optional metadata must never invalidate an otherwise valid answer.
        return " ".join(value.split())[:60] or None if isinstance(value, str) else None


class ConversationTitleConflict(Exception):
    pass


def rename_conversation(
    session: Session,
    *,
    vault_id: uuid.UUID,
    conversation_id: uuid.UUID,
    title: str,
    expected_revision: int,
    protector: SourceContentProtector,
) -> None:
    title = " ".join(title.split())
    if not title or len(title) > 60:
        raise ValueError("title must contain 1 to 60 characters")
    chat = lock_chat(session, vault_id, conversation_id)
    if chat.title_revision != expected_revision:
        raise ConversationTitleConflict
    chat.title_revision += 1
    chat.title_ciphertext = protector.seal(
        vault_id=vault_id,
        document_id=chat.id,
        object_id=chat.id,
        revision_no=chat.title_revision,
        kind="revision",
        plaintext=title,
    )
    session.flush()


def lock_chat(session: Session, vault_id: uuid.UUID, conversation_id: uuid.UUID) -> Conversation:
    session.execute(select(Vault.id).where(Vault.id == vault_id).with_for_update())
    chat = session.scalar(
        select(Conversation).where(
            Conversation.vault_id == vault_id,
            Conversation.id == conversation_id,
            Conversation.deleted_at.is_(None),
        )
    )
    if chat is None:
        raise ModelInvocationDenied("conversation unavailable")
    return chat


def source_rows(
    session: Session, vault_id: uuid.UUID, ids: list[uuid.UUID]
) -> dict[uuid.UUID, Row[tuple[SourceFragment, SourceRevision, SourceDocument]]]:
    rows = session.execute(
        select(SourceFragment, SourceRevision, SourceDocument)
        .join(
            SourceRevision,
            (SourceRevision.id == SourceFragment.revision_id)
            & (SourceRevision.vault_id == SourceFragment.vault_id),
        )
        .join(
            SourceDocument,
            (SourceDocument.id == SourceRevision.document_id)
            & (SourceDocument.vault_id == SourceRevision.vault_id),
        )
        .where(
            SourceFragment.vault_id == vault_id,
            SourceFragment.id.in_(ids),
            SourceFragment.deleted_at.is_(None),
            SourceRevision.deleted_at.is_(None),
            SourceDocument.deleted_at.is_(None),
            SourceDocument.current_revision_id == SourceRevision.id,
        )
    ).all()
    if {row[0].id for row in rows} != set(ids):
        raise ModelInvocationDenied("conversation sources changed")
    return {row[0].id: row for row in rows}


def open_fragment(
    protector: SourceContentProtector,
    vault_id: uuid.UUID,
    row: Row[tuple[SourceFragment, SourceRevision, SourceDocument]],
) -> str:
    fragment, revision, document = row
    value = protector.open(
        vault_id=vault_id,
        document_id=document.id,
        object_id=fragment.id,
        revision_no=revision.revision_no,
        kind="fragment",
        ciphertext=fragment.text_ciphertext,
    )
    if hashlib.sha256(value.encode()).hexdigest() != fragment.text_hash:
        raise ModelInvocationDenied("conversation source integrity failed")
    return value


def new_conversation(
    session: Session,
    *,
    vault_id: uuid.UUID,
    principal_id: uuid.UUID,
    entry_ids: list[uuid.UUID],
    allow_history: bool,
    include_reviewed_memories: bool = False,
) -> Conversation:
    session.execute(select(Vault.id).where(Vault.id == vault_id).with_for_update())
    if not allow_history:
        raise ModelInvocationDenied("explicit conversation permission required")
    passive = require_consent(session, vault_id=vault_id, purpose=ConsentPurpose.PASSIVE_QA)
    now = utc_now()
    # Copy the already chosen provider restrictions, never widen them for chat.
    grant_consent(
        session,
        command=UserConsentCommand(
            vault_id=vault_id,
            principal_id=principal_id,
            purpose=ConsentPurpose.CROSS_RECORD_ANALYSIS,
            action=ConsentAction.GRANT,
            interaction_id=uuid.uuid4(),
            issued_at=now,
            expires_at=now + timedelta(minutes=1),
            provider_policy=passive.provider_policy,
        ),
    )
    rows = session.execute(
        select(SourceFragment, SourceDocument)
        .join(
            SourceRevision,
            (SourceRevision.id == SourceFragment.revision_id)
            & (SourceRevision.vault_id == SourceFragment.vault_id),
        )
        .join(
            SourceDocument,
            (SourceDocument.current_revision_id == SourceRevision.id)
            & (SourceDocument.vault_id == SourceRevision.vault_id),
        )
        .where(
            SourceDocument.vault_id == vault_id,
            SourceDocument.id.in_(entry_ids),
            SourceDocument.source_type == SourceType.NOTE,
            SourceDocument.deleted_at.is_(None),
            SourceFragment.deleted_at.is_(None),
        )
        .order_by(SourceDocument.created_at, SourceFragment.ordinal)
    ).all()
    if {row[1].id for row in rows} != set(entry_ids) or len(rows) > 8:
        raise ModelInvocationDenied("selected diaries unavailable")
    for fragment, document in rows:
        if DataClass.HIGHLY_SENSITIVE in (fragment.data_class, document.data_class):
            raise ModelInvocationDenied("selected diaries unavailable")
        for purpose in (ConsentPurpose.PASSIVE_QA, ConsentPurpose.CROSS_RECORD_ANALYSIS):
            require_consent(
                session, vault_id=vault_id, purpose=purpose, source_document_id=document.id
            )
    chat = Conversation(vault_id=vault_id, include_reviewed_memories=include_reviewed_memories)
    session.add(chat)
    session.flush()
    for i, (fragment, _) in enumerate(rows):
        session.add(
            ConversationMaterial(
                vault_id=vault_id, conversation_id=chat.id, fragment_id=fragment.id, position=i + 1
            )
        )
    session.flush()
    return chat


def enqueue_turn(
    session: Session,
    *,
    vault_id: uuid.UUID,
    conversation_id: uuid.UUID,
    principal_id: uuid.UUID,
    membership_generation: int,
    request_id: uuid.UUID,
    question: str,
    model_binding: str,
    protector: SourceContentProtector,
    auto_retrieve: bool = False,
    experience: Literal["conversation", "past_letter"] = "conversation",
) -> ConversationTurn:
    if experience == "past_letter":
        auto_retrieve = True
    lock_chat(session, vault_id, conversation_id)
    turns = list(
        session.scalars(
            select(ConversationTurn)
            .where(
                ConversationTurn.vault_id == vault_id,
                ConversationTurn.conversation_id == conversation_id,
            )
            .order_by(ConversationTurn.position)
        )
    )
    for turn in turns:
        if turn.request_id == request_id:
            previous = open_fragment(
                protector,
                vault_id,
                source_rows(session, vault_id, [turn.question_fragment_id])[
                    turn.question_fragment_id
                ],
            )
            if (
                previous != question
                or bool(turn.retrieval) != auto_retrieve
                or (turn.retrieval or {}).get("experience", "conversation") != experience
            ):
                raise ModelInvocationDenied("request identifier reused")
            return turn
    if len(turns) >= MAX_TURNS or any(turn.state in ("queued", "running") for turn in turns):
        raise ModelInvocationDenied("conversation busy or full")
    retrieval = None
    if auto_retrieve:
        search_query = question
        follows_previous = len(question.strip()) <= 30 and any(
            word in question for word in ("继续", "为什么", "那我", "这件事", "刚才", "怎么办")
        )
        if follows_previous and turns:
            prior_id = turns[-1].question_fragment_id
            search_query = (
                open_fragment(
                    protector, vault_id, source_rows(session, vault_id, [prior_id])[prior_id]
                )[:500]
                + " "
                + question
            )
        pinned = list(
            session.scalars(
                select(ConversationMaterial.fragment_id)
                .where(
                    ConversationMaterial.vault_id == vault_id,
                    ConversationMaterial.conversation_id == conversation_id,
                )
                .order_by(ConversationMaterial.position)
            )
        )
        found = search_diaries(
            session, vault_id=vault_id, query=search_query, protector=protector, limit=50
        )
        picked = list(pinned)
        characters = sum(
            len(open_fragment(protector, vault_id, row))
            for row in source_rows(session, vault_id, pinned).values()
        )
        for hit in found.items:
            if len(picked) >= 8:
                break
            if hit.fragment_id in picked or any(
                not resolve_consent(
                    session,
                    vault_id=vault_id,
                    purpose=purpose,
                    source_document_id=hit.entry_id,
                ).allowed
                for purpose in (ConsentPurpose.PASSIVE_QA, ConsentPurpose.CROSS_RECORD_ANALYSIS)
            ):
                continue
            row = source_rows(session, vault_id, [hit.fragment_id])[hit.fragment_id]
            content = open_fragment(protector, vault_id, row)
            if characters + len(content) > 14000:
                continue
            picked.append(hit.fragment_id)
            characters += len(content)
        retrieval = {
            "fragment_ids": [str(key) for key in picked],
            "automatic_fragment_ids": [str(key) for key in picked if key not in pinned],
            "matched": len(picked) - len(pinned),
            "scanned": found.scanned,
            "indexed": found.indexed,
            "pending": found.pending,
            "truncated": found.truncated,
            "mode": "local_lexical",
            "experience": experience,
            "used_previous_question": follows_previous and bool(turns),
        }
    doc_id, revision_id, fragment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    digest = hashlib.sha256(question.encode()).hexdigest()
    create_source_document(
        session,
        vault_id=vault_id,
        document_id=doc_id,
        revision_id=revision_id,
        source_type=SourceType.CONVERSATION,
        content_hash=digest,
        content_mime="text/plain",
        content_ciphertext=protector.seal(
            vault_id=vault_id,
            document_id=doc_id,
            object_id=revision_id,
            revision_no=1,
            kind="revision",
            plaintext=question,
        ),
    )
    create_source_fragment(
        session,
        vault_id=vault_id,
        revision_id=revision_id,
        fragment_id=fragment_id,
        ordinal=0,
        char_start=0,
        char_end=len(question),
        fragment_kind=FragmentKind.PARAGRAPH,
        text_hash=digest,
        text_ciphertext=protector.seal(
            vault_id=vault_id,
            document_id=doc_id,
            object_id=fragment_id,
            revision_no=1,
            kind="fragment",
            plaintext=question,
        ),
    )
    snapshot = capture_snapshot(session, vault_id)
    turn = ConversationTurn(
        vault_id=vault_id,
        conversation_id=conversation_id,
        request_id=request_id,
        question_fragment_id=fragment_id,
        principal_id=principal_id,
        membership_generation=membership_generation,
        position=len(turns) + 1,
        policy_epoch=snapshot.policy_epoch,
        source_generation=snapshot.source_generation,
        model_binding=model_binding,
        retrieval=retrieval,
    )
    session.add(turn)
    session.flush()
    # Fail within the send transaction, before a worker or a provider is dispatched.
    ConversationAuthority(protector).prepare(
        session=session, vault_id=vault_id, context_id=turn.id, allow_queued=True
    )
    return turn


class ConversationAuthority:
    def __init__(self, protector: SourceContentProtector) -> None:
        self.protector = protector
        self.sources = SourceConsentAuthority(ProtectedSourceFragmentPlaintextReader(protector))

    def prepare(
        self,
        *,
        session: Session,
        vault_id: uuid.UUID,
        context_id: uuid.UUID,
        allow_queued: bool = False,
    ) -> ModelTaskContextSnapshot:
        session.execute(select(Vault.id).where(Vault.id == vault_id).with_for_update())
        turn = session.scalar(
            select(ConversationTurn).where(
                ConversationTurn.vault_id == vault_id, ConversationTurn.id == context_id
            )
        )
        if turn is None or turn.state not in (
            ("running", "queued") if allow_queued else ("running",)
        ):
            raise ModelInvocationDenied("conversation turn unavailable")
        chat = lock_chat(session, vault_id, turn.conversation_id)
        # Material revisions and each effective consent are checked below. A new
        # unrelated diary/chat must not invalidate a queued or running turn.
        material_ids = list(
            session.scalars(
                select(ConversationMaterial.fragment_id)
                .where(
                    ConversationMaterial.vault_id == vault_id,
                    ConversationMaterial.conversation_id == turn.conversation_id,
                )
                .order_by(ConversationMaterial.position)
            )
        )
        if turn.retrieval is not None:
            material_ids = [uuid.UUID(value) for value in turn.retrieval["fragment_ids"]]
        memories = (
            reviewed_context(
                session, vault_id=vault_id, material_ids=material_ids, protector=self.protector
            )
            if chat.include_reviewed_memories
            else None
        )
        previous = list(
            session.scalars(
                select(ConversationTurn)
                .where(
                    ConversationTurn.vault_id == vault_id,
                    ConversationTurn.conversation_id == turn.conversation_id,
                    ConversationTurn.position < turn.position,
                )
                .order_by(ConversationTurn.position)
            )
        )
        # Check current sources before opening any historical plaintext.
        current_ids = list(
            dict.fromkeys(
                [
                    *material_ids,
                    *(memories.fragment_ids if memories else ()),
                    *(item.question_fragment_id for item in previous),
                    turn.question_fragment_id,
                ]
            )
        )
        try:
            for _, _, document in source_rows(session, vault_id, current_ids).values():
                for purpose in (ConsentPurpose.PASSIVE_QA, ConsentPurpose.CROSS_RECORD_ANALYSIS):
                    require_consent(
                        session, vault_id=vault_id, purpose=purpose, source_document_id=document.id
                    )
        except ConsentDenied as exc:
            raise ModelInvocationDenied("conversation source permission unavailable") from exc
        visible = conversation_view(
            session,
            vault_id=vault_id,
            conversation_id=chat.id,
            protector=self.protector,
            include_preview=False,
        )
        visible_turns = {
            item["id"]: item for item in cast(list[dict[str, object]], visible["turns"])
        }
        historical_ids: list[uuid.UUID] = []
        history = []
        for item in previous:
            prior = visible_turns[str(item.id)]
            answer = prior["reply"]
            partial = prior.get("partial")
            if answer is not None or partial is not None:
                old_materials = (
                    [uuid.UUID(value) for value in item.retrieval["fragment_ids"]]
                    if item.retrieval
                    else list(
                        session.scalars(
                            select(ConversationMaterial.fragment_id).where(
                                ConversationMaterial.vault_id == vault_id,
                                ConversationMaterial.conversation_id == chat.id,
                            )
                        )
                    )
                )
                historical_ids.extend(old_materials)
            history.append(
                {
                    "turn": item.position,
                    "question": prior["question"],
                    "assistant": answer if answer is not None else partial,
                    "assistant_state": (
                        "available"
                        if answer is not None
                        else "incomplete_unverified"
                        if partial is not None
                        else "unavailable"
                    ),
                }
            )
        ids = list(
            dict.fromkeys(
                [
                    *material_ids,
                    *(memories.fragment_ids if memories else ()),
                    *historical_ids,
                    *(item.question_fragment_id for item in previous),
                    turn.question_fragment_id,
                ]
            )
        )
        if len(ids) + 1 + (len(memories.input_refs) if memories else 0) > MAX_CONTEXT_FRAGMENTS:
            raise ConversationContextLimit("conversation fragment budget exceeded")
        rows = source_rows(session, vault_id, ids)
        # Recheck search authorization as well as both model purposes.
        try:
            if turn.retrieval:
                for key in turn.retrieval["automatic_fragment_ids"]:
                    require_consent(
                        session,
                        vault_id=vault_id,
                        purpose=ConsentPurpose.SEARCH,
                        source_document_id=rows[uuid.UUID(key)][2].id,
                    )
            for _, _, document in rows.values():
                for purpose in (ConsentPurpose.PASSIVE_QA, ConsentPurpose.CROSS_RECORD_ANALYSIS):
                    require_consent(
                        session,
                        vault_id=vault_id,
                        purpose=purpose,
                        source_document_id=document.id,
                    )
        except ConsentDenied as exc:
            raise ModelInvocationDenied("conversation source permission unavailable") from exc
        # Validate size before the provider boundary; full source text is bounded here.
        contents = {key: open_fragment(self.protector, vault_id, row) for key, row in rows.items()}
        from life_coach.application.care_letters import eligible, permits_private_diaries

        care_allowed = all(
            row[2].source_type == SourceType.CONVERSATION
            or row[2].data_class == DataClass.NORMAL
            or (
                row[2].data_class == DataClass.SENSITIVE
                and permits_private_diaries(session, vault_id)
            )
            for row in rows.values()
        ) and eligible(session, vault_id, [row[2].id for row in rows.values()])
        if chat.care_origin and not care_allowed:
            raise ModelInvocationDenied("care no longer authorized")
        data = {
            "care_allowed": care_allowed,
            "care_check_only": chat.care_origin,
            "question": contents[turn.question_fragment_id],
            "experience": (turn.retrieval or {}).get("experience", "conversation"),
            "history": history,
            "history_dependencies": [
                str(item.id)
                for item in previous
                if visible_turns[str(item.id)]["reply"] is not None
                or visible_turns[str(item.id)].get("partial") is not None
            ],
            "history_coverage": {
                "previous_turns": len(previous),
                "included_turns": len(history),
                "truncated": False,
            },
            "historical_diary_fragments": list(dict.fromkeys(str(key) for key in historical_ids)),
            "diary_fragments": [str(key) for key in material_ids],
            "diary_recorded_at": {
                str(key): rows[key][2].created_at.isoformat() for key in material_ids
            },
            "coverage": turn.retrieval or {"mode": "selected_diaries"},
            "reviewed_memories": memories.items if memories else [],
            "memory_fingerprint": memories.fingerprint if memories else None,
        }
        canonical = json.dumps(
            {"turn": str(turn.id), "data": data, "fragments": [str(i) for i in ids]},
            sort_keys=True,
            ensure_ascii=False,
        )
        if len(canonical) + sum(map(len, contents.values())) > MAX_CONTEXT_CHARS:
            raise ConversationContextLimit("conversation history budget exceeded")
        return ModelTaskContextSnapshot(
            context_id=turn.id,
            input_ref=ModelInputRef(
                vault_id=str(vault_id),
                kind=ModelInputKind.SOURCE_REVISION,
                object_id=str(rows[turn.question_fragment_id][1].id),
            ),
            content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
            source_fragment_ids=tuple(ids),
            data=cast(JsonValue, data),
            additional_input_refs=memories.input_refs if memories else (),
        )

    def assert_current(self, *, session: Session, snapshot: ModelTaskContextSnapshot) -> None:
        if (
            self.prepare(
                session=session,
                vault_id=uuid.UUID(snapshot.input_ref.vault_id),
                context_id=snapshot.context_id,
            )
            != snapshot
        ):
            raise ModelInvocationDenied("conversation context changed")


class ConversationPersister:
    def __init__(self, authority: ConversationAuthority) -> None:
        self.authority = authority

    async def persist(
        self, session: VaultAsyncSession, *, context: ModelResultContext, result: BaseModel
    ) -> ModelRunArtifactSpec:
        snapshot = context.prepared.context_snapshot
        if snapshot is None or type(result) is not ConversationReply:
            raise ModelResultRejected("conversation result unavailable")
        self_snapshot = snapshot

        def save(db: Session) -> None:
            self.authority.assert_current(session=db, snapshot=self_snapshot)
            by_id = {
                fragment.fragment_id: fragment for fragment in context.prepared.snapshot.fragments
            }
            citations = []
            data = cast(dict[str, JsonValue], self_snapshot.data)
            is_letter = data.get("experience") == "past_letter"
            diary_ids = cast(list[str], data.get("diary_fragments", []))
            rejected_citations = 0
            for citation in result.citations:
                if is_letter and str(citation.source_fragment_id) not in diary_ids:
                    rejected_citations += 1
                    continue
                fragment = by_id.get(citation.source_fragment_id)
                if fragment is None or citation.quote not in fragment.text:
                    rejected_citations += 1
                    continue
                start = fragment.text.index(citation.quote)
                citations.append(
                    {
                        "fragment_id": str(fragment.fragment_id),
                        "entry_id": str(fragment.document_id),
                        "quote": citation.quote,
                        "start": start,
                        "end": start + len(citation.quote),
                    }
                )
            if is_letter and diary_ids and not citations:
                raise ModelResultRejected("past letter requires original diary evidence")
            turn = db.get(ConversationTurn, self_snapshot.context_id)
            if turn is None:
                raise ModelResultRejected("conversation turn unavailable")
            care_letter = (
                result.care_letter
                if cast(dict[str, JsonValue], self_snapshot.data).get("care_allowed")
                else None
            )
            if care_letter and care_letter.strip():
                from life_coach.application.care_letters import offered

                offered(db, context.vault_id, turn)
            # Quotes are kept inside the same encrypted envelope, not the manifest.
            turn.answer_ciphertext = self.authority.protector.seal(
                vault_id=context.vault_id,
                document_id=turn.conversation_id,
                object_id=turn.id,
                revision_no=1,
                kind="revision",
                plaintext=json.dumps(
                    {
                        "answer": result.answer,
                        "care_letter": care_letter,
                        "history_dependencies": cast(dict[str, JsonValue], self_snapshot.data).get(
                            "history_dependencies", []
                        ),
                        "title": result.title,
                        "uncertainty": (
                            "部分引用未能与原文核对，已隐藏。正文是 AI 的回应，不代表已确认的事实。"
                            if rejected_citations
                            else result.uncertainty
                        ),
                        "citations": citations,
                        "memory_fingerprint": cast(dict[str, JsonValue], self_snapshot.data).get(
                            "memory_fingerprint"
                        ),
                        "reviewed_memories": cast(dict[str, JsonValue], self_snapshot.data).get(
                            "reviewed_memories", []
                        ),
                    },
                    ensure_ascii=False,
                ),
            )
            turn.citations = [
                {k: v for k, v in citation.items() if k != "quote"} for citation in citations
            ]
            turn.state = "completed"
            db.flush()

        await session.run_sync(save)
        return ModelRunArtifactSpec(
            vault_id=context.vault_id,
            artifact_kind=ModelRunArtifactKind.CONVERSATION,
            conversation_turn_id=snapshot.context_id,
        )


def preserve_interrupted_answer(
    session: Session,
    *,
    vault_id: uuid.UUID,
    turn_id: uuid.UUID,
    protector: SourceContentProtector,
    stream: ChatStream,
) -> None:
    """Keep received prose separately from validated answers, after live authorization."""
    snapshot = ConversationAuthority(protector).prepare(
        session=session, vault_id=vault_id, context_id=turn_id
    )
    answer = stream.preview(snapshot.content_hash)
    turn = session.get(ConversationTurn, turn_id)
    if not answer or turn is None or turn.state != "running":
        return
    data = cast(dict[str, JsonValue], snapshot.data)
    turn.answer_ciphertext = protector.seal(
        vault_id=vault_id,
        document_id=turn.conversation_id,
        object_id=turn.id,
        revision_no=1,
        kind="revision",
        plaintext=json.dumps(
            {
                "partial": True,
                "answer": answer,
                "citations": [],
                "memory_fingerprint": data.get("memory_fingerprint"),
                "history_dependencies": data.get("history_dependencies", []),
            },
            ensure_ascii=False,
        ),
    )
    session.flush()


def delete_conversation(
    session: Session, *, vault_id: uuid.UUID, conversation_id: uuid.UUID
) -> None:
    chat = lock_chat(session, vault_id, conversation_id)
    turns = list(
        session.scalars(
            select(ConversationTurn).where(
                ConversationTurn.vault_id == vault_id,
                ConversationTurn.conversation_id == conversation_id,
            )
        )
    )
    for turn in turns:
        turn.state = "canceled"
        turn.answer_ciphertext = None
        turn.citations = None
        fragment = session.get(SourceFragment, turn.question_fragment_id)
        if fragment is not None and fragment.deleted_at is None:
            revision = session.get(SourceRevision, fragment.revision_id)
            if revision is not None:
                tombstone_source_document(
                    session, vault_id=vault_id, document_id=revision.document_id
                )
    chat.title_ciphertext = None
    chat.deleted_at = utc_now()


def conversation_view(
    session: Session,
    *,
    vault_id: uuid.UUID,
    conversation_id: uuid.UUID,
    protector: SourceContentProtector,
    summary_only: bool = False,
    include_preview: bool = True,
) -> dict[str, object]:
    chat = lock_chat(session, vault_id, conversation_id)
    manual_title = (
        protector.open(
            vault_id=vault_id,
            document_id=chat.id,
            object_id=chat.id,
            revision_no=chat.title_revision,
            kind="revision",
            ciphertext=chat.title_ciphertext,
        )
        if chat.title_ciphertext is not None
        else None
    )
    summary = {
        "id": str(chat.id),
        "created_at": chat.created_at.isoformat(),
        "title": manual_title or "一段新的对话",
        "title_revision": chat.title_revision,
        "title_source": "manual" if manual_title else "default",
    }
    if summary_only and manual_title:
        return summary
    turns = list(
        session.scalars(
            select(ConversationTurn)
            .where(
                ConversationTurn.vault_id == vault_id,
                ConversationTurn.conversation_id == conversation_id,
            )
            .order_by(ConversationTurn.position)
        )
    )
    materials = list(
        session.scalars(
            select(ConversationMaterial.fragment_id)
            .where(
                ConversationMaterial.vault_id == vault_id,
                ConversationMaterial.conversation_id == conversation_id,
            )
            .order_by(ConversationMaterial.position)
        )
    )
    blocked = False
    source_ids = []
    try:
        rows = source_rows(
            session, vault_id, materials + [turn.question_fragment_id for turn in turns]
        )
        for _, _, document in rows.values():
            for purpose in (ConsentPurpose.PASSIVE_QA, ConsentPurpose.CROSS_RECORD_ANALYSIS):
                require_consent(
                    session, vault_id=vault_id, purpose=purpose, source_document_id=document.id
                )
        source_ids = list(dict.fromkeys(str(rows[key][2].id) for key in materials))
    except (ModelInvocationDenied, ConsentDenied):
        blocked = True
    memories = (
        reviewed_context(session, vault_id=vault_id, material_ids=materials, protector=protector)
        if chat.include_reviewed_memories and not blocked
        else None
    )
    result: list[dict[str, object]] = []
    failures = {
        key: code
        for key, code in session.execute(
            select(ModelRun.idempotency_key, ModelRun.safe_error_code).where(
                ModelRun.vault_id == vault_id,
                ModelRun.task_type == "conversation_reply",
                ModelRun.idempotency_key.in_([str(turn.id) for turn in turns]),
                ModelRun.safe_error_code.is_not(None),
            )
        ).all()
    }
    for turn in turns:
        turn_blocked = blocked
        turn_materials = materials
        if turn.retrieval is not None:
            turn_materials = [uuid.UUID(value) for value in turn.retrieval["fragment_ids"]]
            try:
                for fragment, _, document in source_rows(
                    session, vault_id, turn_materials
                ).values():
                    purposes = [ConsentPurpose.PASSIVE_QA, ConsentPurpose.CROSS_RECORD_ANALYSIS]
                    if str(fragment.id) in turn.retrieval["automatic_fragment_ids"]:
                        purposes.append(ConsentPurpose.SEARCH)
                    for purpose in purposes:
                        require_consent(
                            session,
                            vault_id=vault_id,
                            purpose=purpose,
                            source_document_id=document.id,
                        )
            except (ModelInvocationDenied, ConsentDenied):
                turn_blocked = True
        turn_memories = (
            reviewed_context(
                session, vault_id=vault_id, material_ids=turn_materials, protector=protector
            )
            if chat.include_reviewed_memories and not turn_blocked
            else None
        )
        question = "原始消息已不可用"
        try:
            question = open_fragment(
                protector,
                vault_id,
                source_rows(session, vault_id, [turn.question_fragment_id])[
                    turn.question_fragment_id
                ],
            )
        except ModelInvocationDenied:
            blocked = True
        payload = None
        if (
            not blocked
            and not turn_blocked
            and turn.state in ("completed", "failed", "unknown")
            and turn.answer_ciphertext is not None
        ):
            payload = json.loads(
                protector.open(
                    vault_id=vault_id,
                    document_id=conversation_id,
                    object_id=turn.id,
                    revision_no=1,
                    kind="revision",
                    ciphertext=turn.answer_ciphertext,
                )
            )
        outdated = (
            payload is not None
            and turn_memories is not None
            and payload.get("memory_fingerprint") != turn_memories.fingerprint
        )
        if payload is not None:
            available = {
                item["id"]
                for item in result
                if item["reply"] is not None or item.get("partial") is not None
            }
            outdated = outdated or any(
                key not in available for key in payload.get("history_dependencies", [])
            )
        if outdated:
            payload = None
        if payload is not None:
            question_turns = {str(item.question_fragment_id): str(item.id) for item in turns}
            for citation in payload.get("citations", []):
                citation["turn_id"] = question_turns.get(citation["fragment_id"])
                citation["source_type"] = "conversation" if citation["turn_id"] else "note"
        preview = ""
        stream = get_stream(vault_id, turn.id)
        if (
            include_preview
            and not summary_only
            and stream is not None
            and turn.state == "running"
            and not blocked
            and not turn_blocked
        ):
            try:
                live_context = ConversationAuthority(protector).prepare(
                    session=session, vault_id=vault_id, context_id=turn.id
                )
                preview = stream.preview(live_context.content_hash)
            except (ModelInvocationDenied, ConsentDenied):
                stream.close()
        elif (
            include_preview
            and not summary_only
            and stream is not None
            and (blocked or turn_blocked or turn.state != "queued")
        ):
            # The worker can claim a queued turn after this view reads its state.
            # A stale queued snapshot must not cancel the newly started stream.
            stream.close()
        result.append(
            {
                "id": str(turn.id),
                "question": question,
                "state": "outdated" if outdated else turn.state,
                "failure_code": failures.get(str(turn.id)),
                "reply": payload if payload is not None and not payload.get("partial") else None,
                "partial": (
                    {"answer": payload["answer"]}
                    if payload is not None and payload.get("partial")
                    else None
                ),
                "preview": preview,
                "created_at": turn.created_at.isoformat(),
                "retrieval": turn.retrieval,
            }
        )
    if not manual_title:
        first_question = str(result[0]["question"]) if result else ""
        if first_question and first_question != "原始消息已不可用":
            summary.update(title=" ".join(first_question.split())[:60], title_source="question")
        for item in reversed(result):
            reply = item["reply"]
            if (
                isinstance(reply, dict)
                and isinstance(reply.get("title"), str)
                and reply["title"].strip()
            ):
                summary.update(title=reply["title"], title_source="ai")
                break
    if summary_only:
        return summary
    return {
        **summary,
        "turns": result,
        "blocked": blocked,
        "entry_ids": source_ids,
        "max_turns": MAX_TURNS,
        "include_reviewed_memories": chat.include_reviewed_memories,
        "reviewed_memory_count": len(memories.items) if memories else 0,
    }
