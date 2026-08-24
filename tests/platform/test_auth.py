"""Authentication, membership, and authorized session composition tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from life_coach.modules.identity.models import MembershipRole
from life_coach.platform.auth import (
    AuthenticatedPrincipal,
    AuthenticationDenied,
    AuthorizedVaultContext,
    OidcIntrospectionAuthenticator,
    ProductionSessionFactory,
    SqlPrincipalVaultMembershipAuthorizer,
    VaultMembershipDenied,
    extract_bearer_token,
)
from life_coach.platform.database import AsyncSessionFactory, VaultAsyncSession


class _Authenticator:
    def __init__(self, principal: AuthenticatedPrincipal) -> None:
        self.principal = principal
        self.tokens: list[SecretStr] = []

    async def authenticate(self, access_token: SecretStr) -> AuthenticatedPrincipal:
        self.tokens.append(access_token)
        return self.principal


class _Authorizer:
    def __init__(self, context: AuthorizedVaultContext) -> None:
        self.context = context
        self.calls: list[tuple[object, object, object]] = []

    async def authorize(
        self,
        session: VaultAsyncSession,
        *,
        principal_id: object,
        vault_id: object,
    ) -> AuthorizedVaultContext:
        self.calls.append((session, principal_id, vault_id))
        return self.context


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Basic abc", "Bearer", "Bearer ", "Bearer two tokens", "Bearer\ttoken"],
)
def test_bearer_parser_fails_closed_without_reflecting_input(
    authorization: str | None,
) -> None:
    with pytest.raises(AuthenticationDenied) as exc_info:
        extract_bearer_token(authorization)

    if authorization:
        assert authorization not in str(exc_info.value)


def test_bearer_parser_returns_secret_value() -> None:
    token = extract_bearer_token("bEaReR opaque-token")

    assert isinstance(token, SecretStr)
    assert token.get_secret_value() == "opaque-token"
    assert "opaque-token" not in repr(token)


async def test_production_factory_binds_identity_membership_and_transaction() -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    principal_id, vault_id = uuid4(), uuid4()
    principal = AuthenticatedPrincipal(principal_id, now + timedelta(minutes=5))
    context = AuthorizedVaultContext(principal_id, vault_id, MembershipRole.OWNER, 3)
    authenticator = _Authenticator(principal)
    authorizer = _Authorizer(context)
    session = cast(VaultAsyncSession, object())
    transaction_events: list[str] = []

    @asynccontextmanager
    async def fake_transaction(
        _session_factory: AsyncSessionFactory,
        requested_vault_id: object,
    ):
        assert requested_vault_id == vault_id
        transaction_events.append("open")
        try:
            yield session
        finally:
            transaction_events.append("close")

    factory = ProductionSessionFactory(
        session_factory=cast(AsyncSessionFactory, object()),
        authenticator=authenticator,
        membership_authorizer=authorizer,
        clock=lambda: now,
    )
    with patch("life_coach.platform.auth.vault_transaction", fake_transaction):
        async with factory.open(
            authorization="Bearer opaque-token",
            vault_id=str(vault_id),
        ) as authorized:
            assert transaction_events == ["open"]
            assert authorized.session is session
            assert authorized.context == context

    assert transaction_events == ["open", "close"]
    assert authenticator.tokens[0].get_secret_value() == "opaque-token"
    assert authorizer.calls == [(session, principal_id, vault_id)]


async def test_expired_authentication_never_opens_a_vault_transaction() -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    principal = AuthenticatedPrincipal(uuid4(), now)
    factory = ProductionSessionFactory(
        session_factory=cast(AsyncSessionFactory, object()),
        authenticator=_Authenticator(principal),
        clock=lambda: now,
    )

    with (
        patch("life_coach.platform.auth.vault_transaction") as transaction,
        pytest.raises(AuthenticationDenied),
    ):
        async with factory.open(authorization="Bearer expired", vault_id=uuid4()):
            pytest.fail("an expired credential must never yield a session")

    transaction.assert_not_called()


async def test_authorizer_cannot_return_a_context_for_another_identity_or_vault() -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    principal_id, vault_id = uuid4(), uuid4()
    principal = AuthenticatedPrincipal(principal_id, now + timedelta(minutes=5))
    forged = AuthorizedVaultContext(uuid4(), vault_id, MembershipRole.MEMBER, 1)

    @asynccontextmanager
    async def fake_transaction(_factory: AsyncSessionFactory, _vault_id: object):
        yield cast(VaultAsyncSession, object())

    factory = ProductionSessionFactory(
        session_factory=cast(AsyncSessionFactory, object()),
        authenticator=_Authenticator(principal),
        membership_authorizer=_Authorizer(forged),
        clock=lambda: now,
    )
    with (
        patch("life_coach.platform.auth.vault_transaction", fake_transaction),
        pytest.raises(VaultMembershipDenied),
    ):
        async with factory.open(authorization="Bearer token", vault_id=vault_id):
            pytest.fail("a mismatched authorization context must not be yielded")


async def test_sql_authorizer_accepts_only_an_active_exact_membership() -> None:
    principal_id, vault_id = uuid4(), uuid4()
    session = AsyncMock(spec=VaultAsyncSession)
    result = AsyncMock()
    result.one_or_none = lambda: (MembershipRole.MEMBER, 4)
    session.execute.return_value = result

    context = await SqlPrincipalVaultMembershipAuthorizer().authorize(
        session,
        principal_id=principal_id,
        vault_id=vault_id,
    )

    assert context == AuthorizedVaultContext(principal_id, vault_id, MembershipRole.MEMBER, 4)
    statement = session.execute.await_args.args[0]
    sql = str(statement)
    assert "vault_membership.principal_id" in sql
    assert "vault_membership.vault_id" in sql
    assert "vault_membership.revoked_at IS NULL" in sql


async def test_sql_authorizer_denies_missing_or_revoked_membership() -> None:
    session = AsyncMock(spec=VaultAsyncSession)
    result = AsyncMock()
    result.one_or_none = lambda: None
    session.execute.return_value = result

    with pytest.raises(VaultMembershipDenied):
        await SqlPrincipalVaultMembershipAuthorizer().authorize(
            session,
            principal_id=uuid4(),
            vault_id=uuid4(),
        )


async def test_oidc_introspection_validates_authority_and_internal_principal_claim() -> None:
    principal_id = uuid4()
    observed_authorization = ""
    observed_token = ""

    def introspect(request: httpx.Request) -> httpx.Response:
        nonlocal observed_authorization, observed_token
        observed_authorization = request.headers["Authorization"]
        observed_token = request.content.decode()
        return httpx.Response(
            200,
            json={
                "active": True,
                "iss": "https://id.example",
                "aud": ["life-coach-api"],
                "sub": "external-subject-never-returned",
                "exp": 2_000_000_000,
                "principal_id": str(principal_id),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(introspect)) as client:
        adapter = OidcIntrospectionAuthenticator(
            http_client=client,
            endpoint="https://id.example/introspect",
            client_id="life-coach",
            client_secret=SecretStr("client-secret"),
            expected_issuer="https://id.example",
            expected_audience="life-coach-api",
        )
        principal = await adapter.authenticate(SecretStr("access-token"))

    assert principal.principal_id == principal_id
    assert principal.expires_at == datetime.fromtimestamp(2_000_000_000, tz=UTC)
    assert observed_authorization.startswith("Basic ")
    assert "client-secret" not in observed_authorization
    assert observed_token == "token=access-token"


@pytest.mark.parametrize(
    "payload",
    [
        {"active": False},
        {
            "active": True,
            "iss": "https://wrong.example",
            "aud": "life-coach-api",
            "sub": "subject",
            "exp": 2_000_000_000,
            "principal_id": str(uuid4()),
        },
        {
            "active": True,
            "iss": "https://id.example",
            "aud": "wrong-api",
            "sub": "subject",
            "exp": 2_000_000_000,
            "principal_id": str(uuid4()),
        },
    ],
)
async def test_oidc_introspection_denies_inactive_or_wrong_authority(
    payload: dict[str, object],
) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = OidcIntrospectionAuthenticator(
            http_client=client,
            endpoint="https://id.example/introspect",
            client_id="life-coach",
            client_secret=SecretStr("client-secret"),
            expected_issuer="https://id.example",
            expected_audience="life-coach-api",
        )
        with pytest.raises(AuthenticationDenied):
            await adapter.authenticate(SecretStr("never-reflected"))


async def test_oidc_introspection_denies_expired_or_not_yet_active_tokens() -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    principal_id = uuid4()

    async def denied(payload: dict[str, object]) -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
        async with httpx.AsyncClient(transport=transport) as client:
            adapter = OidcIntrospectionAuthenticator(
                http_client=client,
                endpoint="https://id.example/introspect",
                client_id="life-coach",
                client_secret=SecretStr("client-secret"),
                expected_issuer="https://id.example",
                expected_audience="life-coach-api",
                clock=lambda: now,
            )
            with pytest.raises(AuthenticationDenied):
                await adapter.authenticate(SecretStr("never-reflected"))

    base: dict[str, object] = {
        "active": True,
        "iss": "https://id.example",
        "aud": "life-coach-api",
        "sub": "subject",
        "principal_id": str(principal_id),
    }
    await denied({**base, "exp": now.timestamp()})
    await denied(
        {
            **base,
            "exp": (now + timedelta(minutes=10)).timestamp(),
            "nbf": (now + timedelta(minutes=1)).timestamp(),
        }
    )
