"""Purpose-level consent event creation and deterministic reduction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from life_coach.modules.consent.exceptions import (
    ConsentDenied,
    ConsentScopeNotFound,
    InvalidConsentAction,
    InvalidConsentPurpose,
)
from life_coach.modules.consent.models import (
    ConsentAction,
    ConsentPurpose,
    ConsentRecord,
    ConsentScope,
    require_user_consent_actor,
)
from life_coach.modules.consent.provider_policy import ProviderPolicy
from life_coach.modules.identity.models import CreatedBy
from life_coach.modules.identity.service import (
    VaultSnapshot,
    capture_vault_snapshot,
    get_vault,
    increment_policy_epoch,
    is_vault_snapshot_current,
)


@dataclass(frozen=True, slots=True)
class ConsentResolution:
    """The latest applicable event, or an explicit default-deny result."""

    allowed: bool
    purpose: ConsentPurpose
    source_document_id: UUID | None
    record_id: UUID | None
    policy_epoch: int | None
    scope: ConsentScope | None
    provider_policy: ProviderPolicy


def _normalise_purpose(purpose: ConsentPurpose | str) -> ConsentPurpose:
    candidate = purpose.strip() if isinstance(purpose, str) else purpose
    try:
        return ConsentPurpose(candidate)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ConsentPurpose)
        raise InvalidConsentPurpose(f"purpose must be one of: {allowed}") from exc


def _normalise_action(action: ConsentAction | str) -> ConsentAction:
    try:
        return ConsentAction(action)
    except ValueError as exc:
        raise InvalidConsentAction("action must be grant or revoke") from exc


def _source_exists_in_vault(session: Session, *, vault_id: UUID, source_document_id: UUID) -> bool:
    # Keep model import order independent while retaining a real composite database FK.
    from life_coach.modules.sources.models import SourceDocument

    return (
        session.scalar(
            select(SourceDocument.id).where(
                SourceDocument.vault_id == vault_id,
                SourceDocument.id == source_document_id,
                SourceDocument.deleted_at.is_(None),
            )
        )
        is not None
    )


def _require_source_in_vault(session: Session, *, vault_id: UUID, source_document_id: UUID) -> None:
    if not _source_exists_in_vault(
        session, vault_id=vault_id, source_document_id=source_document_id
    ):
        raise ConsentScopeNotFound(vault_id, source_document_id)


def record_consent(
    session: Session,
    *,
    vault_id: UUID,
    purpose: ConsentPurpose | str,
    action: ConsentAction | str,
    source_document_id: UUID | None = None,
    provider_policy: ProviderPolicy | Mapping[str, object] | None = None,
    created_by: CreatedBy | str = CreatedBy.USER,
) -> ConsentRecord:
    """Append an event and atomically advance its vault policy epoch.

    The caller owns commit/rollback. If inserting the event fails, rolling back the
    transaction also rolls back the epoch increment.
    """

    normalised_purpose = _normalise_purpose(purpose)
    normalised_action = _normalise_action(action)
    normalised_actor = require_user_consent_actor(created_by)
    normalised_provider_policy = ProviderPolicy.from_value(provider_policy)
    # Serialize policy changes with Source mutation/deletion using one lock order.
    get_vault(session, vault_id, for_update=True)
    if source_document_id is not None:
        _require_source_in_vault(session, vault_id=vault_id, source_document_id=source_document_id)

    policy_epoch = increment_policy_epoch(session, vault_id)
    record = ConsentRecord(
        vault_id=vault_id,
        purpose=normalised_purpose,
        action=normalised_action,
        scope=(ConsentScope.VAULT if source_document_id is None else ConsentScope.SOURCE_DOCUMENT),
        source_document_id=source_document_id,
        provider_policy=normalised_provider_policy,
        policy_epoch=policy_epoch,
        created_by=normalised_actor,
    )
    session.add(record)
    session.flush()
    return record


def grant_consent(
    session: Session,
    *,
    vault_id: UUID,
    purpose: ConsentPurpose | str,
    source_document_id: UUID | None = None,
    provider_policy: ProviderPolicy | Mapping[str, object] | None = None,
    created_by: CreatedBy | str = CreatedBy.USER,
) -> ConsentRecord:
    """Append a grant event at vault or single-Source scope."""

    return record_consent(
        session,
        vault_id=vault_id,
        purpose=purpose,
        action=ConsentAction.GRANT,
        source_document_id=source_document_id,
        provider_policy=provider_policy,
        created_by=created_by,
    )


def revoke_consent(
    session: Session,
    *,
    vault_id: UUID,
    purpose: ConsentPurpose | str,
    source_document_id: UUID | None = None,
    provider_policy: ProviderPolicy | Mapping[str, object] | None = None,
    created_by: CreatedBy | str = CreatedBy.USER,
) -> ConsentRecord:
    """Append a revocation event which immediately wins by its newer epoch."""

    return record_consent(
        session,
        vault_id=vault_id,
        purpose=purpose,
        action=ConsentAction.REVOKE,
        source_document_id=source_document_id,
        provider_policy=provider_policy,
        created_by=created_by,
    )


def _latest_scope_event(
    *, vault_id: UUID, purpose: ConsentPurpose, source_document_id: UUID | None
) -> Select[tuple[ConsentRecord]]:
    statement = select(ConsentRecord).where(
        ConsentRecord.vault_id == vault_id,
        ConsentRecord.purpose == purpose,
        ConsentRecord.deleted_at.is_(None),
        (
            ConsentRecord.source_document_id.is_(None)
            if source_document_id is None
            else ConsentRecord.source_document_id == source_document_id
        ),
    )
    return statement.order_by(ConsentRecord.policy_epoch.desc()).limit(1)


def resolve_consent(
    session: Session,
    *,
    vault_id: UUID,
    purpose: ConsentPurpose | str,
    source_document_id: UUID | None = None,
) -> ConsentResolution:
    """Resolve vault and Source scope conservatively; absence is always deny.

    A Source-specific revoke remains an exception across later vault-wide grants. A newer
    vault-wide revoke still shuts off an older Source grant; a later Source grant can then
    explicitly re-authorize that one Source. Cross-vault identifiers are rejected first.
    """

    normalised_purpose = _normalise_purpose(purpose)
    get_vault(session, vault_id)
    if source_document_id is not None:
        _require_source_in_vault(session, vault_id=vault_id, source_document_id=source_document_id)
    vault_record = session.scalar(
        _latest_scope_event(
            vault_id=vault_id,
            purpose=normalised_purpose,
            source_document_id=None,
        )
    )
    source_record = None
    if source_document_id is not None:
        source_record = session.scalar(
            _latest_scope_event(
                vault_id=vault_id,
                purpose=normalised_purpose,
                source_document_id=source_document_id,
            )
        )
    record = vault_record
    if source_record is not None:
        if source_record.action is ConsentAction.REVOKE:
            record = source_record
        elif (
            vault_record is not None
            and vault_record.action is ConsentAction.REVOKE
            and vault_record.policy_epoch > source_record.policy_epoch
        ):
            record = vault_record
        else:
            record = source_record
    if record is None:
        return ConsentResolution(
            allowed=False,
            purpose=normalised_purpose,
            source_document_id=source_document_id,
            record_id=None,
            policy_epoch=None,
            scope=None,
            provider_policy=ProviderPolicy(),
        )
    return ConsentResolution(
        allowed=record.action is ConsentAction.GRANT,
        purpose=normalised_purpose,
        source_document_id=source_document_id,
        record_id=record.id,
        policy_epoch=record.policy_epoch,
        scope=record.scope,
        provider_policy=record.provider_policy,
    )


def check_consent(
    session: Session,
    *,
    vault_id: UUID,
    purpose: ConsentPurpose | str,
    source_document_id: UUID | None = None,
) -> bool:
    """Return the purpose decision using a default-deny reducer."""

    return resolve_consent(
        session,
        vault_id=vault_id,
        purpose=purpose,
        source_document_id=source_document_id,
    ).allowed


def require_consent(
    session: Session,
    *,
    vault_id: UUID,
    purpose: ConsentPurpose | str,
    source_document_id: UUID | None = None,
) -> ConsentResolution:
    """Return the applicable grant or raise a content-free default-deny error."""

    resolution = resolve_consent(
        session,
        vault_id=vault_id,
        purpose=purpose,
        source_document_id=source_document_id,
    )
    if not resolution.allowed:
        raise ConsentDenied(
            vault_id=vault_id,
            purpose=resolution.purpose.value,
            source_document_id=source_document_id,
        )
    return resolution


capture_snapshot = capture_vault_snapshot
check_snapshot = is_vault_snapshot_current

__all__ = [
    "ConsentResolution",
    "VaultSnapshot",
    "capture_snapshot",
    "check_consent",
    "check_snapshot",
    "grant_consent",
    "record_consent",
    "require_consent",
    "resolve_consent",
    "revoke_consent",
]
