"""Idempotently provision the deterministic local principal, Vault, and runtime role."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from life_coach.modules.consent.models import (
    ConsentAction,
    ConsentPurpose,
    ConsentRecord,
)
from life_coach.modules.consent.provider_policy import ProviderPolicy
from life_coach.modules.consent.service import UserConsentCommand, grant_consent
from life_coach.platform.model_registry import load_model_registry

_ROLE_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_PASSWORD_PATTERN = re.compile(r"[A-Za-z0-9]{24,128}\Z")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value.strip()


async def seed() -> None:
    load_model_registry()
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
$local_role$
'''
    engine = create_async_engine(admin_url, hide_parameters=True, pool_pre_ping=True)
    now = datetime.now(UTC)
    fingerprint = hashlib.sha256(b"vistora-local-development-principal").hexdigest()
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(role_sql)
            await connection.exec_driver_sql(f'GRANT "life_coach_app" TO "{runtime_role}"')
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
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await session.execute(
                text("SELECT set_config('app.vault_id', :vault_id, true)"),
                {"vault_id": str(vault_id)},
            )
            grants = (
                (
                    ConsentPurpose.LONG_TERM_INFERENCE,
                    UUID("33333333-3333-4333-8333-333333333333"),
                ),
                (
                    ConsentPurpose.PASSIVE_QA,
                    UUID("44444444-4444-4444-8444-444444444444"),
                ),
            )
            for purpose, interaction_id in grants:
                existing_consent = await session.scalar(
                    select(ConsentRecord.id).where(
                        ConsentRecord.vault_id == vault_id,
                        ConsentRecord.purpose == purpose,
                        ConsentRecord.action == ConsentAction.GRANT,
                    )
                )
                if existing_consent is not None:
                    continue
                consent_now = datetime.now(UTC)
                command = UserConsentCommand(
                    vault_id=vault_id,
                    principal_id=principal_id,
                    purpose=purpose,
                    action=ConsentAction.GRANT,
                    interaction_id=interaction_id,
                    issued_at=consent_now - timedelta(seconds=1),
                    expires_at=consent_now + timedelta(minutes=4),
                    provider_policy=ProviderPolicy(
                        allowed_providers=(
                            "zero-retention-provider",
                            "stepfun-step-plan",
                        ),
                        processing_regions=("eu", "apac"),
                        zero_retention_required=False,
                        training_use_allowed=False,
                        max_retention_days=None,
                        policy_version="v1",
                    ),
                )
                await session.run_sync(
                    lambda sync, current=command: grant_consent(sync, command=current)
                )
            await session.commit()
    finally:
        await engine.dispose()


def main() -> int:
    try:
        asyncio.run(seed())
    except (RuntimeError, SQLAlchemyError, ValueError) as exc:
        # Never reflect connection strings or generated credentials into logs.
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        table_name = getattr(getattr(exc, "orig", None), "table_name", None)
        diagnostic = f" sqlstate={sqlstate}" if isinstance(sqlstate, str) else ""
        if isinstance(table_name, str) and _ROLE_PATTERN.fullmatch(table_name):
            diagnostic += f" table={table_name}"
        print(
            "Local identity seed failed; inspect PostgreSQL availability and migrations "
            f"({type(exc).__name__}{diagnostic})."
        )
        return 1
    print("Local principal, Vault membership, and non-owner runtime role are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
