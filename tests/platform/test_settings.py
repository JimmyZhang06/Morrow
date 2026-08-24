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
    "APP_SOURCE_API_ENABLED",
    "APP_SOURCE_API_HMAC_KEY",
    "APP_LOCAL_SOURCE_CONTENT_KEY",
    "APP_MODEL_PROVIDER",
    "APP_MODEL_RUN_HMAC_KEY",
    "APP_LOG_LEVEL",
    "APP_READINESS_TIMEOUT_SECONDS",
    "APP_AUTH_INTROSPECTION_URL",
    "APP_AUTH_ISSUER",
    "APP_AUTH_AUDIENCE",
    "APP_AUTH_CLIENT_ID",
    "APP_AUTH_CLIENT_SECRET",
    "APP_AUTH_TIMEOUT_SECONDS",
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


@pytest.mark.parametrize(
    "host",
    ["localhost", "LOCALHOST.", "api.localhost", "127.0.0.1", "[::1]"],
)
def test_production_settings_reject_loopback_database_hosts(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    clear_app_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "APP_DATABASE_URL",
        f"postgresql+asyncpg://{host}/life_coach?ssl=verify-full",
    )
    monkeypatch.setenv("APP_OBJECT_STORE_ENDPOINT", "https://objects.internal")

    with pytest.raises(ValidationError, match="non-loopback host"):
        load_settings_without_dotenv()


@pytest.mark.parametrize("override", ["host=localhost", "hostaddr=127.0.0.1"])
def test_production_settings_reject_query_host_overrides(
    monkeypatch: pytest.MonkeyPatch,
    override: str,
) -> None:
    clear_app_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "APP_DATABASE_URL",
        f"postgresql+asyncpg://db.internal/life_coach?ssl=verify-full&{override}",
    )
    monkeypatch.setenv("APP_OBJECT_STORE_ENDPOINT", "https://objects.internal")

    with pytest.raises(ValidationError, match="must not override its host"):
        load_settings_without_dotenv()


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


def test_enabled_model_provider_requires_secret_model_run_hmac_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_app_environment(monkeypatch)
    monkeypatch.setenv("APP_MODEL_PROVIDER", "provider:1001")

    with pytest.raises(ValidationError, match="requires model_run_hmac_key"):
        load_settings_without_dotenv()


def test_model_run_hmac_key_is_strong_and_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_app_environment(monkeypatch)
    secret = "model-run-test-key-material-at-least-32-bytes"
    monkeypatch.setenv("APP_MODEL_PROVIDER", "provider:1001")
    monkeypatch.setenv("APP_MODEL_RUN_HMAC_KEY", secret)

    settings = load_settings_without_dotenv()

    assert settings.model_run_hmac_key is not None
    assert settings.model_run_hmac_key.get_secret_value() == secret
    assert secret not in repr(settings)


def test_enabled_source_api_requires_hmac_key(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_app_environment(monkeypatch)
    monkeypatch.setenv("APP_SOURCE_API_ENABLED", "true")

    with pytest.raises(ValidationError, match="requires source_api_hmac_key"):
        load_settings_without_dotenv()


def test_production_rejects_local_source_content_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_app_environment(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "APP_DATABASE_URL",
        "postgresql+asyncpg://db.internal/life_coach?ssl=verify-full",
    )
    monkeypatch.setenv("APP_SOURCE_API_ENABLED", "true")
    monkeypatch.setenv("APP_SOURCE_API_HMAC_KEY", "h" * 32)
    monkeypatch.setenv("APP_LOCAL_SOURCE_CONTENT_KEY", "c" * 32)

    with pytest.raises(ValidationError, match="managed Source content protector"):
        load_settings_without_dotenv()
