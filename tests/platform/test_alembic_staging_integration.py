"""Destructive migration rehearsal in a disposable PostgreSQL database.

Set ``TEST_POSTGRES_DSN`` to an administrative PostgreSQL database. The test creates
and drops a uniquely named sibling database, exercises upgrade -> downgrade ->
upgrade, and then validates the migrated schema through the non-owner runtime role.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from life_coach.api.app import create_app
from life_coach.application.source_entries import (
    LocalAesGcmSourceContentProtector,
)
from life_coach.platform.auth import AuthenticatedPrincipal
from life_coach.platform.settings import AppEnvironment, Settings

_ADMIN_DSN = os.getenv("TEST_POSTGRES_DSN")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_ROLE = "life_coach_app"
_RUNTIME_PASSWORD = "synthetic_source_runtime_password"

pytestmark = pytest.mark.skipif(
    not _ADMIN_DSN,
    reason="set TEST_POSTGRES_DSN to run the disposable staging migration drill",
)


def _async_url(value: str) -> URL:
    url = make_url(value)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+asyncpg")
    if url.drivername != "postgresql+asyncpg":
        pytest.fail("TEST_POSTGRES_DSN must use PostgreSQL/asyncpg")
    return url


def _run_alembic(database_url: URL, command: str, target: str) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "APP_DATABASE_URL": database_url.render_as_string(hide_password=False),
            "PYTHONPATH": str(_PROJECT_ROOT / "src"),
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "alembic/alembic.ini",
            command,
            target,
        ],
        cwd=_PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def _set_scope(connection: AsyncConnection, vault_id: uuid.UUID) -> None:
    await connection.execute(
        text("SELECT pg_catalog.set_config('app.vault_id', :vault_id, true)"),
        {"vault_id": str(vault_id)},
    )


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", getattr(error.orig, "pgcode", None))


class _StaticAuthenticator:
    def __init__(self, principal_id: uuid.UUID) -> None:
        self._principal_id = principal_id

    async def authenticate(self, _access_token: SecretStr) -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            principal_id=self._principal_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


async def _exercise_source_api(
    *,
    database_url: URL,
    principal_id: uuid.UUID,
    vault_id: uuid.UUID,
) -> None:
    runtime_engine = create_async_engine(database_url, pool_size=1, max_overflow=0)

    async def ready() -> None:
        return None

    app = create_app(
        settings=Settings(
            _env_file=None,
            env=AppEnvironment.TEST,
            database_url=SecretStr(database_url.render_as_string(hide_password=False)),
            source_api_enabled=True,
            source_api_hmac_key=SecretStr("s" * 32),
        ),
        engine=runtime_engine,
        readiness_probe=ready,
        authenticator=_StaticAuthenticator(principal_id),
        source_content_protector=LocalAesGcmSourceContentProtector(b"c" * 32),
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {
        "Authorization": "Bearer synthetic-staging-token",
        "X-Vault-ID": str(vault_id),
        "Idempotency-Key": "source-create-staging-1",
    }
    plaintext = "synthetic source canary: I completed one deliberate step."
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            capabilities = await client.get("/health/capabilities")
            assert capabilities.json()["features"]["entries"] is True

            created = await client.post(
                "/v1/entries",
                headers=headers,
                json={"content": plaintext, "client_id": "staging-device-1"},
            )
            assert created.status_code == 201, created.text
            entry_id = created.json()["id"]
            assert created.json()["revision"] == 1

            replay = await client.post(
                "/v1/entries",
                headers=headers,
                json={"content": plaintext, "client_id": "staging-device-1"},
            )
            assert replay.status_code == 201, replay.text
            assert replay.json()["id"] == entry_id

            read = await client.get(
                f"/v1/entries/{entry_id}",
                headers={
                    "Authorization": headers["Authorization"],
                    "X-Vault-ID": headers["X-Vault-ID"],
                },
            )
            assert read.status_code == 200, read.text
            assert read.json()["content"] == plaintext

            replacement = "synthetic source canary: I corrected the deliberate step."
            revision_headers = {
                **headers,
                "Idempotency-Key": "source-append-staging-1",
                "If-Match": '"1"',
            }
            revised = await client.patch(
                f"/v1/entries/{entry_id}",
                headers=revision_headers,
                json={"content": replacement},
            )
            assert revised.status_code == 200, revised.text
            assert revised.json()["revision"] == 2

            revised_replay = await client.patch(
                f"/v1/entries/{entry_id}",
                headers=revision_headers,
                json={"content": replacement},
            )
            assert revised_replay.status_code == 200, revised_replay.text
            assert revised_replay.json()["revision"] == 2

            delete_headers = {
                **headers,
                "Idempotency-Key": "source-delete-staging-1",
                "If-Match": '"2"',
            }
            deleted = await client.delete(
                f"/v1/entries/{entry_id}",
                headers=delete_headers,
            )
            assert deleted.status_code == 202, deleted.text
            assert deleted.json()["tombstoned"] is True

            delete_replay = await client.delete(
                f"/v1/entries/{entry_id}",
                headers=delete_headers,
            )
            assert delete_replay.status_code == 202, delete_replay.text
            assert delete_replay.json() == deleted.json()

            hidden = await client.get(
                f"/v1/entries/{entry_id}",
                headers={
                    "Authorization": headers["Authorization"],
                    "X-Vault-ID": headers["X-Vault-ID"],
                },
            )
            assert hidden.status_code == 404
            assert plaintext not in hidden.text
            assert replacement not in hidden.text
    finally:
        await runtime_engine.dispose()


@pytest.mark.asyncio
async def test_alembic_rehearsal_and_migrated_non_owner_runtime_boundaries() -> None:
    assert _ADMIN_DSN is not None
    admin_url = _async_url(_ADMIN_DSN)
    database_name = f"lc_stage_{uuid.uuid4().hex[:12]}"
    staging_url = admin_url.set(database=database_name)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    staging_engine = None
    role_granted = False
    runtime_role = f"lc_runtime_{uuid.uuid4().hex[:12]}"
    runtime_role_created = False

    try:
        async with admin_engine.connect() as connection:
            await connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')

        _run_alembic(staging_url, "upgrade", "head")
        _run_alembic(staging_url, "downgrade", "base")
        _run_alembic(staging_url, "upgrade", "head")

        staging_engine = create_async_engine(staging_url, pool_size=1, max_overflow=0)
        vault_id, other_vault_id, principal_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        model_run_id, model_input_id = uuid.uuid4(), uuid.uuid4()
        source_document_id = uuid.uuid4()
        source_revision_id = uuid.uuid4()
        source_receipt_id = uuid.uuid4()
        now = datetime.now(UTC)
        async with staging_engine.begin() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "7e2c1f9a6b40"
            await connection.execute(
                text(
                    "INSERT INTO principal "
                    "(id, issuer, subject_fingerprint, disabled_at, created_at, updated_at) "
                    "VALUES (:id, :issuer, :fingerprint, NULL, :now, :now)"
                ),
                {
                    "id": principal_id,
                    "issuer": "https://id.staging.example",
                    "fingerprint": "a" * 64,
                    "now": now,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO vault "
                    "(id, policy_epoch, source_generation, deleted_at, created_by, data_class, "
                    "created_at, updated_at) "
                    "VALUES (:id, 0, 0, NULL, 'user', 'sensitive', :now, :now)"
                ),
                {"id": vault_id, "now": now},
            )
            await connection.execute(
                text(
                    "INSERT INTO vault "
                    "(id, policy_epoch, source_generation, deleted_at, created_by, data_class, "
                    "created_at, updated_at) "
                    "VALUES (:id, 0, 0, NULL, 'user', 'sensitive', :now, :now)"
                ),
                {"id": other_vault_id, "now": now},
            )
            await connection.execute(
                text(
                    "INSERT INTO vault_membership "
                    "(id, vault_id, principal_id, role, generation, revoked_at, "
                    "created_at, updated_at) "
                    "VALUES (:id, :vault_id, :principal_id, 'owner', 1, NULL, :now, :now)"
                ),
                {
                    "id": uuid.uuid4(),
                    "vault_id": vault_id,
                    "principal_id": principal_id,
                    "now": now,
                },
            )
            await connection.exec_driver_sql(f'GRANT "{_APP_ROLE}" TO CURRENT_USER')
            role_granted = True
            await connection.exec_driver_sql(
                f"CREATE ROLE \"{runtime_role}\" LOGIN PASSWORD '{_RUNTIME_PASSWORD}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS"
            )
            await connection.exec_driver_sql(f'GRANT "{_APP_ROLE}" TO "{runtime_role}"')
            runtime_role_created = True

        await _exercise_source_api(
            database_url=staging_url.set(
                username=runtime_role,
                password=_RUNTIME_PASSWORD,
            ),
            principal_id=principal_id,
            vault_id=vault_id,
        )

        async with staging_engine.connect() as connection:
            async with connection.begin():
                await connection.exec_driver_sql(f'SET ROLE "{_APP_ROLE}"')
            async with connection.begin():
                assert await connection.scalar(text("SELECT count(*) FROM vault_membership")) == 0
                assert await connection.scalar(text("SELECT count(*) FROM model_run")) == 0
            async with connection.begin():
                await _set_scope(connection, vault_id)
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM vault_membership "
                            "WHERE principal_id = :principal_id"
                        ),
                        {"principal_id": principal_id},
                    )
                    == 1
                )
                await connection.execute(
                    text(
                        "INSERT INTO source_document "
                        "(id, vault_id, source_type, origin, current_revision_id, title, "
                        "event_time_hint, capture_timezone, processing_state, "
                        "retention_policy_id, created_at, updated_at, created_by, "
                        "data_class, deleted_at) VALUES "
                        "(:id, :vault_id, 'note', 'first_party', NULL, NULL, NULL, "
                        "'UTC', 'pending', NULL, :now, :now, 'user', 'sensitive', NULL)"
                    ),
                    {"id": source_document_id, "vault_id": vault_id, "now": now},
                )
                await connection.execute(
                    text(
                        "INSERT INTO source_revision "
                        "(id, vault_id, document_id, revision_no, content_ciphertext, "
                        "object_key, content_mime, content_hash, language, created_at, "
                        "supersedes_revision_id, edit_origin, created_by, data_class, deleted_at) "
                        "VALUES (:id, :vault_id, :document_id, 1, :ciphertext, NULL, "
                        "'text/plain', :content_hash, NULL, :now, NULL, 'user', "
                        "'user', 'sensitive', NULL)"
                    ),
                    {
                        "id": source_revision_id,
                        "vault_id": vault_id,
                        "document_id": source_document_id,
                        "ciphertext": b"synthetic-staging-ciphertext",
                        "content_hash": "sha256:" + "e" * 64,
                        "now": now,
                    },
                )
                await connection.execute(
                    text(
                        "UPDATE source_document SET current_revision_id = :revision_id "
                        "WHERE id = :document_id AND vault_id = :vault_id"
                    ),
                    {
                        "revision_id": source_revision_id,
                        "document_id": source_document_id,
                        "vault_id": vault_id,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO source_command_receipt "
                        "(id, vault_id, operation, client_key_hash, request_hash, "
                        "resource_id, resource_revision_id, result_revision_no, "
                        "source_generation, policy_epoch, created_at) VALUES "
                        "(:id, :vault_id, 'source.create', :client_key_hash, :request_hash, "
                        ":resource_id, :revision_id, 1, 0, 0, :now)"
                    ),
                    {
                        "id": source_receipt_id,
                        "vault_id": vault_id,
                        "client_key_hash": f"hmac-sha256:v1:{'f' * 64}",
                        "request_hash": f"hmac-sha256:v1:{'1' * 64}",
                        "resource_id": source_document_id,
                        "revision_id": source_revision_id,
                        "now": now,
                    },
                )
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM source_command_receipt WHERE id = :id"),
                        {"id": source_receipt_id},
                    )
                    == 1
                )
                await connection.execute(
                    text(
                        "INSERT INTO model_run "
                        "(id, vault_id, task_type, idempotency_key, request_hash, "
                        "task_definition_hash, provider, model, model_revision, "
                        "prompt_template_version, schema_version, pipeline_version, "
                        "consent_snapshot_id, policy_epoch, source_generation, "
                        "actual_sensitivity, data_residency, retention_policy, state, "
                        "attempt, dispatch_generation, authorized_at) VALUES "
                        "(:id, :vault_id, 'candidate_insight', :idempotency_key, :request_hash, "
                        ":task_hash, 'provider:1001', 'model:1001', 'rev-1', 'prompt-v1', "
                        "'schema-v1', 'pipeline-v1', :consent_snapshot_id, 0, 0, 'sensitive', "
                        "'region:1001', 'zero_retention', 'authorized', 0, 0, :now)"
                    ),
                    {
                        "id": model_run_id,
                        "vault_id": vault_id,
                        "idempotency_key": f"modelrun:{uuid.uuid4().hex}",
                        "request_hash": f"hmac-sha256:v1:{'a' * 64}",
                        "task_hash": f"hmac-sha256:v1:{'b' * 64}",
                        "consent_snapshot_id": f"consent:{'c' * 64}",
                        "now": now,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO model_run_input "
                        "(id, vault_id, model_run_id, kind, object_id, "
                        "content_fingerprint, ordinal) VALUES "
                        "(:id, :vault_id, :model_run_id, 'source_fragment', :object_id, "
                        ":content_fingerprint, 0)"
                    ),
                    {
                        "id": model_input_id,
                        "vault_id": vault_id,
                        "model_run_id": model_run_id,
                        "object_id": uuid.uuid4(),
                        "content_fingerprint": f"hmac-sha256:v1:{'d' * 64}",
                    },
                )
                generation = await connection.scalar(
                    text(
                        "UPDATE model_run SET state = 'dispatching', attempt = attempt + 1, "
                        "dispatch_generation = dispatch_generation + 1, "
                        "dispatch_started_at = clock_timestamp(), "
                        "dispatch_expires_at = clock_timestamp() + interval '30 seconds' "
                        "WHERE id = :id AND vault_id = :vault_id AND state = 'authorized' "
                        "RETURNING dispatch_generation"
                    ),
                    {"id": model_run_id, "vault_id": vault_id},
                )
                assert generation == 1
                finalized = await connection.scalar(
                    text(
                        "UPDATE model_run SET state = 'succeeded', "
                        "io_finished_at = clock_timestamp(), completed_at = clock_timestamp() "
                        "WHERE id = :id AND vault_id = :vault_id AND state = 'dispatching' "
                        "AND dispatch_generation = :generation RETURNING id"
                    ),
                    {"id": model_run_id, "vault_id": vault_id, "generation": generation},
                )
                assert finalized == model_run_id
                stale_finalize = await connection.scalar(
                    text(
                        "UPDATE model_run SET state = 'failed', "
                        "safe_error_code = 'provider.failed' "
                        "WHERE id = :id AND vault_id = :vault_id AND state = 'dispatching' "
                        "AND dispatch_generation = :generation RETURNING id"
                    ),
                    {"id": model_run_id, "vault_id": vault_id, "generation": generation},
                )
                assert stale_finalize is None
            with pytest.raises(DBAPIError) as cross_vault_insert:
                async with connection.begin():
                    await _set_scope(connection, vault_id)
                    await connection.execute(
                        text(
                            "INSERT INTO model_run "
                            "(id, vault_id, task_type, idempotency_key, request_hash, "
                            "task_definition_hash, provider, model, model_revision, "
                            "prompt_template_version, schema_version, pipeline_version, "
                            "consent_snapshot_id, policy_epoch, source_generation, "
                            "actual_sensitivity, data_residency, retention_policy, state, "
                            "attempt, dispatch_generation, provider_request_id, safe_error_code, "
                            "authorized_at, dispatch_started_at, dispatch_expires_at, "
                            "io_finished_at, completed_at) "
                            "SELECT :new_id, :other_vault_id, task_type, :idempotency_key, "
                            "request_hash, task_definition_hash, provider, model, model_revision, "
                            "prompt_template_version, schema_version, pipeline_version, "
                            "consent_snapshot_id, policy_epoch, source_generation, "
                            "actual_sensitivity, data_residency, retention_policy, state, "
                            "attempt, dispatch_generation, provider_request_id, safe_error_code, "
                            "authorized_at, dispatch_started_at, dispatch_expires_at, "
                            "io_finished_at, completed_at FROM model_run WHERE id = :source_id"
                        ),
                        {
                            "new_id": uuid.uuid4(),
                            "other_vault_id": other_vault_id,
                            "idempotency_key": f"modelrun:{uuid.uuid4().hex}",
                            "source_id": model_run_id,
                        },
                    )
            assert _sqlstate(cross_vault_insert.value) == "42501"
            with pytest.raises(DBAPIError) as membership_write:
                async with connection.begin():
                    await _set_scope(connection, vault_id)
                    await connection.execute(
                        text(
                            "UPDATE vault_membership SET generation = generation + 1 "
                            "WHERE principal_id = :principal_id"
                        ),
                        {"principal_id": principal_id},
                    )
            assert _sqlstate(membership_write.value) == "42501"
            with pytest.raises(DBAPIError) as receipt_delete:
                async with connection.begin():
                    await _set_scope(connection, vault_id)
                    await connection.execute(
                        text("DELETE FROM model_run WHERE id = :id"),
                        {"id": model_run_id},
                    )
            assert _sqlstate(receipt_delete.value) == "42501"
            with pytest.raises(DBAPIError) as source_receipt_delete:
                async with connection.begin():
                    await _set_scope(connection, vault_id)
                    await connection.execute(
                        text("DELETE FROM source_command_receipt WHERE id = :id"),
                        {"id": source_receipt_id},
                    )
            assert _sqlstate(source_receipt_delete.value) in {"42501", "55000"}
            with pytest.raises(DBAPIError) as source_receipt_mutation:
                async with connection.begin():
                    await _set_scope(connection, vault_id)
                    await connection.execute(
                        text(
                            "UPDATE source_command_receipt "
                            "SET result_revision_no = 2 WHERE id = :id"
                        ),
                        {"id": source_receipt_id},
                    )
            assert _sqlstate(source_receipt_mutation.value) in {"42501", "55000"}
            with pytest.raises(DBAPIError) as input_mutation:
                async with connection.begin():
                    await _set_scope(connection, vault_id)
                    await connection.execute(
                        text("UPDATE model_run_input SET ordinal = 1 WHERE id = :id"),
                        {"id": model_input_id},
                    )
            assert _sqlstate(input_mutation.value) in {"42501", "55000"}
            async with connection.begin():
                await connection.exec_driver_sql("RESET ROLE")
                owner = await connection.scalar(
                    text(
                        "SELECT tableowner FROM pg_catalog.pg_tables "
                        "WHERE schemaname = 'public' AND tablename = 'vault_membership'"
                    )
                )
                assert owner != _APP_ROLE
                receipt_security = (
                    await connection.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity "
                            "FROM pg_catalog.pg_class "
                            "WHERE oid = 'public.model_run'::regclass"
                        )
                    )
                ).one()
                assert receipt_security == (True, True)
                source_receipt_security = (
                    await connection.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity "
                            "FROM pg_catalog.pg_class "
                            "WHERE oid = 'public.source_command_receipt'::regclass"
                        )
                    )
                ).one()
                assert source_receipt_security == (True, True)
    finally:
        if staging_engine is not None:
            if role_granted:
                async with staging_engine.begin() as connection:
                    await connection.exec_driver_sql(f'REVOKE "{_APP_ROLE}" FROM CURRENT_USER')
            await staging_engine.dispose()
        async with admin_engine.connect() as connection:
            if runtime_role_created:
                await connection.exec_driver_sql(f'REVOKE "{_APP_ROLE}" FROM "{runtime_role}"')
                await connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{runtime_role}"')
            await connection.execute(
                text(
                    "SELECT pg_catalog.pg_terminate_backend(pid) "
                    "FROM pg_catalog.pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_catalog.pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin_engine.dispose()
