"""Authentication-to-Vault authorization and production session composition.

The request's Vault identifier is routing input, never proof of authorization.
An access-token adapter first returns a trusted internal principal identifier. A
Vault-scoped transaction is then opened and the active membership is checked in
that same transaction before any business repository receives the session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import httpx
from pydantic import SecretStr
from sqlalchemy import select

from life_coach.modules.identity.models import MembershipRole, VaultMembership
from life_coach.platform.database import (
    AsyncSessionFactory,
    VaultAsyncSession,
    validate_vault_id,
    vault_transaction,
)

_MAX_AUTHORIZATION_LENGTH = 16_384


class AuthenticationDenied(RuntimeError):
    """The request did not present a valid authenticated principal."""


class VaultMembershipDenied(RuntimeError):
    """The authenticated principal has no active membership in the requested Vault."""


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """Minimal result emitted only by a trusted access-token adapter."""

    principal_id: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthorizedVaultContext:
    """Identity and authorization facts bound to one database transaction."""

    principal_id: UUID
    vault_id: UUID
    role: MembershipRole
    membership_generation: int


@dataclass(frozen=True, slots=True)
class AuthorizedVaultSession:
    """The only business-facing session value produced by the composition root."""

    context: AuthorizedVaultContext
    session: VaultAsyncSession


class AccessTokenAuthenticator(Protocol):
    """Port implemented by an OIDC/JWT or token-introspection adapter."""

    async def authenticate(self, access_token: SecretStr) -> AuthenticatedPrincipal:
        """Validate a token and return only trusted internal identity facts."""
        ...


class OidcIntrospectionAuthenticator:
    """RFC 7662-style OIDC adapter with strict issuer/audience/expiry checks.

    The identity provider must emit an internal UUID claim (``principal_id`` by
    default). External ``sub`` values never become application identifiers and
    are never logged or persisted by this adapter.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        endpoint: str,
        client_id: str,
        client_secret: SecretStr,
        expected_issuer: str,
        expected_audience: str,
        principal_claim: str = "principal_id",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not all(
            value and value == value.strip()
            for value in (
                endpoint,
                client_id,
                expected_issuer,
                expected_audience,
                principal_claim,
            )
        ):
            raise ValueError("OIDC introspection configuration must not be blank")
        if httpx.URL(endpoint).scheme != "https":
            raise ValueError("OIDC introspection endpoint must use HTTPS")
        if not client_secret.get_secret_value():
            raise ValueError("OIDC introspection client secret must not be blank")
        self._http_client = http_client
        self._endpoint = endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._expected_issuer = expected_issuer
        self._expected_audience = expected_audience
        self._principal_claim = principal_claim
        self._clock = clock or (lambda: datetime.now(UTC))

    async def authenticate(self, access_token: SecretStr) -> AuthenticatedPrincipal:
        try:
            response = await self._http_client.post(
                self._endpoint,
                data={"token": access_token.get_secret_value()},
                auth=httpx.BasicAuth(
                    self._client_id,
                    self._client_secret.get_secret_value(),
                ),
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise AuthenticationDenied("authentication could not be verified") from None

        if not isinstance(payload, dict) or payload.get("active") is not True:
            raise AuthenticationDenied("authentication is invalid")
        if payload.get("iss") != self._expected_issuer:
            raise AuthenticationDenied("authentication is invalid")

        audience = payload.get("aud")
        audiences = {audience} if isinstance(audience, str) else audience
        if (
            not isinstance(audiences, (list, set, tuple))
            or not all(isinstance(value, str) for value in audiences)
            or self._expected_audience not in audiences
        ):
            raise AuthenticationDenied("authentication is invalid")

        expires_at_value = payload.get("exp")
        principal_value = payload.get(self._principal_claim)
        subject = payload.get("sub")
        if (
            isinstance(expires_at_value, bool)
            or not isinstance(expires_at_value, (int, float))
            or not isinstance(principal_value, str)
            or not isinstance(subject, str)
            or not subject
        ):
            raise AuthenticationDenied("authentication is invalid")
        try:
            principal_id = UUID(principal_value)
            expires_at = datetime.fromtimestamp(expires_at_value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            raise AuthenticationDenied("authentication is invalid") from None
        now = self._clock()
        if now.tzinfo is None or expires_at <= now:
            raise AuthenticationDenied("authentication is expired")

        not_before = payload.get("nbf")
        if not_before is not None:
            if isinstance(not_before, bool) or not isinstance(not_before, (int, float)):
                raise AuthenticationDenied("authentication is invalid")
            if now.timestamp() < not_before:
                raise AuthenticationDenied("authentication is not active")
        return AuthenticatedPrincipal(principal_id=principal_id, expires_at=expires_at)


class PrincipalVaultMembershipAuthorizer(Protocol):
    """Port for the authority that resolves active Vault membership."""

    async def authorize(
        self,
        session: VaultAsyncSession,
        *,
        principal_id: UUID,
        vault_id: UUID,
    ) -> AuthorizedVaultContext:
        """Return an authorization context or deny without exposing membership state."""
        ...


def extract_bearer_token(authorization: str | None) -> SecretStr:
    """Parse a strict Bearer credential without reflecting it in failures."""

    if authorization is None or len(authorization) > _MAX_AUTHORIZATION_LENGTH:
        raise AuthenticationDenied("authentication is required")
    scheme, separator, credential = authorization.partition(" ")
    if (
        separator != " "
        or scheme.casefold() != "bearer"
        or not credential
        or credential != credential.strip()
        or any(character.isspace() for character in credential)
    ):
        raise AuthenticationDenied("authentication is required")
    return SecretStr(credential)


class SqlPrincipalVaultMembershipAuthorizer:
    """Read the authoritative active membership through the already scoped session."""

    async def authorize(
        self,
        session: VaultAsyncSession,
        *,
        principal_id: UUID,
        vault_id: UUID,
    ) -> AuthorizedVaultContext:
        statement = select(
            VaultMembership.role,
            VaultMembership.generation,
        ).where(
            VaultMembership.vault_id == vault_id,
            VaultMembership.principal_id == principal_id,
            VaultMembership.revoked_at.is_(None),
        )
        membership = (await session.execute(statement)).one_or_none()
        if membership is None:
            raise VaultMembershipDenied("vault membership is unavailable")
        role, generation = membership
        return AuthorizedVaultContext(
            principal_id=principal_id,
            vault_id=vault_id,
            role=role,
            membership_generation=generation,
        )


class ProductionSessionFactory:
    """Compose authentication, RLS scope, membership, and transaction lifetime."""

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        authenticator: AccessTokenAuthenticator,
        membership_authorizer: PrincipalVaultMembershipAuthorizer | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._authenticator = authenticator
        self._membership_authorizer = (
            membership_authorizer or SqlPrincipalVaultMembershipAuthorizer()
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    @asynccontextmanager
    async def open(
        self,
        *,
        authorization: str | None,
        vault_id: UUID | str,
    ) -> AsyncIterator[AuthorizedVaultSession]:
        """Yield exactly one authorized transaction; rollback on every denial/failure."""

        token = extract_bearer_token(authorization)
        principal = await self._authenticator.authenticate(token)
        now = self._clock()
        if now.tzinfo is None or principal.expires_at.tzinfo is None:
            raise AuthenticationDenied("authentication is invalid")
        if principal.expires_at <= now:
            raise AuthenticationDenied("authentication is expired")

        normalized_vault_id = validate_vault_id(vault_id)
        async with vault_transaction(self._session_factory, normalized_vault_id) as session:
            context = await self._membership_authorizer.authorize(
                session,
                principal_id=principal.principal_id,
                vault_id=normalized_vault_id,
            )
            if (
                context.principal_id != principal.principal_id
                or context.vault_id != normalized_vault_id
                or context.membership_generation <= 0
            ):
                raise VaultMembershipDenied("vault membership is unavailable")
            yield AuthorizedVaultSession(context=context, session=session)


__all__ = [
    "AccessTokenAuthenticator",
    "AuthenticatedPrincipal",
    "AuthenticationDenied",
    "AuthorizedVaultContext",
    "AuthorizedVaultSession",
    "OidcIntrospectionAuthenticator",
    "PrincipalVaultMembershipAuthorizer",
    "ProductionSessionFactory",
    "SqlPrincipalVaultMembershipAuthorizer",
    "VaultMembershipDenied",
    "extract_bearer_token",
]
