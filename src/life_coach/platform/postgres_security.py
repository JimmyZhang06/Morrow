"""PostgreSQL trust-boundary DDL for tenant data and privacy fences.

The application session guard is useful defence in depth, but PostgreSQL is the final
tenant boundary. This module stays independent of feature model imports and generated
Alembic revisions: after creating tables, a migration passes its connection and shared
metadata to :func:`apply_postgres_security`.

Policies read a transaction-local ``app.vault_id``. A missing/reset value becomes NULL
and matches no row; an invalid non-empty UUID raises. Both behaviours fail closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import Connection, MetaData, text

DEFAULT_BUSINESS_ROLE = "life_coach_app"
DEFAULT_MAINTENANCE_ROLE = "life_coach_maintenance"
DEFAULT_DATA_SCHEMA = "public"
DEFAULT_SECURITY_SCHEMA = "life_coach_private"

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_IMMUTABLE_TABLES = frozenset(
    {
        "consent_record",
        "action_command_receipt",
        "action_verdict",
        "model_run_artifact",
        "model_run_input",
        "source_command_receipt",
        "source_revision",
        "user_verdict",
    }
)
_AUTHORIZATION_TABLES = frozenset({"vault_membership"})
_RECEIPT_STATE_TABLES = frozenset({"model_run"})
_SOURCE_ANCESTRY_TABLES = frozenset(
    {"vault", "source_document", "source_revision", "source_fragment"}
)
_POLICIES = (
    "lc_business_select",
    "lc_business_insert",
    "lc_business_update",
    "lc_business_delete",
    "lc_maintenance_scope",
    "lc_owner_scope",
    "lc_vault_scope_guard",
)


class PostgresSecurityConfigurationError(ValueError):
    """Metadata or deployment identifiers cannot uphold the security contract."""


@dataclass(frozen=True, slots=True)
class TenantTable:
    """One table protected by a vault discriminator."""

    name: str
    scope_column: str
    columns: frozenset[str]


def _identifier(value: str, *, field: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise PostgresSecurityConfigurationError(f"{field} must be a simple SQL identifier")
    return value


def _quoted(value: str) -> str:
    """Quote an already validated PostgreSQL identifier."""

    return f'"{value}"'


def _qualified(schema: str, name: str) -> str:
    return f"{_quoted(schema)}.{_quoted(name)}"


def discover_tenant_tables(
    metadata: MetaData,
    *,
    data_schema: str = DEFAULT_DATA_SCHEMA,
) -> tuple[TenantTable, ...]:
    """Find every table whose rows belong to a vault.

    ``vault`` is scoped by ``id``. Every other governed table must carry ``vault_id``.
    Tables explicitly assigned to another schema belong to that schema's migration.
    """

    data_schema = _identifier(data_schema, field="data_schema")
    discovered: list[TenantTable] = []
    for table in metadata.sorted_tables:
        table_schema = table.schema or data_schema
        if table_schema != data_schema:
            continue
        name = _identifier(table.name, field="table name")
        columns = frozenset(_identifier(column.name, field="column name") for column in table.c)
        if name == "vault":
            if "id" not in columns:
                raise PostgresSecurityConfigurationError("vault table must contain id")
            discovered.append(TenantTable(name=name, scope_column="id", columns=columns))
        elif "vault_id" in columns:
            discovered.append(TenantTable(name=name, scope_column="vault_id", columns=columns))

    by_name = {table.name: table for table in discovered}
    vault = by_name.get("vault")
    if vault is None:
        raise PostgresSecurityConfigurationError("metadata must contain the vault table")
    required_vault_columns = {"id", "policy_epoch", "source_generation", "deleted_at"}
    missing_vault = required_vault_columns.difference(vault.columns)
    if missing_vault:
        missing = ", ".join(sorted(missing_vault))
        raise PostgresSecurityConfigurationError(f"vault security columns are missing: {missing}")
    if "consent_record" in by_name and "policy_epoch" not in by_name["consent_record"].columns:
        raise PostgresSecurityConfigurationError("consent_record must contain policy_epoch")
    return tuple(sorted(discovered, key=lambda table: table.name))


def _role_bootstrap_statement(role_name: str) -> str:
    role_name = _identifier(role_name, field="role")
    quoted_role = _quoted(role_name)
    attributes = "NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
    return f"""
