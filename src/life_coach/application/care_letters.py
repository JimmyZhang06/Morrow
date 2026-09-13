"""Opt-in, rate-limited care letters, stored inside encrypted conversation replies."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from life_coach.application.model_gateway import ModelInvocationDenied
from life_coach.application.source_entries import SourceContentProtector
from life_coach.modules.consent import ConsentAction, ConsentPurpose
from life_coach.modules.consent.exceptions import ConsentDenied
from life_coach.modules.consent.service import (
    UserConsentCommand,
    grant_consent,
    require_consent,
    revoke_consent,
)
from life_coach.modules.conversations import ConversationMaterial, ConversationTurn
from life_coach.modules.identity.models import DataClass, Vault
from life_coach.modules.sources.models import SourceDocument, SourceType
from life_coach.shared.database import utc_now


def owner(session: Session, vault_id: uuid.UUID) -> Vault:
    row = session.scalar(
        select(Vault).where(Vault.id == vault_id, Vault.deleted_at.is_(None)).with_for_update()
    )
    if row is None:
        raise ModelInvocationDenied("care unavailable")
    return row


def active(settings: dict[str, object]) -> bool:
    return settings.get("enabled") is True and (
        not settings.get("paused_until") or str(settings["paused_until"]) <= utc_now().isoformat()
    )


def eligible(session: Session, vault_id: uuid.UUID, document_ids: list[uuid.UUID]) -> bool:
    settings = owner(session, vault_id).care_settings or {}
    if not active(settings) or settings.get("pending"):
        return False
    last = settings.get("last_letter_at")
    if last and datetime.fromisoformat(str(last)) + timedelta(hours=72) > utc_now():
        return False
    try:
        require_consent(session, vault_id=vault_id, purpose=ConsentPurpose.PROACTIVE_RESURFACING)
        for doc_id in document_ids:
            require_consent(
                session,
                vault_id=vault_id,
                purpose=ConsentPurpose.PROACTIVE_RESURFACING,
                source_document_id=doc_id,
            )
    except ConsentDenied:
        return False
    return True


def permits_private_diaries(session: Session, vault_id: uuid.UUID) -> bool:
    """Old opt-ins never silently expand to sensitive diary content."""
    return (owner(session, vault_id).care_settings or {}).get("include_private_diaries") is True


def configure(
    session: Session,
    *,
    vault_id: uuid.UUID,
    principal_id: uuid.UUID,
    enabled: bool,
    presentation: str,
    include_private_diaries: bool = False,
) -> None:
    vault = owner(session, vault_id)
    settings = dict(vault.care_settings or {})
    now = utc_now()
    policy = (
        require_consent(
            session, vault_id=vault_id, purpose=ConsentPurpose.PASSIVE_QA
        ).provider_policy
        if enabled
        else None
    )
    changed = enabled != bool(settings.get("enabled"))
    if changed:
        (grant_consent if enabled else revoke_consent)(
            session,
            command=UserConsentCommand(
                vault_id=vault_id,
                principal_id=principal_id,
                purpose=ConsentPurpose.PROACTIVE_RESURFACING,
                action=ConsentAction.GRANT if enabled else ConsentAction.REVOKE,
                interaction_id=uuid.uuid4(),
                issued_at=now,
                expires_at=now + timedelta(minutes=1),
                provider_policy=policy,
            ),
        )
    if enabled and not settings.get("enabled"):
        settings.update(enabled_at=now.isoformat(), checked_at=now.isoformat())
    if include_private_diaries and not settings.get("include_private_diaries"):
        settings["checked_at"] = now.isoformat()
    if settings.get("include_private_diaries") and not include_private_diaries:
        settings.pop("pending", None)
    settings.update(
        enabled=enabled, presentation=presentation, include_private_diaries=include_private_diaries
    )
    if changed:
        settings["paused_until"] = None
    if not enabled:
        settings.pop("pending", None)
    vault.care_settings = settings
    session.flush()


def offered(session: Session, vault_id: uuid.UUID, turn: ConversationTurn) -> None:
    vault = owner(session, vault_id)
    settings = dict(vault.care_settings or {})
    settings.update(pending=str(turn.id), last_letter_at=utc_now().isoformat())
    vault.care_settings = settings


def view(
    session: Session, *, vault_id: uuid.UUID, protector: SourceContentProtector
) -> dict[str, object]:
    from life_coach.application.conversations import conversation_view

    settings = owner(session, vault_id).care_settings or {}
    letter = None
    pending = settings.get("pending")
    if active(settings) and pending:
        turn = session.scalar(
            select(ConversationTurn).where(
                ConversationTurn.vault_id == vault_id,
                ConversationTurn.id == uuid.UUID(str(pending)),
            )
        )
        if turn:
            try:
                from life_coach.application.conversations import source_rows

                context_turns = list(
                    session.scalars(
                        select(ConversationTurn).where(
                            ConversationTurn.vault_id == vault_id,
                            ConversationTurn.conversation_id == turn.conversation_id,
                            ConversationTurn.position <= turn.position,
                        )
                    )
                )
                ids = [item.question_fragment_id for item in context_turns]
                ids.extend(
                    session.scalars(
                        select(ConversationMaterial.fragment_id).where(
                            ConversationMaterial.vault_id == vault_id,
                            ConversationMaterial.conversation_id == turn.conversation_id,
                        )
                    )
                )
                for item in context_turns:
                    ids.extend(
                        uuid.UUID(key) for key in (item.retrieval or {}).get("fragment_ids", [])
                    )
                for _, _, document in source_rows(
                    session, vault_id, list(dict.fromkeys(ids))
                ).values():
                    require_consent(
                        session,
                        vault_id=vault_id,
                        purpose=ConsentPurpose.PROACTIVE_RESURFACING,
                        source_document_id=document.id,
                    )
                detail = conversation_view(
                    session,
                    vault_id=vault_id,
                    conversation_id=turn.conversation_id,
                    protector=protector,
                    include_preview=False,
                )
                visible_item = next(
                    item
                    for item in cast(list[dict[str, object]], detail["turns"])
                    if item["id"] == pending
                )
                payload = visible_item["reply"]
                if isinstance(payload, dict) and payload.get("care_letter"):
                    letter = {
                        "id": str(turn.id),
                        "body": payload["care_letter"],
                        "created_at": settings["last_letter_at"],
                    }
            except (ModelInvocationDenied, ConsentDenied):
                pass
    return {
        "enabled": settings.get("enabled", False),
        "presentation": settings.get("presentation", "gentle"),
        "include_private_diaries": settings.get("include_private_diaries", False),
        "paused_until": settings.get("paused_until"),
        "letter": letter,
    }


def respond(session: Session, *, vault_id: uuid.UUID, action: str, letter_id: str | None) -> None:
    vault = owner(session, vault_id)
    settings = dict(vault.care_settings or {})
    if action == "pause":
        settings["paused_until"] = (utc_now() + timedelta(days=7)).isoformat()
    if action in ("dismiss", "pause") and (
        action == "pause" or settings.get("pending") == letter_id
    ):
        settings.pop("pending", None)
    if action == "resume":
        settings["paused_until"] = None
    vault.care_settings = settings
    session.flush()


def check_diaries(
    session: Session,
    *,
    vault_id: uuid.UUID,
    principal_id: uuid.UUID,
    membership_generation: int,
    model_binding: str,
    protector: SourceContentProtector,
) -> None:
    from life_coach.application.conversations import enqueue_turn, new_conversation

    vault = owner(session, vault_id)
    settings = dict(vault.care_settings or {})
    if (
        active(settings)
        and settings.get("pending")
        and view(session, vault_id=vault_id, protector=protector)["letter"] is None
    ):
        settings.pop("pending", None)
        vault.care_settings = settings
    if not eligible(session, vault_id, []):
        return
    if session.scalar(
        select(ConversationTurn.id)
        .where(
            ConversationTurn.vault_id == vault_id, ConversationTurn.state.in_(["queued", "running"])
        )
        .limit(1)
    ):
        return
    now = utc_now()
    checked = datetime.fromisoformat(str(settings.get("checked_at", now.isoformat())))
    if checked + timedelta(minutes=10) > now:
        return
    notes = list(
        session.scalars(
            select(SourceDocument)
            .where(
                SourceDocument.vault_id == vault_id,
                SourceDocument.source_type == SourceType.NOTE,
                SourceDocument.deleted_at.is_(None),
                SourceDocument.data_class.in_(
                    [DataClass.NORMAL, DataClass.SENSITIVE]
                    if settings.get("include_private_diaries")
                    else [DataClass.NORMAL]
                ),
                SourceDocument.created_at > max(checked, now - timedelta(days=3)),
            )
            .order_by(SourceDocument.created_at.desc())
            .limit(4)
        )
    )
    allowed = []
    for note in notes:
        try:
            for purpose in (ConsentPurpose.PASSIVE_QA, ConsentPurpose.PROACTIVE_RESURFACING):
                require_consent(
                    session, vault_id=vault_id, purpose=purpose, source_document_id=note.id
                )
            allowed.append(note.id)
        except ConsentDenied:
            pass
    settings["checked_at"] = now.isoformat()
    vault.care_settings = settings
    if not allowed:
        session.flush()
        return
    chat = new_conversation(
        session, vault_id=vault_id, principal_id=principal_id, entry_ids=allowed, allow_history=True
    )
    chat.care_origin = True
    session.flush()
    enqueue_turn(
        session,
        vault_id=vault_id,
        conversation_id=chat.id,
        principal_id=principal_id,
        membership_generation=membership_generation,
        request_id=uuid.uuid4(),
        question="请结合最近的记录判断是否适合写一封简短关怀信。没有充分理由就不来信。",
        model_binding=model_binding,
        protector=protector,
    )
