"""Tests for environment-backed settings and secret-safe representations."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from life_coach.platform.settings import AppEnvironment, Settings

APP_ENVIRONMENT_KEYS = (
    "APP_ENV",
    "APP_DATABASE_URL",
    "APP_OBJECT_STORE_ENDPOINT",
    "APP_OBJECT_STORE_BUCKET",
    "APP_MODEL_PROVIDER",
    "APP_LOG_LEVEL",
    "APP_READINESS_TIMEOUT_SECONDS",
)


def clear_app_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in APP_ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)


def load_settings_without_dotenv() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_have_local_secret_free_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_app_environment(monkeypatch)

    settings = load_settings_without_dotenv()

    assert settings.env is AppEnvironment.DEVELOPMENT
    assert settings.database_dsn == "postgresql+asyncpg://localhost:5432/life_coach"
    assert settings.object_store_endpoint is None
    assert "@" not in settings.database_dsn


def test_settings_load_app_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_app_environment(monkeypatch)
    secret = "postgresql+asyncpg://app:example-password@db.internal/life_coach"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_DATABASE_URL", secret)
    monkeypatch.setenv("APP_OBJECT_STORE_ENDPOINT", "https://objects.example.test")
    monkeypatch.setenv("APP_OBJECT_STORE_BUCKET", "private-records")
    monkeypatch.setenv("APP_MODEL_PROVIDER", "disabled")
    monkeypatch.setenv("APP_LOG_LEVEL", "warning")

    settings = load_settings_without_dotenv()

    assert settings.env is AppEnvironment.TEST
    assert settings.database_dsn == secret
    assert str(settings.object_store_endpoint) == "https://objects.example.test/"
    assert settings.object_store_bucket == "private-records"
    assert settings.log_level == "WARNING"
    assert "example-password" not in repr(settings)
    assert "example-password" not in repr(settings.database_url)


def test_settings_reject_non_async_postgresql_url(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_app_environment(monkeypatch)
    leaked_url = "postgresql://app:SUPERSECRET@db.internal/private-diary"
    monkeypatch.setenv("APP_DATABASE_URL", leaked_url)

    with pytest.raises(ValidationError, match="postgresql\\+asyncpg") as exc_info:
        load_settings_without_dotenv()

    assert "SUPERSECRET" not in str(exc_info.value)
    assert "private-diary" not in str(exc_info.value)


def test_settings_reject_object_store_credentials_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_app_environment(monkeypatch)
    monkeypatch.setenv(
        "APP_OBJECT_STORE_ENDPOINT",
        "https://user:OBJECT_SECRET@objects.example.test?signature=PRIVATE_SIGNATURE",
    )

    with pytest.raises(ValidationError) as exc_info:
        load_settings_without_dotenv()

    rendered_error = str(exc_info.value)
    assert "OBJECT_SECRET" not in rendered_error
    assert "PRIVATE_SIGNATURE" not in rendered_error


def test_production_settings_require_encrypted_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_app_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_DATABASE_URL", "postgresql+asyncpg://db.internal/life_coach")
    monkeypatch.setenv("APP_OBJECT_STORE_ENDPOINT", "https://objects.internal")

    with pytest.raises(ValidationError, match="verified TLS"):
        load_settings_without_dotenv()


def test_production_settings_require_https_object_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_app_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "APP_DATABASE_URL",
        "postgresql+asyncpg://db.internal/life_coach?ssl=verify-full",
    )
    monkeypatch.setenv("APP_OBJECT_STORE_ENDPOINT", "http://objects.internal")

    with pytest.raises(ValidationError, match="must use HTTPS"):
        load_settings_without_dotenv()


def test_production_settings_accept_explicit_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_app_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "APP_DATABASE_URL",
        "postgresql+asyncpg://db.internal/life_coach?ssl=verify-full",
    )
    monkeypatch.setenv("APP_OBJECT_STORE_ENDPOINT", "https://objects.internal")

    settings = load_settings_without_dotenv()

    assert settings.env is AppEnvironment.PRODUCTION


def test_settings_reject_secret_database_query_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_app_environment(monkeypatch)
    monkeypatch.setenv(
        "APP_DATABASE_URL",
        "postgresql+asyncpg://db.internal/life_coach?password=QUERY_SECRET",
    )

    with pytest.raises(ValidationError) as exc_info:
        load_settings_without_dotenv()

    assert "QUERY_SECRET" not in str(exc_info.value)


def test_settings_hide_malformed_database_url_input(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_app_environment(monkeypatch)
    monkeypatch.setenv(
        "APP_DATABASE_URL",
        "postgresql+asyncpg://db.internal:PORT_SECRET/life_coach",
    )

    with pytest.raises(ValidationError) as exc_info:
        load_settings_without_dotenv()

    assert "PORT_SECRET" not in str(exc_info.value)