DO $life_coach_role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{role_name}') THEN
        EXECUTE 'CREATE ROLE {quoted_role} {attributes}';
    ELSE
        EXECUTE 'ALTER ROLE {quoted_role} {attributes}';
    END IF;
END
$life_coach_role$
""".strip()


def build_business_role_bootstrap_statements(
    *,
    business_role: str = DEFAULT_BUSINESS_ROLE,
) -> tuple[str, ...]:
    """Create/repair the non-login, non-owner application group role."""

    return (_role_bootstrap_statement(business_role),)


def build_maintenance_role_bootstrap_statements(
    *,
    maintenance_role: str = DEFAULT_MAINTENANCE_ROLE,
) -> tuple[str, ...]:
    """Create/repair a separate scoped role for deletion and erasure workers."""

    return (_role_bootstrap_statement(maintenance_role),)


def _scope_expression(table: TenantTable) -> str:
    table_name = _quoted(_identifier(table.name, field="table name"))
    column = _quoted(_identifier(table.scope_column, field="scope column"))
    setting = "NULLIF(pg_catalog.current_setting('app.vault_id', true), '')::uuid"
    return f"{table_name}.{column} = {setting}"


def _business_live_expression(table: TenantTable, *, data_schema: str) -> str:
    """Hide tombstoned Source ancestry from the ordinary application role."""

    scope = _scope_expression(table)
    name = table.name
    outer = _quoted(name)
    vault = _qualified(data_schema, "vault")
    if name == "vault":
        return f'({scope}) AND {outer}."deleted_at" IS NULL'
    if name == "source_document":
        return (
            f'({scope}) AND {outer}."deleted_at" IS NULL AND EXISTS ('
            f"SELECT 1 FROM {vault} AS owner_vault "
            f'WHERE owner_vault.id = {outer}."vault_id" '
            "AND owner_vault.deleted_at IS NULL)"
        )
    if name == "source_revision":
        document = _qualified(data_schema, "source_document")
        return (
            f'({scope}) AND {outer}."deleted_at" IS NULL AND EXISTS ('
            f"SELECT 1 FROM {document} AS document JOIN {vault} AS owner_vault "
            "ON owner_vault.id = document.vault_id "
            f'WHERE document.vault_id = {outer}."vault_id" '
            f'AND document.id = {outer}."document_id" '
            "AND document.deleted_at IS NULL AND owner_vault.deleted_at IS NULL)"
        )
    if name == "source_fragment":
        revision = _qualified(data_schema, "source_revision")
        document = _qualified(data_schema, "source_document")
        return (
            f'({scope}) AND {outer}."deleted_at" IS NULL AND EXISTS ('
            f"SELECT 1 FROM {revision} AS revision JOIN {document} AS document "
            "ON document.vault_id = revision.vault_id "
            "AND document.id = revision.document_id "
            f"JOIN {vault} AS owner_vault ON owner_vault.id = revision.vault_id "
            f'WHERE revision.vault_id = {outer}."vault_id" '
            f'AND revision.id = {outer}."revision_id" '
            "AND revision.deleted_at IS NULL AND document.deleted_at IS NULL "
            "AND owner_vault.deleted_at IS NULL)"
        )
    if name == "search_projection":
        fragment = _qualified(data_schema, "source_fragment")
        revision = _qualified(data_schema, "source_revision")
        document = _qualified(data_schema, "source_document")
        return (
            f'({scope}) AND {outer}."deleted_at" IS NULL AND EXISTS ('
            f"SELECT 1 FROM {fragment} AS fragment JOIN {revision} AS revision "
            "ON revision.vault_id = fragment.vault_id "
            "AND revision.id = fragment.revision_id "
            f"JOIN {document} AS document ON document.vault_id = revision.vault_id "
            "AND document.id = revision.document_id "
            f"JOIN {vault} AS owner_vault ON owner_vault.id = fragment.vault_id "
            f'WHERE fragment.vault_id = {outer}."vault_id" '
            f'AND fragment.id = {outer}."source_fragment_id" '
            "AND fragment.deleted_at IS NULL AND revision.deleted_at IS NULL "
            "AND document.deleted_at IS NULL AND owner_vault.deleted_at IS NULL)"
        )
    return (
        f"({scope}) AND EXISTS ("
        f"SELECT 1 FROM {vault} AS owner_vault "
        f'WHERE owner_vault.id = {outer}."vault_id" '
        "AND owner_vault.deleted_at IS NULL)"
    )


def build_rls_statements(
    tenant_tables: tuple[TenantTable, ...],
    *,
    data_schema: str = DEFAULT_DATA_SCHEMA,
    business_role: str = DEFAULT_BUSINESS_ROLE,
    maintenance_role: str = DEFAULT_MAINTENANCE_ROLE,
) -> tuple[str, ...]:
    """Force RLS and install command-specific ordinary/maintenance policies."""

    data_schema = _identifier(data_schema, field="data_schema")
    business_role = _identifier(business_role, field="business_role")
    maintenance_role = _identifier(maintenance_role, field="maintenance_role")
    app_role = _quoted(business_role)
    erase_role = _quoted(maintenance_role)
    statements: list[str] = []
    for table in tenant_tables:
        qualified_table = _qualified(data_schema, table.name)
        scope = _scope_expression(table)
        live = _business_live_expression(table, data_schema=data_schema)
        statements.extend(
            (
                f"ALTER TABLE {qualified_table} ENABLE ROW LEVEL SECURITY",
                f"ALTER TABLE {qualified_table} FORCE ROW LEVEL SECURITY",
            )
        )
        statements.extend(
            f"DROP POLICY IF EXISTS {_quoted(policy)} ON {qualified_table}" for policy in _POLICIES
        )
        statements.extend(
            (
                f'CREATE POLICY "lc_business_select" ON {qualified_table} '
                f"AS PERMISSIVE FOR SELECT TO {app_role} USING ({live})",
                f'CREATE POLICY "lc_business_insert" ON {qualified_table} '
                f"AS PERMISSIVE FOR INSERT TO {app_role} WITH CHECK ({live})",
                f'CREATE POLICY "lc_business_update" ON {qualified_table} '
                f"AS PERMISSIVE FOR UPDATE TO {app_role} USING ({live}) "
                f"WITH CHECK ({scope})",
                f'CREATE POLICY "lc_business_delete" ON {qualified_table} '
                f"AS PERMISSIVE FOR DELETE TO {app_role} USING ({live})",
                f'CREATE POLICY "lc_maintenance_scope" ON {qualified_table} '
                f"AS PERMISSIVE FOR ALL TO {erase_role} USING ({scope}) WITH CHECK ({scope})",
                f'CREATE POLICY "lc_owner_scope" ON {qualified_table} '
                f"AS PERMISSIVE FOR ALL TO CURRENT_USER USING ({scope}) WITH CHECK ({scope})",
                f'CREATE POLICY "lc_vault_scope_guard" ON {qualified_table} '
                f"AS RESTRICTIVE FOR ALL TO PUBLIC USING ({scope}) WITH CHECK ({scope})",
            )
        )
    return tuple(statements)


def _role_assertion_statement(
    role_name: str,
    *,
    tenant_tables: tuple[TenantTable, ...],
    data_schema: str,
    security_schema: str,
) -> str:
    role_name = _identifier(role_name, field="role")
    table_names = ", ".join(f"'{table.name}'" for table in tenant_tables)
    return f"""
