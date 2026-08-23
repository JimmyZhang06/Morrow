"""Purpose-level consent event creation and deterministic reduction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from life_coach.modules.consent.exceptions import (
    ConsentDenied,
    ConsentInteractionReplayed,
    ConsentScopeNotFound,
    InvalidConsentAction,
    InvalidConsentCommand,
    InvalidConsentPurpose,
)
from life_coach.modules.consent.models import (
    ConsentAction,
    ConsentPurpose,
    ConsentRecord,
    ConsentScope,
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
from life_coach.shared.database import utc_now

_MAX_INTERACTION_LIFETIME = timedelta(minutes=5)


def _normalise_purpose(purpose: ConsentPurpose | str) -> ConsentPurpose:
    candidate = purpose.strip() if isinstance(purpose, str) else purpose
    try:
        return ConsentPurpose(candidate)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in ConsentPurpose)
        raise InvalidConsentPurpose(f"purpose must be one of: {allowed}") from exc


def _normalise_action(action: ConsentAction | str) -> ConsentAction:
    try:
        return ConsentAction(action)
    except (TypeError, ValueError) as exc:
        raise InvalidConsentAction("action must be grant or revoke") from exc


def _require_uuid(value: object, *, field_name: str) -> UUID:
    if not isinstance(value, UUID) or value.int == 0:
        raise InvalidConsentCommand(f"{field_name} must be a non-nil UUID")
    return value


def _aware_utc(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidConsentCommand(f"{field_name} must be an aware datetime")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class UserConsentCommand:
    """Single-use authorization command minted by a trusted authenticated interaction.

    This is a domain input, not a request DTO. The API/authentication boundary must construct it
    from the authenticated principal and must not deserialize an untrusted object directly into
    this type. Every field that can alter the authorization is bound into the same command.
    """

    vault_id: UUID
    principal_id: UUID
    purpose: ConsentPurpose | str
    action: ConsentAction | str
    interaction_id: UUID
    issued_at: datetime
    expires_at: datetime
    source_document_id: UUID | None = None
    provider_policy: ProviderPolicy | Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        vault_id = _require_uuid(self.vault_id, field_name="vault_id")
        principal_id = _require_uuid(self.principal_id, field_name="principal_id")
        interaction_id = _require_uuid(self.interaction_id, field_name="interaction_id")
        source_document_id = self.source_document_id
        if source_document_id is not None:
            source_document_id = _require_uuid(source_document_id, field_name="source_document_id")
        purpose = _normalise_purpose(self.purpose)
        action = _normalise_action(self.action)
        issued_at = _aware_utc(self.issued_at, field_name="issued_at")
        expires_at = _aware_utc(self.expires_at, field_name="expires_at")
        if expires_at <= issued_at:
            raise InvalidConsentCommand("expires_at must be after issued_at")
        if expires_at - issued_at > _MAX_INTERACTION_LIFETIME:
            raise InvalidConsentCommand("consent interaction lifetime is too long")
        provider_policy = ProviderPolicy.from_value(self.provider_policy)

        object.__setattr__(self, "vault_id", vault_id)
        object.__setattr__(self, "principal_id", principal_id)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "interaction_id", interaction_id)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "source_document_id", source_document_id)
        object.__setattr__(self, "provider_policy", provider_policy)


@dataclass(frozen=True, slots=True)
class ConsentResolution:
    """The reduced decision and restrictive provider policy, or default deny."""

    allowed: bool
    purpose: ConsentPurpose
    source_document_id: UUID | None
    record_id: UUID | None
    policy_epoch: int | None
    scope: ConsentScope | None
    provider_policy: ProviderPolicy


def _validated_current_command(command: UserConsentCommand) -> UserConsentCommand:
    if not isinstance(command, UserConsentCommand):
        raise InvalidConsentCommand("a trusted UserConsentCommand is required")
    try:
        validated = UserConsentCommand(
            vault_id=command.vault_id,
            principal_id=command.principal_id,
            purpose=command.purpose,
            action=command.action,
            interaction_id=command.interaction_id,
            issued_at=command.issued_at,
            expires_at=command.expires_at,
            source_document_id=command.source_document_id,
            provider_policy=command.provider_policy,
        )
    except AttributeError as exc:
        raise InvalidConsentCommand("consent command is incomplete") from exc

    now = utc_now()
    if validated.issued_at > now:
        raise InvalidConsentCommand("consent interaction is not yet valid")
    if validated.expires_at <= now:
        raise InvalidConsentCommand("consent interaction has expired")
    return validated


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


def _interaction_was_used(session: Session, command: UserConsentCommand) -> bool:
    return (
        session.scalar(
            select(ConsentRecord.id).where(
                ConsentRecord.vault_id == command.vault_id,
                ConsentRecord.interaction_id == command.interaction_id,
            )
        )
        is not None
    )


def record_consent(
    session: Session,
    *,
    command: UserConsentCommand,
) -> ConsentRecord:
    """Append one trusted user event and atomically advance its vault policy epoch.

    The caller owns commit/rollback. The unique ``(vault_id, interaction_id)`` constraint is
    the concurrency backstop for the early replay check.
    """

    command = _validated_current_command(command)
    if _interaction_was_used(session, command):
        raise ConsentInteractionReplayed(command.vault_id, command.interaction_id)

    # Serialize policy changes with Source mutation/deletion using one lock order.
    get_vault(session, command.vault_id, for_update=True)
    if command.source_document_id is not None:
        _require_source_in_vault(
            session,
            vault_id=command.vault_id,
            source_document_id=command.source_document_id,
        )

    database_allocates_epoch = session.get_bind().dialect.name == "postgresql"
    # PostgreSQL's BEFORE INSERT trigger locks the vault, increments the fence and
    # overwrites this placeholder. SQLite keeps the portable service-level allocation.
    policy_epoch = (
        0
        if database_allocates_epoch
        else increment_policy_epoch(session, command.vault_id)
    )
    record = ConsentRecord(
        vault_id=command.vault_id,
        purpose=command.purpose,
        action=command.action,
        scope=(
            ConsentScope.VAULT
            if command.source_document_id is None
            else ConsentScope.SOURCE_DOCUMENT
        ),
        source_document_id=command.source_document_id,
        principal_id=command.principal_id,
        interaction_id=command.interaction_id,
        issued_at=command.issued_at,
        expires_at=command.expires_at,
        provider_policy=command.provider_policy,
        policy_epoch=policy_epoch,
        created_by=CreatedBy.USER,
    )
    session.add(record)
    session.flush()
    if database_allocates_epoch:
        session.refresh(record, attribute_names=["policy_epoch"])

    if command.purpose == ConsentPurpose.SEARCH and command.action == ConsentAction.REVOKE:
        # Runtime import avoids making the consent model/import graph depend on Sources.
        from life_coach.modules.sources.service import invalidate_search_projections

        invalidate_search_projections(
            session,
            vault_id=command.vault_id,
            source_document_id=command.source_document_id,
        )
    return record


def grant_consent(session: Session, *, command: UserConsentCommand) -> ConsentRecord:
    """Append a trusted grant command at vault or single-Source scope."""

    command = _validated_current_command(command)
    if command.action != ConsentAction.GRANT:
        raise InvalidConsentAction("grant_consent requires a grant command")
    return record_consent(session, command=command)


def revoke_consent(session: Session, *, command: UserConsentCommand) -> ConsentRecord:
    """Append a trusted revocation command and synchronously enforce SEARCH invalidation."""

    command = _validated_current_command(command)
    if command.action != ConsentAction.REVOKE:
        raise InvalidConsentAction("revoke_consent requires a revoke command")
    return record_consent(session, command=command)


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


def _reduce_decision(
    vault_record: ConsentRecord | None,
    source_record: ConsentRecord | None,
) -> ConsentRecord | None:
    """Apply the scope/action truth table independently from provider constraints."""

    if source_record is None:
        return vault_record
    # A Source-specific revoke is a sticky opt-out across vault-wide grants.
    if source_record.action == ConsentAction.REVOKE:
        return source_record
    if vault_record is None:
        return source_record
    if vault_record.action == ConsentAction.GRANT:
        # Both are grants. The newest is provenance, but both policies remain applicable.
        return max((vault_record, source_record), key=lambda record: record.policy_epoch)
    # A later explicit Source grant can re-authorize after an older vault revoke. A newer
    # vault revoke shuts down the older Source grant.
    if source_record.policy_epoch > vault_record.policy_epoch:
        return source_record
    return vault_record


def resolve_consent(
    session: Session,
    *,
    vault_id: UUID,
    purpose: ConsentPurpose | str,
    source_document_id: UUID | None = None,
) -> ConsentResolution:
    """Reduce decision and provider constraints separately; absence always denies.

    Decision truth table for the latest vault event ``V`` and latest Source event ``S``::

        V:none  S:none   deny       V:grant S:none   allow
        V:revoke S:none  deny       V:none  S:grant  allow
        V:none  S:revoke deny       V:grant S:grant  allow
        V:grant S:revoke deny       V:revoke S:revoke deny
        V:revoke S:grant allow only when S is newer than V, otherwise deny

    When records exist, their provider policies are always combined with a restrictive meet.
    Thus a Source grant cannot replace or loosen the applicable vault-wide policy.
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

    decision_record = _reduce_decision(vault_record, source_record)
    applicable_records = tuple(
        record for record in (vault_record, source_record) if record is not None
    )
    provider_policy = ProviderPolicy.meet(
        *(record.provider_policy for record in applicable_records)
    )
    if decision_record is None:
        return ConsentResolution(
            allowed=False,
            purpose=normalised_purpose,
            source_document_id=source_document_id,
            record_id=None,
            policy_epoch=None,
            scope=None,
            provider_policy=provider_policy,
        )
    return ConsentResolution(
        allowed=decision_record.action == ConsentAction.GRANT,
        purpose=normalised_purpose,
        source_document_id=source_document_id,
        record_id=decision_record.id,
        policy_epoch=decision_record.policy_epoch,
        scope=decision_record.scope,
        provider_policy=provider_policy,
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
    "UserConsentCommand",
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
