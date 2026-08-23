"""Vault-scoped entity candidate generation without automatic high-impact merges."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Self

from pydantic import Field, field_validator, model_validator

from .contracts import (
    ContractModel,
    EntityCandidate,
    EntityKind,
    EntityResolutionSignal,
    EntityResolutionState,
    SourceSpan,
)

_AMBIGUOUS_MENTIONS = frozenset(
    {"他", "她", "他们", "她们", "老板", "妈妈", "爸爸", "同事", "朋友", "he", "she", "they"}
)


class EntityResolutionOption(ContractModel):
    """A bounded candidate projection supplied from exactly one vault."""

    vault_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    kind: EntityKind
    canonical_label: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    related_entity_ids: frozenset[str] = frozenset()
    active_from: datetime | None = None
    active_to: datetime | None = None
    high_impact_merge: bool = False

    @field_validator("active_from", "active_to")
    @classmethod
    def aware_bounds(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("entity active-window bounds must be timezone-aware")
        return value

    @model_validator(mode="after")
    def ordered_window(self) -> Self:
        if (
            self.active_from is not None
            and self.active_to is not None
            and self.active_to <= self.active_from
        ):
            raise ValueError("entity active window must be non-empty")
        return self


def resolve_entity_candidates(
    *,
    vault_id: str,
    mention: str,
    mention_span: SourceSpan,
    kind: EntityKind,
    options: Sequence[EntityResolutionOption],
    related_entity_ids: frozenset[str] = frozenset(),
    mentioned_at: datetime | None = None,
) -> tuple[EntityCandidate, ...]:
    """Return auditable candidates after vault, kind, alias, relation, and time checks.

    This stage proposes links only. It never emits CONFIRMED, so a mistaken name
    or pronoun cannot silently merge people. Cross-vault options are discarded
    before any scoring.
    """

    if mention_span.vault_id != vault_id:
        raise ValueError("entity mention and resolution space must share a vault")
    if mentioned_at is not None and (
        mentioned_at.tzinfo is None or mentioned_at.utcoffset() is None
    ):
        raise ValueError("mentioned_at must be timezone-aware")

    normalized_mention = _normalize(mention)
    ranked: list[tuple[int, str, EntityCandidate]] = []
    for option in options:
        if option.vault_id != vault_id or option.kind is not kind:
            continue
        labels = {_normalize(option.canonical_label)}
        labels.update(_normalize(alias) for alias in option.aliases)
        if normalized_mention not in labels:
            continue

        signals = {EntityResolutionSignal.ALIAS}
        if related_entity_ids & option.related_entity_ids:
            signals.add(EntityResolutionSignal.RELATIONSHIP)
        has_active_window = option.active_from is not None or option.active_to is not None
        if has_active_window and mentioned_at is not None and _within_window(option, mentioned_at):
            signals.add(EntityResolutionSignal.TIME)
        if len(signals) >= 2:
            signals.add(EntityResolutionSignal.LOCAL_CONTEXT)

        reasons = ", ".join(sorted(signal.value for signal in signals))
        if option.high_impact_merge:
            reasons = f"{reasons}; high-impact merge requires user confirmation"
        if normalized_mention in _AMBIGUOUS_MENTIONS:
            reasons = f"{reasons}; ambiguous role or pronoun requires confirmation"
        candidate = EntityCandidate(
            mention=mention,
            mention_span=mention_span,
            kind=kind,
            canonical_label=option.canonical_label,
            entity_id=option.entity_id,
            resolution_state=EntityResolutionState.PROPOSED,
            signals=frozenset(signals),
            confidence_reason=reasons,
            requires_user_confirmation=True,
        )
        ranked.append((-len(signals), option.entity_id, candidate))

    if ranked:
        ranked.sort(key=lambda item: (item[0], item[1]))
        return tuple(item[2] for item in ranked)
    return (
        EntityCandidate(
            mention=mention,
            mention_span=mention_span,
            kind=kind,
            resolution_state=EntityResolutionState.UNRESOLVED,
            confidence_reason="no same-vault candidate has sufficient alias evidence",
            requires_user_confirmation=True,
        ),
    )


def _within_window(option: EntityResolutionOption, mentioned_at: datetime) -> bool:
    return (option.active_from is None or mentioned_at >= option.active_from) and (
        option.active_to is None or mentioned_at < option.active_to
    )


def _normalize(value: str) -> str:
    return "".join(value.casefold().split())


__all__ = ["EntityResolutionOption", "resolve_entity_candidates"]