DO $life_coach_role_assertion$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{role_name}') THEN
        RAISE EXCEPTION 'required application role is missing' USING ERRCODE = '42501';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = '{role_name}'
          AND (
              rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb
              OR rolcanlogin OR rolinherit
          )
    ) THEN
        RAISE EXCEPTION 'application role has forbidden attributes' USING ERRCODE = '42501';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_catalog.pg_roles AS r ON r.oid = c.relowner
        WHERE n.nspname = '{data_schema}' AND c.relname IN ({table_names})
          AND r.rolname = '{role_name}'
    ) THEN
        RAISE EXCEPTION 'application role must not own tenant tables' USING ERRCODE = '42501';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS n
        JOIN pg_catalog.pg_roles AS r ON r.oid = n.nspowner
        WHERE n.nspname IN ('{data_schema}', '{security_schema}')
          AND r.rolname = '{role_name}'
    ) THEN
        RAISE EXCEPTION 'application role must not own application schemas'
            USING ERRCODE = '42501';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
        WHERE member.rolname = '{role_name}'
    ) THEN
        RAISE EXCEPTION 'application role must not inherit another role'
            USING ERRCODE = '42501';
    END IF;
END
$life_coach_role_assertion$
""".strip()


def build_table_privilege_statements(
    tenant_tables: tuple[TenantTable, ...],
    *,
    data_schema: str = DEFAULT_DATA_SCHEMA,
    security_schema: str = DEFAULT_SECURITY_SCHEMA,
    business_role: str = DEFAULT_BUSINESS_ROLE,
    maintenance_role: str = DEFAULT_MAINTENANCE_ROLE,
) -> tuple[str, ...]:
    """Grant least privilege and assert neither runtime role can bypass forced RLS."""

    data_schema = _identifier(data_schema, field="data_schema")
    security_schema = _identifier(security_schema, field="security_schema")
    business_role = _identifier(business_role, field="business_role")
    maintenance_role = _identifier(maintenance_role, field="maintenance_role")
    app_role = _quoted(business_role)
    erase_role = _quoted(maintenance_role)
    statements: list[str] = [
        f"REVOKE CREATE ON SCHEMA {_quoted(data_schema)} FROM PUBLIC",
        f"GRANT USAGE ON SCHEMA {_quoted(data_schema)} TO {app_role}",
        f"GRANT USAGE ON SCHEMA {_quoted(data_schema)} TO {erase_role}",
        f"REVOKE ALL ON SCHEMA {_quoted(security_schema)} FROM PUBLIC",
        f"GRANT USAGE ON SCHEMA {_quoted(security_schema)} TO {app_role}",
    ]
    for table in tenant_tables:
        qualified_table = _qualified(data_schema, table.name)
        statements.extend(
            (
                f"REVOKE ALL ON TABLE {qualified_table} FROM PUBLIC",
                f"REVOKE ALL ON TABLE {qualified_table} FROM {app_role}",
                f"REVOKE ALL ON TABLE {qualified_table} FROM {erase_role}",
            )
        )
        if table.name in _AUTHORIZATION_TABLES:
            # Membership provisioning is an administrative capability. The
            # business role may authorize a request but can never self-grant,
            # mutate, or revoke membership.
            app_privileges = "SELECT"
        elif table.name in _IMMUTABLE_TABLES or table.name == "vault":
            app_privileges = "SELECT, INSERT"
        elif table.name in _RECEIPT_STATE_TABLES:
            # Model receipts are state machines: the runtime may advance a row,
            # but ordinary business paths must never erase the audit trail.
            app_privileges = "SELECT, INSERT, UPDATE"
        elif table.name == "source_document":
            app_privileges = "SELECT, INSERT, UPDATE"
        else:
            app_privileges = "SELECT, INSERT, UPDATE, DELETE"
        statements.append(f"GRANT {app_privileges} ON TABLE {qualified_table} TO {app_role}")

        if table.name == "vault":
            mutable = tuple(
                column
                for column in ("deleted_at", "updated_at", "data_class")
                if column in table.columns
            )
            if mutable:
                columns = ", ".join(_quoted(column) for column in mutable)
                statements.append(
                    f"GRANT UPDATE ({columns}) ON TABLE {qualified_table} TO {app_role}"
                )
        if table.name in {"source_document", "source_fragment", "search_projection"}:
            statements.append(
                f"GRANT SELECT, UPDATE, DELETE ON TABLE {qualified_table} TO {erase_role}"
            )
        elif table.name == "source_revision":
            statements.append(f"GRANT SELECT ON TABLE {qualified_table} TO {erase_role}")

    statements.extend(
        (
            _role_assertion_statement(
                business_role,
                tenant_tables=tenant_tables,
                data_schema=data_schema,
                security_schema=security_schema,
            ),
            _role_assertion_statement(
                maintenance_role,
                tenant_tables=tenant_tables,
                data_schema=data_schema,
                security_schema=security_schema,
            ),
        )
    )
    return tuple(statements)


def _fence_function_statement(
    *,
    counter: str,
    data_schema: str,
    security_schema: str,
) -> str:
    counter = _identifier(counter, field="counter")
    function = _qualified(security_schema, f"advance_{counter}")
    vault = _qualified(data_schema, "vault")
    return f"""
