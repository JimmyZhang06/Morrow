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
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

_ADMIN_DSN = os.getenv("TEST_POSTGRES_DSN")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_ROLE = "life_coach_app"

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


@pytest.mark.asyncio
async def test_alembic_rehearsal_and_migrated_non_owner_membership_boundary() -> None:
    assert _ADMIN_DSN is not None
    admin_url = _async_url(_ADMIN_DSN)
    database_name = f"lc_stage_{uuid.uuid4().hex[:12]}"
    staging_url = admin_url.set(database=database_name)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    staging_engine = None
    role_granted = False

    try:
        async with admin_engine.connect() as connection:
            await connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')

        _run_alembic(staging_url, "upgrade", "head")
        _run_alembic(staging_url, "downgrade", "base")
        _run_alembic(staging_url, "upgrade", "head")

        staging_engine = create_async_engine(staging_url, pool_size=1, max_overflow=0)
        vault_id, principal_id = uuid.uuid4(), uuid.uuid4()
        now = datetime.now(UTC)
        async with staging_engine.begin() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "8b1f0d7e4a21"
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

        async with staging_engine.connect() as connection:
            async with connection.begin():
                await connection.exec_driver_sql(f'SET ROLE "{_APP_ROLE}"')
            async with connection.begin():
                assert await connection.scalar(text("SELECT count(*) FROM vault_membership")) == 0
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
            async with connection.begin():
                await connection.exec_driver_sql("RESET ROLE")
                owner = await connection.scalar(
                    text(
                        "SELECT tableowner FROM pg_catalog.pg_tables "
                        "WHERE schemaname = 'public' AND tablename = 'vault_membership'"
                    )
                )
                assert owner != _APP_ROLE
    finally:
        if staging_engine is not None:
            if role_granted:
                async with staging_engine.begin() as connection:
                    await connection.exec_driver_sql(f'REVOKE "{_APP_ROLE}" FROM CURRENT_USER')
            await staging_engine.dispose()
        async with admin_engine.connect() as connection:
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
