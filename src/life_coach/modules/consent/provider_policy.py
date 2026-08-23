"""Bounded, content-free technical policy for external model providers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

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

# This is deliberately a closed, immutable registry. Adding a provider or region is a
# reviewed server configuration/code change, never something accepted from a consent payload.
TRUSTED_PROVIDER_REGISTRY: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "zero-retention-provider": frozenset({"eu", "us"}),
        "regional-provider": frozenset({"apac", "eu"}),
        "another-provider": frozenset({"apac", "us"}),
    }
)
TRUSTED_POLICY_VERSIONS: Final[frozenset[str]] = frozenset({"v1", "v2"})
_TRUSTED_PROCESSING_REGIONS = frozenset(
    region
    for supported_regions in TRUSTED_PROVIDER_REGISTRY.values()
    for region in supported_regions
)


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


def _registered_tokens(
    value: object,
    *,
    field_name: str,
    registry: frozenset[str],
) -> tuple[str, ...]:
    result = _technical_tokens(value, field_name=field_name)
    if not set(result).issubset(registry):
        raise InvalidProviderPolicy(f"{field_name} contains an unregistered ID")
    return result


def _strict_bool(value: object, *, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise InvalidProviderPolicy(f"{field_name} must be a boolean")
    return value


def _retention_days(value: object) -> int | None:
    if value is not None and (type(value) is not int or not 0 <= value <= _MAX_RETENTION_DAYS):
        raise InvalidProviderPolicy(
            f"max_retention_days must be an integer from 0 to {_MAX_RETENTION_DAYS}"
        )
    return value


def _policy_version(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _TECHNICAL_TOKEN.fullmatch(value) is None:
        raise InvalidProviderPolicy("policy_version must be a technical ID")
    if value not in TRUSTED_POLICY_VERSIONS:
        raise InvalidProviderPolicy("policy_version is not registered")
    return value


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    """Whitelisted provider controls; arbitrary prose and nested JSON are impossible.

    Empty provider or region sets mean that no external provider/region is permitted. They
    are never interpreted as wildcards. ``None`` retention is the unconstrained/top value;
    any numeric bound is therefore more restrictive when policies are combined.
    """

    allowed_providers: tuple[str, ...] = ()
    processing_regions: tuple[str, ...] = ()
    zero_retention_required: bool = True
    training_use_allowed: bool = False
    max_retention_days: int | None = None
    policy_version: str | None = None

    def __post_init__(self) -> None:
        """Canonicalise and validate even direct dataclass construction."""

        providers = _registered_tokens(
            self.allowed_providers,
            field_name="allowed_providers",
            registry=frozenset(TRUSTED_PROVIDER_REGISTRY),
        )
        regions = _registered_tokens(
            self.processing_regions,
            field_name="processing_regions",
            registry=_TRUSTED_PROCESSING_REGIONS,
        )
        zero_retention = _strict_bool(
            self.zero_retention_required,
            field_name="zero_retention_required",
            default=True,
        )
        training_use = _strict_bool(
            self.training_use_allowed,
            field_name="training_use_allowed",
            default=False,
        )
        retention_days = _retention_days(self.max_retention_days)
        policy_version = _policy_version(self.policy_version)
        object.__setattr__(self, "allowed_providers", providers)
        object.__setattr__(self, "processing_regions", regions)
        object.__setattr__(self, "zero_retention_required", zero_retention)
        object.__setattr__(self, "training_use_allowed", training_use)
        object.__setattr__(self, "max_retention_days", retention_days)
        object.__setattr__(self, "policy_version", policy_version)

    @classmethod
    def from_value(cls, value: ProviderPolicy | Mapping[str, object] | None) -> ProviderPolicy:
        """Return a newly validated policy, including when ``value`` is already one."""

        if value is None:
            return cls()
        if isinstance(value, cls):
            # Reconstruct instead of returning the instance. Besides keeping one path for all
            # callers, this catches instances corrupted with object.__setattr__ or object.__new__.
            return cls(
                allowed_providers=value.allowed_providers,
                processing_regions=value.processing_regions,
                zero_retention_required=value.zero_retention_required,
                training_use_allowed=value.training_use_allowed,
                max_retention_days=value.max_retention_days,
                policy_version=value.policy_version,
            )
        if not isinstance(value, Mapping):
            raise InvalidProviderPolicy("provider_policy must be a technical policy mapping")
        unknown = set(value).difference(_ALLOWED_KEYS)
        if unknown:
            raise InvalidProviderPolicy("provider_policy contains an unsupported key")

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
            max_retention_days=_retention_days(value.get("max_retention_days")),
            policy_version=_policy_version(value.get("policy_version")),
        )

    @classmethod
    def meet(cls, *values: ProviderPolicy) -> ProviderPolicy:
        """Return the restrictive meet of all applicable scope policies."""

        policies = tuple(cls.from_value(value) for value in values)
        if not policies:
            return cls()

        providers = set(policies[0].allowed_providers)
        regions = set(policies[0].processing_regions)
        for policy in policies[1:]:
            providers.intersection_update(policy.allowed_providers)
            regions.intersection_update(policy.processing_regions)

        retention_bounds = [
            policy.max_retention_days
            for policy in policies
            if policy.max_retention_days is not None
        ]
        versions = {
            policy.policy_version for policy in policies if policy.policy_version is not None
        }
        if len(versions) > 1:
            raise InvalidProviderPolicy("applicable policies use incomparable versions")

        return cls(
            allowed_providers=tuple(sorted(providers)),
            processing_regions=tuple(sorted(regions)),
            zero_retention_required=any(policy.zero_retention_required for policy in policies),
            training_use_allowed=all(policy.training_use_allowed for policy in policies),
            max_retention_days=min(retention_bounds) if retention_bounds else None,
            policy_version=next(iter(versions), None),
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
