"""Static contract tests for PostgreSQL trust-boundary DDL generation."""

from __future__ import annotations

import pytest

from life_coach.platform.model_registry import load_model_registry
from life_coach.platform.postgres_security import (
    PostgresSecurityConfigurationError,
    TenantTable,
    build_business_role_bootstrap_statements,
    build_integrity_trigger_statements,
    build_maintenance_role_bootstrap_statements,
    build_rls_statements,
    build_table_privilege_statements,
    discover_tenant_tables,
)
from life_coach.shared.database import Base


def governed_tables() -> tuple[TenantTable, ...]:
    load_model_registry()
    return discover_tenant_tables(Base.metadata)


def test_every_vault_bearing_table_receives_enable_and_force_rls() -> None:
    tables = governed_tables()
    protected_names = {table.name for table in tables}
    expected_names = {
        table.name
        for table in Base.metadata.tables.values()
        if table.name == "vault" or "vault_id" in table.c
    }

    assert protected_names == expected_names
    statements = build_rls_statements(tables)
    rendered = "\n".join(statements)
    for table in tables:
        qualified_name = f'"public"."{table.name}"'
        assert f"ALTER TABLE {qualified_name} ENABLE ROW LEVEL SECURITY" in statements
        assert f"ALTER TABLE {qualified_name} FORCE ROW LEVEL SECURITY" in statements
        assert f'CREATE POLICY "lc_business_select" ON {qualified_name}' in rendered
        assert f'CREATE POLICY "lc_vault_scope_guard" ON {qualified_name}' in rendered


def test_rls_scope_is_transaction_setting_and_missing_scope_is_null() -> None:
    statements = build_rls_statements(governed_tables())
    policies = "\n".join(statement for statement in statements if statement.startswith("CREATE"))

    assert "NULLIF(pg_catalog.current_setting('app.vault_id', true), '')::uuid" in policies
    assert '"id" = NULLIF' in policies  # vault itself
    assert '"vault_id" = NULLIF' in policies  # every other tenant table
    assert "AS RESTRICTIVE FOR ALL TO PUBLIC" in policies


def test_business_source_select_policy_requires_live_ancestry() -> None:
    policies = "\n".join(build_rls_statements(governed_tables()))

    for table_name in (
        "source_document",
        "source_revision",
        "source_fragment",
        "search_projection",
    ):
        assert f'CREATE POLICY "lc_business_select" ON "public"."{table_name}"' in policies
    assert "document.deleted_at IS NULL" in policies
    assert "revision.deleted_at IS NULL" in policies
    assert "fragment.deleted_at IS NULL" in policies
    assert "owner_vault.deleted_at IS NULL" in policies
    assert 'CREATE POLICY "lc_maintenance_scope"' in policies


def test_append_only_and_source_lifecycle_triggers_cover_required_tables() -> None:
    statements = build_integrity_trigger_statements(governed_tables())
    rendered = "\n".join(statements)

    for table_name in (
        "consent_record",
        "model_run_input",
        "source_revision",
        "user_verdict",
    ):
        assert (
            f'CREATE TRIGGER "lc_append_only" BEFORE UPDATE OR DELETE ON "public"."{table_name}"'
        ) in rendered
    for table_name in ("source_document", "source_fragment", "search_projection"):
        assert (
            'CREATE TRIGGER "lc_tombstone_monotonic" BEFORE UPDATE OF deleted_at '
            f'ON "public"."{table_name}"'
        ) in rendered
    assert 'CREATE TRIGGER "lc_vault_fences"' in rendered
    assert 'CREATE TRIGGER "lc_consent_epoch"' in rendered
    assert "NEW.policy_epoch :=" in rendered
    assert "vault fences are monotonic" in rendered
    assert "a vault tombstone cannot be removed" in rendered
    assert "a fragment requires live Source ancestry" in rendered
    assert "a search projection requires live Source ancestry" in rendered
    assert "document.deleted_at IS NULL" in rendered
    assert "owner_vault.deleted_at IS NULL" in rendered


def test_business_role_requires_a_live_vault_for_every_tenant_table() -> None:
    tables = governed_tables()
    statements = build_rls_statements(tables)
    select_policies = {
        table.name: next(
            statement
            for statement in statements
            if statement.startswith(
                f'CREATE POLICY "lc_business_select" ON "public"."{table.name}"'
            )
        )
        for table in tables
    }

    for table_name, policy in select_policies.items():
        assert "owner_vault.deleted_at IS NULL" in policy or table_name == "vault"


def test_business_role_is_non_login_non_owner_and_has_no_append_only_mutation() -> None:
    bootstrap = "\n".join(build_business_role_bootstrap_statements())
    maintenance = "\n".join(build_maintenance_role_bootstrap_statements())
    privileges = "\n".join(build_table_privilege_statements(governed_tables()))
    triggers = "\n".join(build_integrity_trigger_statements(governed_tables()))

    assert "NOLOGIN" in bootstrap
    assert "NOSUPERUSER" in bootstrap
    assert "NOBYPASSRLS" in bootstrap
    assert "NOCREATEROLE" in bootstrap
    assert "NOBYPASSRLS" in maintenance
    assert "application role must not own tenant tables" in privileges
    assert 'GRANT EXECUTE ON FUNCTION "life_coach_private"."advance_policy_epoch"' in triggers
    for table_name in (
        "consent_record",
        "model_run_input",
        "source_revision",
        "user_verdict",
    ):
        grant = f'GRANT SELECT, INSERT ON TABLE "public"."{table_name}"'
        assert grant in privileges
    assert 'GRANT SELECT, INSERT, UPDATE ON TABLE "public"."model_run"' in privileges
    assert 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "public"."model_run"' not in privileges
    assert 'GRANT SELECT, INSERT ON TABLE "public"."vault"' in privileges
    assert 'GRANT UPDATE ("policy_epoch"' not in privileges


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("data_schema", "public; DROP SCHEMA public"),
        ("business_role", 'app" SUPERUSER'),
    ],
)
def test_untrusted_sql_identifiers_are_rejected(keyword: str, value: str) -> None:
    with pytest.raises(PostgresSecurityConfigurationError):
        if keyword == "data_schema":
            discover_tenant_tables(Base.metadata, data_schema=value)
        else:
            build_business_role_bootstrap_statements(business_role=value)