CREATE OR REPLACE FUNCTION {function}(p_vault_id uuid)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $life_coach_function$
DECLARE
    next_value integer;
BEGIN
    IF p_vault_id IS DISTINCT FROM
       NULLIF(pg_catalog.current_setting('app.vault_id', true), '')::uuid THEN
        RAISE EXCEPTION 'vault scope does not authorize fence advance' USING ERRCODE = '42501';
    END IF;
    UPDATE {vault}
       SET {_quoted(counter)} = {_quoted(counter)} + 1
     WHERE id = p_vault_id AND deleted_at IS NULL
     RETURNING {_quoted(counter)} INTO next_value;
    IF next_value IS NULL THEN
        RAISE EXCEPTION 'vault is unavailable' USING ERRCODE = '55000';
    END IF;
    RETURN next_value;
END
$life_coach_function$
""".strip()


def build_integrity_trigger_statements(
    tenant_tables: tuple[TenantTable, ...],
    *,
    data_schema: str = DEFAULT_DATA_SCHEMA,
    security_schema: str = DEFAULT_SECURITY_SCHEMA,
    business_role: str = DEFAULT_BUSINESS_ROLE,
) -> tuple[str, ...]:
    """Build database triggers for fences, append-only streams and Source lifecycle."""

    data_schema = _identifier(data_schema, field="data_schema")
    security_schema = _identifier(security_schema, field="security_schema")
    business_role = _identifier(business_role, field="business_role")
    app_role = _quoted(business_role)
    table_names = {table.name for table in tenant_tables}
    security = _quoted(security_schema)
    vault = _qualified(data_schema, "vault")
    statements: list[str] = [
        f"CREATE SCHEMA IF NOT EXISTS {security}",
        f"REVOKE ALL ON SCHEMA {security} FROM PUBLIC",
    ]

    fence_guard = _qualified(security_schema, "enforce_vault_fences")
    statements.extend(
        (
            f"""
