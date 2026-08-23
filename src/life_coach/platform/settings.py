"""Environment-backed application settings.

Secrets are represented with ``SecretStr`` so accidental model representations
do not disclose database credentials.
"""

from __future__ import annotations

from enum import StrEnum
from ipaddress import ip_address
from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class AppEnvironment(StrEnum):
    """Supported deployment environments."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _is_loopback_host(host: str) -> bool:
    normalized_host = host.rstrip(".").casefold()
    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        return True
    try:
        return ip_address(normalized_host).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    """Configuration loaded from ``APP_*`` environment variables.

    Defaults are local-only and contain no password or production credential.
    Deployments should inject the database URL from a secret manager.
    """

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
        validate_default=True,
    )

    env: AppEnvironment = AppEnvironment.DEVELOPMENT
    database_url: SecretStr = SecretStr("postgresql+asyncpg://localhost:5432/life_coach")
    object_store_endpoint: AnyHttpUrl | None = None
    object_store_bucket: str = "life-coach-dev"
    model_provider: str = "disabled"
    log_level: LogLevel = "INFO"
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    @field_validator("database_url")
    @classmethod
    def require_async_postgresql(cls, value: SecretStr) -> SecretStr:
        """Reject drivers that cannot uphold the PostgreSQL platform contract."""

        try:
            url = make_url(value.get_secret_value())
        except (ArgumentError, TypeError, ValueError):
            raise ValueError("database_url must be a valid SQLAlchemy URL") from None
        if url.drivername != "postgresql+asyncpg":
            raise ValueError("database_url must use the postgresql+asyncpg driver")
        sensitive_markers = ("password", "passwd", "secret", "token", "credential", "api_key")
        if any(marker in key.casefold() for key in url.query for marker in sensitive_markers):
            raise ValueError("database_url query parameters must not contain credentials")
        return value

    @field_validator("object_store_endpoint")
    @classmethod
    def require_safe_object_store_endpoint(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        """Keep credentials and signed parameters out of endpoint configuration."""

        if value is not None and (
            value.username is not None
            or value.password is not None
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError("object_store_endpoint must be an origin without credentials")
        return value

    @field_validator("object_store_bucket", "model_provider")
    @classmethod
    def require_non_empty_value(cls, value: str) -> str:
        """Reject blank operational identifiers."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        """Allow conventional case-insensitive log level values."""

        if isinstance(value, str):
            return value.upper()
        return value

    @model_validator(mode="after")
    def require_production_transport_security(self) -> Self:
        """Require explicit encrypted transports in production configuration."""

        if self.env is not AppEnvironment.PRODUCTION:
            return self

        database_url = make_url(self.database_dsn)
        query = database_url.query
        if query.get("ssl") != "verify-full":
            raise ValueError("production database_url must require verified TLS")
        if any(key.casefold() in {"host", "hostaddr"} for key in query):
            raise ValueError("production database_url must not override its host in query")
        if database_url.host is None or _is_loopback_host(database_url.host):
            raise ValueError("production database_url must use a non-loopback host")
        if self.object_store_endpoint is not None and self.object_store_endpoint.scheme != "https":
            raise ValueError("production object_store_endpoint must use HTTPS")
        return self

    @property
    def database_dsn(self) -> str:
        """Return the URL only at the infrastructure boundary that needs it."""

        return self.database_url.get_secret_value()
