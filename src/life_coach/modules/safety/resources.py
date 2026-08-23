"""Runtime crisis-resource catalog domain rules.

The module intentionally ships with no resource records.  Deployments must load
reviewed regional data at runtime rather than relying on model memory, prompts,
or client constants.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ._time import require_aware
from ._validation import (
    require_bool,
    require_enum,
    require_optional_string,
    require_string,
)


class CrisisServiceType(StrEnum):
    """Kinds of human help represented by the catalog."""

    EMERGENCY = "emergency"
    CRISIS_LINE = "crisis_line"
    TEXT = "text"
    CLINICAL = "clinical"


@dataclass(frozen=True, slots=True)
class CrisisResource:
    """One versioned resource record supplied by an external directory."""

    resource_id: str
    country: str
    region: str | None
    language: str
    service_type: CrisisServiceType
    phone: str | None
    url: str | None
    hours: str
    eligibility: str
    authoritative_source: str
    verified_at: datetime | None
    valid_from: datetime
    valid_to: datetime
    review_owner: str | None
    human_verified: bool

    def __post_init__(self) -> None:
        require_optional_string(self.region, field_name="region")
        require_enum(self.service_type, enum_type=CrisisServiceType, field_name="service_type")
        require_optional_string(self.phone, field_name="phone")
        require_optional_string(self.url, field_name="url")
        require_optional_string(self.review_owner, field_name="review_owner")
        require_bool(self.human_verified, field_name="human_verified")
        for field_name, value in (
            ("resource_id", self.resource_id),
            ("country", self.country),
            ("language", self.language),
            ("hours", self.hours),
            ("eligibility", self.eligibility),
            ("authoritative_source", self.authoritative_source),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        if self.region is not None and not self.region.strip():
            raise ValueError("region must be non-empty when provided")
        if self.phone is None and self.url is None:
            raise ValueError("a resource needs a runtime phone or URL endpoint")
        if self.phone is not None and not self.phone.strip():
            raise ValueError("phone must be non-empty when provided")
        if self.url is not None and not self.url.strip():
            raise ValueError("url must be non-empty when provided")
        require_aware(self.valid_from, field_name="valid_from")
        require_aware(self.valid_to, field_name="valid_to")
        if self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        if self.verified_at is not None:
            require_aware(self.verified_at, field_name="verified_at")
        if self.human_verified and (
            self.verified_at is None or self.review_owner is None or not self.review_owner.strip()
        ):
            raise ValueError("human verification requires verified_at and review_owner")
        if self.verified_at is not None and self.verified_at > self.valid_to:
            raise ValueError("verified_at must not be after valid_to")

    def is_displayable(self, *, at: datetime) -> bool:
        """Return whether a concrete endpoint may be shown at ``at``."""

        require_aware(at, field_name="at")
        return (
            self.human_verified
            and self.verified_at is not None
            and self.verified_at <= at
            and self.valid_from <= at < self.valid_to
        )


class CrisisResourceCatalog:
    """An immutable view over externally supplied crisis-resource records."""

    def __init__(self, resources: Iterable[CrisisResource]) -> None:
        materialized = tuple(resources)
        ids = tuple(resource.resource_id for resource in materialized)
        if len(ids) != len(set(ids)):
            raise ValueError("resource_id values must be unique")
        self._resources = materialized

    @property
    def resources(self) -> tuple[CrisisResource, ...]:
        """Return the loaded runtime records without adding built-in defaults."""

        return self._resources

    def lookup(
        self,
        *,
        country: str | None,
        region: str | None,
        language: str,
        at: datetime,
        service_type: CrisisServiceType | None = None,
    ) -> tuple[CrisisResource, ...]:
        """Return current, reviewed records for an exact locale.

        Unknown country returns no concrete endpoint, allowing the caller to ask
        for location or present generic local-emergency guidance.  A country-only
        query never leaks region-specific entries.
        """

        require_aware(at, field_name="at")
        require_optional_string(country, field_name="country")
        require_optional_string(region, field_name="region")
        require_string(language, field_name="language")
        if service_type is not None:
            require_enum(service_type, enum_type=CrisisServiceType, field_name="service_type")
        if country is None or not country.strip() or not language.strip():
            return ()
        normalized_country = country.strip().casefold()
        normalized_region = region.strip().casefold() if region is not None else None
        normalized_language = language.strip().casefold()

        matches = (
            resource
            for resource in self._resources
            if resource.country.strip().casefold() == normalized_country
            and resource.language.strip().casefold() == normalized_language
            and resource.is_displayable(at=at)
            and (service_type is None or resource.service_type is service_type)
            and self._region_matches(resource.region, normalized_region)
        )
        return tuple(sorted(matches, key=lambda resource: resource.resource_id))

    @staticmethod
    def _region_matches(resource_region: str | None, requested_region: str | None) -> bool:
        if requested_region is None:
            return resource_region is None
        return resource_region is None or resource_region.strip().casefold() == requested_region