CREATE OR REPLACE FUNCTION {fence_guard}()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $life_coach_function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.policy_epoch := 0;
        NEW.source_generation := 0;
        IF NEW.deleted_at IS NOT NULL THEN
            RAISE EXCEPTION 'a vault must be created live' USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.policy_epoch < OLD.policy_epoch OR NEW.source_generation < OLD.source_generation THEN
        RAISE EXCEPTION 'vault fences are monotonic' USING ERRCODE = '55000';
    END IF;
    IF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL THEN
        RAISE EXCEPTION 'a vault tombstone cannot be removed' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$life_coach_function$
""".strip(),
            f'DROP TRIGGER IF EXISTS "lc_vault_fences" ON {vault}',
            f'CREATE TRIGGER "lc_vault_fences" BEFORE INSERT OR UPDATE ON {vault} '
            f"FOR EACH ROW EXECUTE FUNCTION {fence_guard}()",
            f"REVOKE ALL ON FUNCTION {fence_guard}() FROM PUBLIC",
        )
    )

    for counter in ("policy_epoch", "source_generation"):
        function = _qualified(security_schema, f"advance_{counter}")
        statements.extend(
            (
                _fence_function_statement(
                    counter=counter,
                    data_schema=data_schema,
                    security_schema=security_schema,
                ),
                f"REVOKE ALL ON FUNCTION {function}(uuid) FROM PUBLIC",
                f"GRANT EXECUTE ON FUNCTION {function}(uuid) TO {app_role}",
            )
        )

    immutable_tables = sorted(_IMMUTABLE_TABLES.intersection(table_names))
    if immutable_tables:
        immutable_function = _qualified(security_schema, "reject_immutable_mutation")
        statements.append(
            f"""
CREATE OR REPLACE FUNCTION {immutable_function}()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $life_coach_function$
BEGIN
    RAISE EXCEPTION 'append-only row cannot be updated or deleted' USING ERRCODE = '55000';
