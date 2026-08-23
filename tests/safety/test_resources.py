from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from life_coach.modules.safety import (
    CrisisResource,
    CrisisResourceCatalog,
    CrisisServiceType,
)

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)


def resource(**overrides: object) -> CrisisResource:
    values: dict[str, object] = {
        "resource_id": "runtime-resource",
        "country": "ZZ",
        "region": None,
        "language": "en",
        "service_type": CrisisServiceType.EMERGENCY,
        "phone": "runtime-phone-endpoint",
        "url": None,
        "hours": "runtime-directory-hours",
        "eligibility": "runtime-directory-eligibility",
        "authoritative_source": "runtime-directory-source",
        "verified_at": NOW - timedelta(days=1),
        "valid_from": NOW - timedelta(days=2),
        "valid_to": NOW + timedelta(days=2),
        "review_owner": "human-review-owner",
        "human_verified": True,
    }
    values.update(overrides)
    return CrisisResource(**values)  # type: ignore[arg-type]


def test_catalog_has_no_built_in_resources() -> None:
    assert CrisisResourceCatalog(()).resources == ()


def test_lookup_requires_a_known_country() -> None:
    catalog = CrisisResourceCatalog((resource(),))

    assert catalog.lookup(country=None, region=None, language="en", at=NOW) == ()


def test_lookup_filters_by_country_region_and_language() -> None:
    country_wide = resource(resource_id="country-wide")
    regional = resource(resource_id="regional", region="north")
    another_language = resource(resource_id="another-language", language="zz-alt")
    catalog = CrisisResourceCatalog((country_wide, regional, another_language))

    assert catalog.lookup(country="zz", region=None, language="EN", at=NOW) == (country_wide,)
    assert catalog.lookup(country="ZZ", region="NORTH", language="en", at=NOW) == (
        country_wide,
        regional,
    )


def test_unverified_and_expired_endpoints_are_not_displayed() -> None:
    current = resource(resource_id="current")
    unverified = resource(
        resource_id="unverified",
        human_verified=False,
        verified_at=None,
        review_owner=None,
    )
    expired = resource(resource_id="expired", valid_to=NOW)
    future_verification = resource(
        resource_id="future-verification",
        verified_at=NOW + timedelta(minutes=1),
    )
    catalog = CrisisResourceCatalog((current, unverified, expired, future_verification))

    assert catalog.lookup(country="ZZ", region=None, language="en", at=NOW) == (current,)


def test_human_verified_record_requires_a_review_audit_trail() -> None:
    with pytest.raises(ValueError, match="human verification"):
        resource(review_owner=None)


def test_resource_expiry_is_half_open() -> None:
    expiring = resource(valid_to=NOW)

    assert not expiring.is_displayable(at=NOW)


def test_runtime_catalog_rejects_string_enum_and_truthy_verification_values() -> None:
    with pytest.raises(TypeError, match="service_type"):
        resource(service_type="emergency")
    with pytest.raises(TypeError, match="human_verified"):
        resource(human_verified="true")
