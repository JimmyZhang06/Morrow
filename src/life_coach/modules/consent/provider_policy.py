"""Bounded, content-free technical policy for external model providers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import JSON
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from life_coach.modules.consent.exceptions import InvalidProviderPolicy

_ALLOWED_KEYS = frozenset(
    {
        "allowed_providers",
        "processing_regions",
        "zero_retention_required",
        "training_use_allowed",
        "max_retention_days",
        "policy_version",
    }
)
_TECHNICAL_TOKEN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")
_MAX_ITEMS = 16
_MAX_RETENTION_DAYS = 3650


def _technical_tokens(value: object, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_ITEMS:
        raise InvalidProviderPolicy(f"{field_name} must be a list of at most {_MAX_ITEMS} IDs")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or _TECHNICAL_TOKEN.fullmatch(item) is None:
            raise InvalidProviderPolicy(f"{field_name} contains a non-technical ID")
        if item in result:
            raise InvalidProviderPolicy(f"{field_name} contains a duplicate ID")
        result.append(item)
    return tuple(result)


def _strict_bool(value: object, *, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise InvalidProviderPolicy(f"{field_name} must be a boolean")
    return value


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    """Whitelisted provider controls; arbitrary prose and nested JSON are impossible."""

    allowed_providers: tuple[str, ...] = ()
    processing_regions: tuple[str, ...] = ()
    zero_retention_required: bool = True
    training_use_allowed: bool = False
    max_retention_days: int | None = None
    policy_version: str | None = None

    @classmethod
    def from_value(cls, value: ProviderPolicy | Mapping[str, object] | None) -> ProviderPolicy:
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise InvalidProviderPolicy("provider_policy must be a technical policy mapping")
        unknown = set(value).difference(_ALLOWED_KEYS)
        if unknown:
            raise InvalidProviderPolicy("provider_policy contains an unsupported key")

        max_retention_days = value.get("max_retention_days")
        if max_retention_days is not None and (
            type(max_retention_days) is not int
            or not 0 <= max_retention_days <= _MAX_RETENTION_DAYS
        ):
            raise InvalidProviderPolicy(
                f"max_retention_days must be an integer from 0 to {_MAX_RETENTION_DAYS}"
            )
        policy_version = value.get("policy_version")
        if policy_version is not None and (
            not isinstance(policy_version, str)
            or _TECHNICAL_TOKEN.fullmatch(policy_version) is None
        ):
            raise InvalidProviderPolicy("policy_version must be a technical ID")

        return cls(
            allowed_providers=_technical_tokens(
                value.get("allowed_providers"), field_name="allowed_providers"
            ),
            processing_regions=_technical_tokens(
                value.get("processing_regions"), field_name="processing_regions"
            ),
            zero_retention_required=_strict_bool(
                value.get("zero_retention_required"),
                field_name="zero_retention_required",
                default=True,
            ),
            training_use_allowed=_strict_bool(
                value.get("training_use_allowed"),
                field_name="training_use_allowed",
                default=False,
            ),
            max_retention_days=max_retention_days,
            policy_version=policy_version,
        )

    def to_json(self) -> dict[str, object]:
        """Return the fixed-shape JSON representation stored in the append-only row."""

        return {
            "allowed_providers": list(self.allowed_providers),
            "processing_regions": list(self.processing_regions),
            "zero_retention_required": self.zero_retention_required,
            "training_use_allowed": self.training_use_allowed,
            "max_retention_days": self.max_retention_days,
            "policy_version": self.policy_version,
        }


class ProviderPolicyType(TypeDecorator[ProviderPolicy]):
    """Validate the policy again at SQL bind time, including Core insert paths."""

    impl = JSON
    cache_ok = True

    def process_bind_param(
        self, value: ProviderPolicy | None, dialect: Dialect
    ) -> dict[str, object] | None:
        del dialect
        if value is None:
            return None
        return ProviderPolicy.from_value(value).to_json()

    def process_result_value(self, value: object | None, dialect: Dialect) -> ProviderPolicy | None:
        del dialect
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise InvalidProviderPolicy("stored provider_policy is not a mapping")
        return ProviderPolicy.from_value(value)