END
$life_coach_function$
""".strip()
        )
        for table_name in immutable_tables:
            table = _qualified(data_schema, table_name)
            statements.extend(
                (
                    f'DROP TRIGGER IF EXISTS "lc_append_only" ON {table}',
                    f'CREATE TRIGGER "lc_append_only" BEFORE UPDATE OR DELETE ON {table} '
                    f"FOR EACH ROW EXECUTE FUNCTION {immutable_function}()",
                )
            )
        statements.append(f"REVOKE ALL ON FUNCTION {immutable_function}() FROM PUBLIC")

    source_tombstone_contract = {
        "vault": {"id", "policy_epoch", "source_generation", "updated_at", "deleted_at"},
        "source_document": {"id", "vault_id", "updated_at", "deleted_at"},
        "source_revision": {"id", "vault_id", "document_id"},
        "source_fragment": {"id", "vault_id", "revision_id", "updated_at", "deleted_at"},
        "search_projection": {
            "vault_id",
            "source_fragment_id",
            "updated_at",
            "deleted_at",
            "lexical_terms",
            "embedding",
            "tokenizer_version",
            "embedding_version",
        },
    }
    table_contracts = {table.name: table.columns for table in tenant_tables}
    if all(
        required_columns.issubset(table_contracts.get(table_name, frozenset()))
        for table_name, required_columns in source_tombstone_contract.items()
    ):
        isolate_source = _qualified(security_schema, "isolate_source_document")
        document = _qualified(data_schema, "source_document")
        revision = _qualified(data_schema, "source_revision")
        fragment = _qualified(data_schema, "source_fragment")
        projection = _qualified(data_schema, "search_projection")
        statements.extend(
            (
                f"""
