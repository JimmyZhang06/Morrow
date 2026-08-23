"""Real PostgreSQL trust-boundary tests.

Set ``TEST_POSTGRES_DSN`` to an isolated PostgreSQL database whose login can
create roles. The suite creates uniquely named schemas and NOLOGIN runtime roles, executes
the same helper an Alembic migration uses, tests as the non-owner roles, and cleans up.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    Uuid,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from life_coach.platform.postgres_security import apply_postgres_security

_POSTGRES_URL = os.getenv("TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not _POSTGRES_URL,
    reason="set TEST_POSTGRES_DSN to run real PostgreSQL security tests",
)


def _metadata(schema: str) -> MetaData:
    metadata = MetaData()
    Table(
        "vault",
        metadata,
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("policy_epoch", Integer, nullable=False, default=0),
        Column("source_generation", Integer, nullable=False, default=0),
        Column("deleted_at", DateTime(timezone=True)),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Column("data_class", Text, nullable=False),
        schema=schema,
    )
    Table(
        "tenant_note",
        metadata,
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("vault_id", Uuid(as_uuid=True), nullable=False),
        Column("payload", Text, nullable=False),
        schema=schema,
    )
    Table(
        "source_document",
        metadata,
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("vault_id", Uuid(as_uuid=True), nullable=False),
        Column("deleted_at", DateTime(timezone=True)),
        Column("payload", Text, nullable=False),
        schema=schema,
    )
    Table(
        "source_revision",
        metadata,
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("vault_id", Uuid(as_uuid=True), nullable=False),
        Column("document_id", Uuid(as_uuid=True), nullable=False),
        Column("deleted_at", DateTime(timezone=True)),
        Column("content_ciphertext", LargeBinary, nullable=False),
        schema=schema,
    )
    Table(
        "source_fragment",
        metadata,
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("vault_id", Uuid(as_uuid=True), nullable=False),
        Column("revision_id", Uuid(as_uuid=True), nullable=False),
        Column("deleted_at", DateTime(timezone=True)),
        Column("text_ciphertext", LargeBinary, nullable=False),
        schema=schema,
    )
    Table(
        "search_projection",
        metadata,
        Column("id", Uuid(as_uuid=True), primary_key=True),
        Column("vault_id", Uuid(as_uuid=True), nullable=False),
        Column("source_fragment_id", Uuid(as_uuid=True), nullable=False),
        Column("deleted_at", DateTime(timezone=True)),
        Column("payload", Text),
        schema=schema,
    )
    for table_name in ("consent_record", "user_verdict"):
        columns = [
            Column("id", Uuid(as_uuid=True), primary_key=True),
            Column("vault_id", Uuid(as_uuid=True), nullable=False),
            Column("payload", Text, nullable=False),
        ]
        if table_name == "consent_record":
            columns.append(Column("policy_epoch", Integer, nullable=False))
        Table(table_name, metadata, *columns, schema=schema)
    return metadata


def _table(metadata: MetaData, schema: str, name: str) -> Table:
    return metadata.tables[f"{schema}.{name}"]


async def _set_role(connection: AsyncConnection, role: str) -> None:
    async with connection.begin():
        await connection.exec_driver_sql(f'SET ROLE "{role}"')


async def _reset_role(connection: AsyncConnection) -> None:
    async with connection.begin():
        await connection.exec_driver_sql("RESET ROLE")


async def _set_scope(connection: AsyncConnection, vault_id: uuid.UUID) -> None:
    await connection.execute(
        text("SELECT pg_catalog.set_config('app.vault_id', :vault_id, true)"),
        {"vault_id": str(vault_id)},
    )


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", getattr(error.orig, "pgcode", None))


@pytest.mark.asyncio
async def test_postgres_trust_boundary_end_to_end() -> None:
    assert _POSTGRES_URL is not None
    url = make_url(_POSTGRES_URL)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+asyncpg")
    if url.drivername != "postgresql+asyncpg":
        pytest.fail("TEST_POSTGRES_DSN must use PostgreSQL/asyncpg")

    suffix = uuid.uuid4().hex[:12]
    data_schema = f"lc_it_{suffix}"
    security_schema = f"lc_sec_{suffix}"
    business_role = f"lc_app_{suffix}"
    maintenance_role = f"lc_maint_{suffix}"
    metadata = _metadata(data_schema)
    now = datetime.now(UTC)
    vault_a, vault_b, deleted_vault = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    live_document, deleted_document = uuid.uuid4(), uuid.uuid4()
    live_revision, deleted_revision = uuid.uuid4(), uuid.uuid4()
    live_fragment, deleted_fragment = uuid.uuid4(), uuid.uuid4()
    live_projection, deleted_projection = uuid.uuid4(), uuid.uuid4()
    consent_id, verdict_id = uuid.uuid4(), uuid.uuid4()

    engine = create_async_engine(url, pool_size=1, max_overflow=0)
    roles_granted = False
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(f'CREATE SCHEMA "{data_schema}"')
            await connection.run_sync(metadata.create_all)
            await connection.execute(
                _table(metadata, data_schema, "vault").insert(),
                [
                    {
                        "id": vault_a,
                        "policy_epoch": 0,
                        "source_generation": 0,
                        "deleted_at": None,
                        "updated_at": now,
                        "data_class": "sensitive",
                    },
                    {
                        "id": vault_b,
                        "policy_epoch": 0,
                        "source_generation": 0,
                        "deleted_at": None,
                        "updated_at": now,
                        "data_class": "sensitive",
                    },
                    {
                        "id": deleted_vault,
                        "policy_epoch": 3,
                        "source_generation": 2,
                        "deleted_at": now,
                        "updated_at": now,
                        "data_class": "sensitive",
                    },
                ],
            )
            await connection.execute(
                _table(metadata, data_schema, "tenant_note").insert(),
                [
                    {"id": uuid.uuid4(), "vault_id": vault_a, "payload": "vault-a"},
                    {"id": uuid.uuid4(), "vault_id": vault_b, "payload": "vault-b"},
                ],
            )
            await connection.execute(
                _table(metadata, data_schema, "source_document").insert(),
                [
                    {
                        "id": live_document,
                        "vault_id": vault_a,
                        "deleted_at": None,
                        "payload": "live",
                    },
                    {
                        "id": deleted_document,
                        "vault_id": vault_a,
                        "deleted_at": now,
                        "payload": "deleted",
                    },
                ],
            )
            await connection.execute(
                _table(metadata, data_schema, "source_revision").insert(),
                [
                    {
                        "id": live_revision,
                        "vault_id": vault_a,
                        "document_id": live_document,
                        "deleted_at": None,
                        "content_ciphertext": b"live-secret",
                    },
                    {
                        "id": deleted_revision,
                        "vault_id": vault_a,
                        "document_id": deleted_document,
                        "deleted_at": None,
                        "content_ciphertext": b"deleted-secret",
                    },
                ],
            )
            await connection.execute(
                _table(metadata, data_schema, "source_fragment").insert(),
                [
                    {
                        "id": live_fragment,
                        "vault_id": vault_a,
                        "revision_id": live_revision,
                        "deleted_at": None,
                        "text_ciphertext": b"live-fragment",
                    },
                    {
                        "id": deleted_fragment,
                        "vault_id": vault_a,
                        "revision_id": deleted_revision,
                        "deleted_at": now,
                        "text_ciphertext": b"deleted-fragment",
                    },
                ],
            )
            await connection.execute(
                _table(metadata, data_schema, "search_projection").insert(),
                [
                    {
                        "id": live_projection,
                        "vault_id": vault_a,
                        "source_fragment_id": live_fragment,
                        "deleted_at": None,
                        "payload": "live-index",
                    },
                    {
                        "id": deleted_projection,
                        "vault_id": vault_a,
                        "source_fragment_id": deleted_fragment,
                        "deleted_at": now,
                        "payload": "deleted-index",
                    },
                ],
            )
            await connection.execute(
                _table(metadata, data_schema, "user_verdict").insert(),
                {"id": verdict_id, "vault_id": vault_a, "payload": "accepted"},
            )
            await connection.run_sync(
                lambda sync_connection: apply_postgres_security(
                    sync_connection,
                    metadata,
                    data_schema=data_schema,
                    security_schema=security_schema,
                    business_role=business_role,
                    maintenance_role=maintenance_role,
                )
            )
            forced = await connection.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_catalog.pg_class AS c "
                    "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = :schema AND c.relkind = 'r'"
                ),
                {"schema": data_schema},
            )
            assert all(enabled and force for _, enabled, force in forced)
            await connection.exec_driver_sql(f'GRANT "{business_role}" TO CURRENT_USER')
            await connection.exec_driver_sql(f'GRANT "{maintenance_role}" TO CURRENT_USER')
            roles_granted = True

        async with engine.connect() as connection:
            await _set_role(connection, business_role)

            # Missing scope and a reset transaction-local setting reveal nothing.
            async with connection.begin():
                assert (
                    await connection.scalar(
                        text(f'SELECT count(*) FROM "{data_schema}"."tenant_note"')
                    )
                    == 0
                )
            async with connection.begin():
                await _set_scope(connection, vault_a)
                assert (
                    await connection.scalar(
                        text(f'SELECT count(*) FROM "{data_schema}"."tenant_note"')
                    )
                    == 1
                )
            # Same pooled physical connection, next transaction: no scope leakage.
            async with connection.begin():
                assert (
                    await connection.scalar(
                        text(f'SELECT count(*) FROM "{data_schema}"."tenant_note"')
                    )
                    == 0
                )
            async with connection.begin():
                await _set_scope(connection, vault_b)
                assert (
                    await connection.scalar(
                        text(f'SELECT count(*) FROM "{data_schema}"."tenant_note"')
                    )
                    == 1
                )

            with pytest.raises(DBAPIError) as cross_vault:
                async with connection.begin():
                    await _set_scope(connection, vault_a)
                    await connection.execute(
                        text(
                            f'INSERT INTO "{data_schema}"."tenant_note" '
                            "(id, vault_id, payload) VALUES (:id, :vault_id, 'blocked')"
                        ),
                        {"id": uuid.uuid4(), "vault_id": vault_b},
                    )
            assert _sqlstate(cross_vault.value) == "42501"

            # Direct consent INSERT cannot choose or reuse the policy epoch.
            async with connection.begin():
                await _set_scope(connection, vault_a)
                epoch = await connection.scalar(
                    text(
                        f'INSERT INTO "{data_schema}"."consent_record" '
                        "(id, vault_id, payload, policy_epoch) "
                        "VALUES (:id, :vault_id, 'grant', 9999) RETURNING policy_epoch"
                    ),
                    {"id": consent_id, "vault_id": vault_a},
                )
                assert epoch == 1
                assert (
                    await connection.scalar(
                        text(
                            f'SELECT policy_epoch FROM "{data_schema}"."vault" WHERE id = :id'
                        ),
                        {"id": vault_a},
                    )
                    == 1
                )
            async with connection.begin():
                await _set_scope(connection, vault_a)
                epoch = await connection.scalar(
                    text(
                        f'INSERT INTO "{data_schema}"."consent_record" '
                        "(id, vault_id, payload, policy_epoch) "
                        "VALUES (:id, :vault_id, 'revoke', -4) RETURNING policy_epoch"
                    ),
                    {"id": uuid.uuid4(), "vault_id": vault_a},
                )
                assert epoch == 2

            # Fence columns are not directly writable by the ordinary runtime role.
            with pytest.raises(DBAPIError) as fence_write:
                async with connection.begin():
                    await _set_scope(connection, vault_a)
                    await connection.execute(
                        text(
                            f'UPDATE "{data_schema}"."vault" '
                            "SET policy_epoch = 500 WHERE id = :id"
                        ),
                        {"id": vault_a},
                    )
            assert _sqlstate(fence_write.value) == "42501"

            # Deleted ancestry (including revision ciphertext) is invisible to app reads.
            async with connection.begin():
                await _set_scope(connection, vault_a)
                for table_name in (
                    "source_document",
                    "source_revision",
                    "source_fragment",
                    "search_projection",
                ):
                    assert (
                        await connection.scalar(
                            text(f'SELECT count(*) FROM "{data_schema}"."{table_name}"')
                        )
                        == 1
                    )

            with pytest.raises(DBAPIError) as bad_fragment:
                async with connection.begin():
                    await _set_scope(connection, vault_a)
                    await connection.execute(
                        text(
                            f'INSERT INTO "{data_schema}"."source_fragment" '
                            "(id, vault_id, revision_id, deleted_at, text_ciphertext) "
                            "VALUES (:id, :vault_id, :revision_id, NULL, :ciphertext)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "vault_id": vault_a,
                            "revision_id": deleted_revision,
                            "ciphertext": b"blocked",
                        },
                    )
            assert _sqlstate(bad_fragment.value) == "55000"

            with pytest.raises(DBAPIError) as bad_projection:
                async with connection.begin():
                    await _set_scope(connection, vault_a)
                    await connection.execute(
                        text(
                            f'INSERT INTO "{data_schema}"."search_projection" '
                            "(id, vault_id, source_fragment_id, deleted_at, payload) "
                            "VALUES (:id, :vault_id, :fragment_id, NULL, 'blocked')"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "vault_id": vault_a,
                            "fragment_id": deleted_fragment,
                        },
                    )
            assert _sqlstate(bad_projection.value) == "55000"

            # Prove triggers still fail closed if a future grant accidentally broadens DML.
            await _reset_role(connection)
            async with connection.begin():
                for table_name in ("consent_record", "source_revision", "user_verdict"):
                    await connection.exec_driver_sql(
                        f'GRANT UPDATE, DELETE ON "{data_schema}"."{table_name}" '
                        f'TO "{business_role}"'
                    )
            await _set_role(connection, business_role)
            for table_name, row_id in (
                ("consent_record", consent_id),
                ("source_revision", live_revision),
                ("user_verdict", verdict_id),
            ):
                with pytest.raises(DBAPIError) as immutable:
                    async with connection.begin():
                        await _set_scope(connection, vault_a)
                        await connection.execute(
                            text(
                                f'UPDATE "{data_schema}"."{table_name}" '
                                "SET payload = payload WHERE id = :id"
                                if table_name != "source_revision"
                                else f'UPDATE "{data_schema}"."{table_name}" '
                                "SET content_ciphertext = content_ciphertext WHERE id = :id"
                            ),
                            {"id": row_id},
                        )
                assert _sqlstate(immutable.value) == "55000"

            # Maintenance policy can see tombstones, but monotonic triggers forbid recovery.
            await _reset_role(connection)
            async with connection.begin():
                await connection.exec_driver_sql(
                    f'GRANT SELECT, UPDATE ON "{data_schema}"."vault" '
                    f'TO "{maintenance_role}"'
                )
            await _set_role(connection, maintenance_role)
            with pytest.raises(DBAPIError) as source_recovery:
                async with connection.begin():
                    await _set_scope(connection, vault_a)
                    await connection.execute(
                        text(
                            f'UPDATE "{data_schema}"."source_document" '
                            "SET deleted_at = NULL WHERE id = :id"
                        ),
                        {"id": deleted_document},
                    )
            assert _sqlstate(source_recovery.value) == "55000"

            with pytest.raises(DBAPIError) as vault_recovery:
                async with connection.begin():
                    await _set_scope(connection, deleted_vault)
                    await connection.execute(
                        text(
                            f'UPDATE "{data_schema}"."vault" '
                            "SET deleted_at = NULL WHERE id = :id"
                        ),
                        {"id": deleted_vault},
                    )
            assert _sqlstate(vault_recovery.value) == "55000"

            with pytest.raises(DBAPIError) as fence_decrease:
                async with connection.begin():
                    await _set_scope(connection, vault_a)
                    await connection.execute(
                        text(
                            f'UPDATE "{data_schema}"."vault" '
                            "SET policy_epoch = 1 WHERE id = :id"
                        ),
                        {"id": vault_a},
                    )
            assert _sqlstate(fence_decrease.value) == "55000"

            await _reset_role(connection)
    finally:
        async with engine.connect() as cleanup:
            try:
                await cleanup.rollback()
                async with cleanup.begin():
                    await cleanup.exec_driver_sql("RESET ROLE")
                    await cleanup.exec_driver_sql(
                        f'DROP SCHEMA IF EXISTS "{security_schema}" CASCADE'
                    )
                    await cleanup.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{data_schema}" CASCADE')
                    if roles_granted:
                        await cleanup.exec_driver_sql(
                            f'REVOKE "{business_role}" FROM CURRENT_USER'
                        )
                        await cleanup.exec_driver_sql(
                            f'REVOKE "{maintenance_role}" FROM CURRENT_USER'
                        )
                    await cleanup.exec_driver_sql(f'DROP ROLE IF EXISTS "{business_role}"')
                    await cleanup.exec_driver_sql(f'DROP ROLE IF EXISTS "{maintenance_role}"')
            finally:
                await cleanup.rollback()
        await engine.dispose()
