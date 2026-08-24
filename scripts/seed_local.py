"""Idempotently provision the deterministic local principal, Vault, and runtime role."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

_ROLE_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_PASSWORD_PATTERN = re.compile(r"[A-Za-z0-9]{24,128}\Z")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value.strip()


async def seed() -> None:
    admin_url = _required("LOCAL_ADMIN_DATABASE_URL")
    runtime_role = _required("LOCAL_RUNTIME_DATABASE_ROLE")
    runtime_password = _required("LOCAL_RUNTIME_DATABASE_PASSWORD")
    principal_id = UUID(_required("APP_LOCAL_AUTH_PRINCIPAL_ID"))
    vault_id = UUID(_required("LOCAL_VAULT_ID"))
    if _ROLE_PATTERN.fullmatch(runtime_role) is None:
        raise RuntimeError("LOCAL_RUNTIME_DATABASE_ROLE is invalid")
    if _PASSWORD_PATTERN.fullmatch(runtime_password) is None:
        raise RuntimeError("LOCAL_RUNTIME_DATABASE_PASSWORD is invalid")

    # Both interpolated values are constrained above to conservative alphabets.
    # PostgreSQL utility statements do not accept ordinary bind parameters here.
    role_sql = f'''\
DO $local_role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{runtime_role}') THEN
        CREATE ROLE "{runtime_role}" LOGIN PASSWORD '{runtime_password}'
            NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
    ELSE
        ALTER ROLE "{runtime_role}" LOGIN PASSWORD '{runtime_password}'
            NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
    END IF;
END
$local_role$;
GRANT "life_coach_app" TO "{runtime_role}";
'''
    engine = create_async_engine(admin_url, hide_parameters=True, pool_pre_ping=True)
    now = datetime.now(UTC)
    fingerprint = hashlib.sha256(b"vistora-local-development-principal").hexdigest()
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(role_sql)
            await connection.execute(
                text(
                    "INSERT INTO principal "
                    "(id, issuer, subject_fingerprint, disabled_at, created_at, updated_at) "
                    "VALUES (:id, :issuer, :fingerprint, NULL, :now, :now) "
                    "ON CONFLICT (id) DO UPDATE SET disabled_at = NULL, updated_at = :now"
                ),
                {
                    "id": principal_id,
                    "issuer": "vistora-local-development",
                    "fingerprint": fingerprint,
                    "now": now,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO vault "
                    "(id, policy_epoch, source_generation, deleted_at, created_by, data_class, "
                    "created_at, updated_at) "
                    "VALUES (:id, 0, 0, NULL, 'user', 'sensitive', :now, :now) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": vault_id, "now": now},
            )
            await connection.execute(
                text(
                    "INSERT INTO vault_membership "
                    "(id, vault_id, principal_id, role, generation, revoked_at, "
                    "created_at, updated_at) "
                    "VALUES (:id, :vault_id, :principal_id, 'owner', 1, NULL, :now, :now) "
                    "ON CONFLICT (vault_id, principal_id) DO UPDATE SET "
                    "role = 'owner', revoked_at = NULL, updated_at = :now"
                ),
                {
                    "id": uuid4(),
                    "vault_id": vault_id,
                    "principal_id": principal_id,
                    "now": now,
                },
            )
    finally:
        await engine.dispose()


def main() -> int:
    try:
        asyncio.run(seed())
    except (RuntimeError, SQLAlchemyError, ValueError):
        # Never reflect connection strings or generated credentials into logs.
        print("Local identity seed failed; inspect PostgreSQL availability and migrations.")
        return 1
    print("Local principal, Vault membership, and non-owner runtime role are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