CREATE OR REPLACE FUNCTION {isolate_source}(
    target_vault uuid,
    target_document uuid,
    tombstoned_at timestamp with time zone
)
RETURNS TABLE(policy_epoch integer, source_generation integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $life_coach_function$
DECLARE
    scoped_vault uuid;
    affected_documents integer;
BEGIN
    scoped_vault := NULLIF(pg_catalog.current_setting('app.vault_id', true), '')::uuid;
    IF scoped_vault IS NULL OR target_vault IS DISTINCT FROM scoped_vault THEN
        RAISE EXCEPTION 'vault scope does not authorize Source isolation'
            USING ERRCODE = '42501';
    END IF;

    UPDATE {projection} AS candidate
    SET deleted_at = tombstoned_at,
        updated_at = tombstoned_at,
        lexical_terms = NULL,
        embedding = NULL,
        tokenizer_version = NULL,
        embedding_version = NULL
    WHERE candidate.vault_id = target_vault
      AND candidate.deleted_at IS NULL
      AND EXISTS (
          SELECT 1
          FROM {fragment} AS source_fragment
          JOIN {revision} AS source_revision
            ON source_revision.vault_id = source_fragment.vault_id
           AND source_revision.id = source_fragment.revision_id
          WHERE source_fragment.vault_id = target_vault
            AND source_fragment.id = candidate.source_fragment_id
            AND source_revision.document_id = target_document
      );

    UPDATE {fragment} AS candidate
    SET deleted_at = tombstoned_at,
        updated_at = tombstoned_at
    WHERE candidate.vault_id = target_vault
      AND candidate.deleted_at IS NULL
      AND EXISTS (
          SELECT 1
          FROM {revision} AS source_revision
          WHERE source_revision.vault_id = target_vault
            AND source_revision.id = candidate.revision_id
            AND source_revision.document_id = target_document
      );

    UPDATE {document} AS candidate
    SET deleted_at = tombstoned_at,
        updated_at = tombstoned_at
    WHERE candidate.vault_id = target_vault
      AND candidate.id = target_document
      AND candidate.deleted_at IS NULL;
    GET DIAGNOSTICS affected_documents = ROW_COUNT;
    IF affected_documents <> 1 THEN
        RAISE EXCEPTION 'Source document is unavailable' USING ERRCODE = 'P0002';
    END IF;

    RETURN QUERY
    UPDATE {vault} AS owner_vault
    SET policy_epoch = owner_vault.policy_epoch + 1,
        source_generation = owner_vault.source_generation + 1,
        updated_at = tombstoned_at
    WHERE owner_vault.id = target_vault
      AND owner_vault.deleted_at IS NULL
    RETURNING owner_vault.policy_epoch, owner_vault.source_generation;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Vault is unavailable' USING ERRCODE = 'P0002';
    END IF;
END
$life_coach_function$
""".strip(),
                f"REVOKE ALL ON FUNCTION {isolate_source}"
                "(uuid, uuid, timestamp with time zone) FROM PUBLIC",
                f"GRANT EXECUTE ON FUNCTION {isolate_source}"
                f"(uuid, uuid, timestamp with time zone) TO {app_role}",
            )
        )

    if "consent_record" in table_names:
        consent_function = _qualified(security_schema, "allocate_consent_policy_epoch")
        consent = _qualified(data_schema, "consent_record")
        advance_policy = _qualified(security_schema, "advance_policy_epoch")
        statements.extend(
            (
                f"""
CREATE OR REPLACE FUNCTION {consent_function}()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $life_coach_function$
BEGIN
    NEW.policy_epoch := {advance_policy}(NEW.vault_id);
    RETURN NEW;
END
$life_coach_function$
""".strip(),
                f'DROP TRIGGER IF EXISTS "lc_consent_epoch" ON {consent}',
                f'CREATE TRIGGER "lc_consent_epoch" BEFORE INSERT ON {consent} '
                f"FOR EACH ROW EXECUTE FUNCTION {consent_function}()",
                f"REVOKE ALL ON FUNCTION {consent_function}() FROM PUBLIC",
            )
        )

    tombstone_tables = tuple(
        table_name
        for table_name in ("source_document", "source_fragment", "search_projection")
        if table_name in table_names
    )
    if tombstone_tables:
        tombstone_function = _qualified(security_schema, "reject_tombstone_recovery")
        statements.append(
            f"""
CREATE OR REPLACE FUNCTION {tombstone_function}()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $life_coach_function$
BEGIN
    IF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL THEN
        RAISE EXCEPTION 'a Source tombstone cannot be removed' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$life_coach_function$
""".strip()
        )
        for table_name in tombstone_tables:
            table = _qualified(data_schema, table_name)
            statements.extend(
                (
                    f'DROP TRIGGER IF EXISTS "lc_tombstone_monotonic" ON {table}',
                    f'CREATE TRIGGER "lc_tombstone_monotonic" BEFORE UPDATE OF deleted_at '
                    f"ON {table} FOR EACH ROW EXECUTE FUNCTION {tombstone_function}()",
                )
            )
        statements.append(f"REVOKE ALL ON FUNCTION {tombstone_function}() FROM PUBLIC")

    if "source_fragment" in table_names:
        missing = _SOURCE_ANCESTRY_TABLES.difference(table_names)
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise PostgresSecurityConfigurationError(
                f"source_fragment ancestry tables are missing: {missing_list}"
            )
        fragment_function = _qualified(security_schema, "enforce_live_fragment_ancestry")
        source_fragment = _qualified(data_schema, "source_fragment")
        source_revision = _qualified(data_schema, "source_revision")
        source_document = _qualified(data_schema, "source_document")
        statements.extend(
            (
                f"""
CREATE OR REPLACE FUNCTION {fragment_function}()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $life_coach_function$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.deleted_at IS NOT NULL THEN
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM {source_revision} AS revision
        JOIN {source_document} AS document
          ON document.vault_id = revision.vault_id AND document.id = revision.document_id
        JOIN {vault} AS owner_vault ON owner_vault.id = revision.vault_id
        WHERE revision.vault_id = NEW.vault_id AND revision.id = NEW.revision_id
          AND revision.deleted_at IS NULL AND document.deleted_at IS NULL
          AND owner_vault.deleted_at IS NULL
    ) THEN
        RAISE EXCEPTION 'a fragment requires live Source ancestry' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$life_coach_function$
""".strip(),
                f'DROP TRIGGER IF EXISTS "lc_live_ancestry" ON {source_fragment}',
                f'CREATE TRIGGER "lc_live_ancestry" BEFORE INSERT OR UPDATE ON '
                f"{source_fragment} FOR EACH ROW EXECUTE FUNCTION {fragment_function}()",
                f"REVOKE ALL ON FUNCTION {fragment_function}() FROM PUBLIC",
            )
        )

    if "search_projection" in table_names:
        missing = _SOURCE_ANCESTRY_TABLES.union({"search_projection"}).difference(table_names)
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise PostgresSecurityConfigurationError(
                f"search_projection ancestry tables are missing: {missing_list}"
            )
        projection_function = _qualified(security_schema, "enforce_live_projection_ancestry")
        projection = _qualified(data_schema, "search_projection")
        source_fragment = _qualified(data_schema, "source_fragment")
        source_revision = _qualified(data_schema, "source_revision")
        source_document = _qualified(data_schema, "source_document")
        statements.extend(
            (
                f"""
