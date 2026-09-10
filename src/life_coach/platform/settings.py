"""Environment-backed application settings.

Secrets are represented with ``SecretStr`` so accidental model representations
do not disclose database credentials.
"""

from __future__ import annotations

from enum import StrEnum
from ipaddress import ip_address
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class AppEnvironment(StrEnum):
    """Supported deployment environments."""

    DEVELOPMENT = "development"
    TEST = "test"
    DESKTOP = "desktop"
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
    source_api_enabled: bool = False
    memory_api_enabled: bool = False
    source_api_hmac_key: SecretStr | None = None
    local_source_content_key: SecretStr | None = None
    model_provider: str = "disabled"
    candidate_async_enabled: bool = False
    model_run_hmac_key: SecretStr | None = None
    compatible_api_key: SecretStr | None = None
    compatible_base_url: str = ""
    compatible_model: str = ""
    compatible_json_mode: bool = False
    stepfun_api_key: SecretStr | None = None
    stepfun_proxy_url: SecretStr | None = None
    stepfun_base_url: str = "https://api.stepfun.com/step_plan/v1"
    stepfun_model: str = "step-3.7-flash"
    stepfun_timeout_seconds: float = Field(default=20.0, gt=0, le=60)
    auth_introspection_url: AnyHttpUrl | None = None
    auth_issuer: str | None = None
    auth_audience: str | None = None
    auth_client_id: str | None = None
    auth_client_secret: SecretStr | None = None
    auth_timeout_seconds: float = Field(default=3.0, gt=0, le=15)
    local_auth_enabled: bool = False
    local_auth_principal_id: UUID | None = None
    local_auth_token: SecretStr | None = None
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

    @field_validator("object_store_bucket", "model_provider", "stepfun_base_url", "stepfun_model")
    @classmethod
    def require_non_empty_value(cls, value: str) -> str:
        """Reject blank operational identifiers."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("auth_issuer", "auth_audience", "auth_client_id")
    @classmethod
    def normalize_optional_auth_value(cls, value: str | None) -> str | None:
        """Reject configured-but-blank identity provider values."""

        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("configured authentication values must not be blank")
        return normalized

    @field_validator(
        "local_source_content_key",
        "model_run_hmac_key",
        "source_api_hmac_key",
    )
    @classmethod
    def require_strong_application_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        if len(value.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("application cryptographic keys must contain at least 32 bytes")
        return value

    @field_validator("stepfun_proxy_url")
    @classmethod
    def require_safe_stepfun_proxy(cls, value: SecretStr | None) -> SecretStr | None:
        """Allow only an explicit HTTP(S) proxy and keep credentials secret."""

        if value is None:
            return None
        raw = value.get_secret_value().strip()
        parsed = urlsplit(raw)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("stepfun_proxy_url must be an HTTP(S) proxy origin")
        return SecretStr(raw.rstrip("/"))

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

        if self.model_provider != "disabled" and self.model_run_hmac_key is None:
            raise ValueError("an enabled model provider requires model_run_hmac_key")
        if self.model_provider == "desktop-compatible":
            if self.env not in {AppEnvironment.DESKTOP, AppEnvironment.TEST}:
                raise ValueError("custom endpoints require the desktop trust boundary")
            if self.compatible_api_key is None:
                raise ValueError("custom endpoints require an API key")
        if self.candidate_async_enabled and self.model_provider == "disabled":
            raise ValueError("candidate background jobs require an enabled model provider")
        if self.model_provider == "stepfun-step-plan" and self.stepfun_api_key is None:
            raise ValueError("the StepFun provider requires stepfun_api_key")
        if self.stepfun_api_key is not None and self.model_provider != "stepfun-step-plan":
            raise ValueError("stepfun_api_key requires the StepFun provider")
        if self.source_api_enabled and self.source_api_hmac_key is None:
            raise ValueError("an enabled Source API requires source_api_hmac_key")
        local_auth_values = (self.local_auth_principal_id, self.local_auth_token)
        if self.local_auth_enabled and not all(value is not None for value in local_auth_values):
            raise ValueError("enabled local authentication requires principal_id and token")
        if not self.local_auth_enabled and any(value is not None for value in local_auth_values):
            raise ValueError("local authentication values require the explicit enable flag")
        oidc_values = (
            self.auth_introspection_url,
            self.auth_issuer,
            self.auth_audience,
            self.auth_client_id,
            self.auth_client_secret,
        )
        if self.local_auth_enabled and any(value is not None for value in oidc_values):
            raise ValueError("local authentication and OIDC configuration are mutually exclusive")
        if self.env is not AppEnvironment.PRODUCTION:
            if self.env is AppEnvironment.DESKTOP:
                host = make_url(self.database_dsn).host
                if host != "127.0.0.1" or not self.local_auth_enabled:
                    raise ValueError("desktop requires authenticated loopback storage")
                if self.model_provider not in {
                    "disabled", "stepfun-step-plan", "desktop-compatible",
                }:
                    raise ValueError("desktop must not enable synthetic model providers")
            return self

        if self.local_auth_enabled:
            raise ValueError("production must not enable local authentication")

        if self.local_source_content_key is not None:
            raise ValueError("production must inject a managed Source content protector")

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
        if (
            self.auth_introspection_url is not None
            and self.auth_introspection_url.scheme != "https"
        ):
            raise ValueError("production auth_introspection_url must use HTTPS")
        return self

    @property
    def database_dsn(self) -> str:
        """Return the URL only at the infrastructure boundary that needs it."""

        return self.database_url.get_secret_value()