CREATE OR REPLACE FUNCTION {projection_function}()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $life_coach_function$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.deleted_at IS NOT NULL THEN
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM {source_fragment} AS fragment
        JOIN {source_revision} AS revision
          ON revision.vault_id = fragment.vault_id AND revision.id = fragment.revision_id
        JOIN {source_document} AS document
          ON document.vault_id = revision.vault_id AND document.id = revision.document_id
        JOIN {vault} AS owner_vault ON owner_vault.id = fragment.vault_id
        WHERE fragment.vault_id = NEW.vault_id AND fragment.id = NEW.source_fragment_id
          AND fragment.deleted_at IS NULL AND revision.deleted_at IS NULL
          AND document.deleted_at IS NULL AND owner_vault.deleted_at IS NULL
    ) THEN
        RAISE EXCEPTION 'a search projection requires live Source ancestry'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$life_coach_function$
""".strip(),
                f'DROP TRIGGER IF EXISTS "lc_live_ancestry" ON {projection}',
                f'CREATE TRIGGER "lc_live_ancestry" BEFORE INSERT OR UPDATE ON '
                f"{projection} FOR EACH ROW EXECUTE FUNCTION {projection_function}()",
                f"REVOKE ALL ON FUNCTION {projection_function}() FROM PUBLIC",
            )
        )

    return tuple(statements)


def apply_postgres_security(
    connection: Connection,
    metadata: MetaData,
    *,
    data_schema: str = DEFAULT_DATA_SCHEMA,
    security_schema: str = DEFAULT_SECURITY_SCHEMA,
    business_role: str = DEFAULT_BUSINESS_ROLE,
    maintenance_role: str = DEFAULT_MAINTENANCE_ROLE,
    bootstrap_roles: bool = True,
) -> tuple[TenantTable, ...]:
    """Install the complete trust boundary after tables are created.

    Deployments that provision roles outside Alembic pass ``bootstrap_roles=False``.
    The migration identity must own tables/functions; runtime identities must only
    ``SET ROLE`` to one of the provisioned NOLOGIN group roles.
    """

    if connection.dialect.name != "postgresql":
        raise PostgresSecurityConfigurationError("PostgreSQL security DDL requires PostgreSQL")
    tenant_tables = discover_tenant_tables(metadata, data_schema=data_schema)
    statements: list[str] = []
    if bootstrap_roles:
        statements.extend(build_business_role_bootstrap_statements(business_role=business_role))
        statements.extend(
            build_maintenance_role_bootstrap_statements(maintenance_role=maintenance_role)
        )
    statements.extend(
        build_integrity_trigger_statements(
            tenant_tables,
            data_schema=data_schema,
            security_schema=security_schema,
            business_role=business_role,
        )
    )
    statements.extend(
        build_rls_statements(
            tenant_tables,
            data_schema=data_schema,
            business_role=business_role,
            maintenance_role=maintenance_role,
        )
    )
    statements.extend(
        build_table_privilege_statements(
            tenant_tables,
            data_schema=data_schema,
            security_schema=security_schema,
            business_role=business_role,
            maintenance_role=maintenance_role,
        )
    )
    for statement in statements:
        # TextClause works with both a real Connection and Alembic's offline
        # MockConnection, so ``alembic upgrade --sql`` emits the same trust boundary.
        connection.execute(text(statement))
    return tenant_tables


__all__ = [
    "DEFAULT_BUSINESS_ROLE",
    "DEFAULT_DATA_SCHEMA",
    "DEFAULT_MAINTENANCE_ROLE",
    "DEFAULT_SECURITY_SCHEMA",
    "PostgresSecurityConfigurationError",
    "TenantTable",
    "apply_postgres_security",
    "build_business_role_bootstrap_statements",
    "build_integrity_trigger_statements",
    "build_maintenance_role_bootstrap_statements",
    "build_rls_statements",
    "build_table_privilege_statements",
    "discover_tenant_tables",
]
