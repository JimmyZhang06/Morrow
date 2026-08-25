from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from life_coach.jobs.enums import JobState, OutboundOperationState
from life_coach.jobs.models import Job, OutboxEvent
from life_coach.jobs.repository import (
    begin_outbound_execution_statement,
    cancel_claim_statement,
    claim_job_statement,
    complete_job_statement,
    current_claim_binding_statement,
    expire_stale_reconciliations_statement,
    expire_uncertain_outbound_executions_statement,
    failure_job_statement,
    heartbeat_job_statement,
    lock_current_claim_binding_statement,
    set_local_vault_statement,
)


def _postgresql_sql(statement: object) -> str:
    compiled = statement.compile(  # type: ignore[attr-defined]
        dialect=postgresql.dialect(),
    )
    return " ".join(str(compiled).lower().split())


def test_claim_is_one_database_clock_update_returning_metadata_only() -> None:
    sql = _postgresql_sql(claim_job_statement())

    assert sql.startswith("with claim_candidate as")
    assert "for update skip locked" in sql
    assert " update job set " in sql
    assert sql.count("clock_timestamp()") >= 3
    assert "now()" not in sql
    assert "job.lease_generation +" in sql
    assert "job.state in" in sql
    assert "job.state =" in sql  # expired running reclaim branch
    returning = sql.split(" returning ", maxsplit=1)[1]
    assert "job.id as job_id" in returning
    assert "job.vault_id as vault_id" in returning
    assert "job.lease_generation as lease_generation" in returning
    assert "payload" not in returning
    assert "resource_id" not in returning
    assert "job_type" not in returning


def test_heartbeat_matches_owner_generation_and_unexpired_database_lease() -> None:
    sql = _postgresql_sql(heartbeat_job_statement())

    assert "job.lease_owner =" in sql
    assert "job.lease_generation =" in sql
    assert "job.lease_expires_at > clock_timestamp()" in sql
    assert "lease_expires_at=(clock_timestamp() +" in sql


def test_completion_matches_lease_and_all_authoritative_fence_inputs() -> None:
    sql = _postgresql_sql(complete_job_statement())

    assert "job.lease_generation =" in sql
    assert "job.lease_expires_at > clock_timestamp()" in sql
    assert "job.policy_epoch =" in sql
    assert "job.source_generation =" in sql
    assert "tombstone_clear" not in sql
    assert "returning job.id" in sql


def test_job_settlement_execution_parameter_names_do_not_collide_with_columns() -> None:
    statements_and_keys = (
        (
            heartbeat_job_statement(),
            ["hb_job_id", "hb_vault_id", "hb_claimed_by", "hb_lease_generation", "hb_lease_for"],
        ),
        (
            complete_job_statement(),
            [
                "complete_job_id",
                "complete_vault_id",
                "complete_claimed_by",
                "complete_lease_generation",
                "complete_policy_epoch",
                "complete_source_generation",
            ],
        ),
        (
            cancel_claim_statement(),
            ["cancel_job_id", "cancel_vault_id", "cancel_claimed_by", "cancel_lease_generation"],
        ),
        (
            failure_job_statement(JobState.DEAD),
            [
                "failure_job_id",
                "failure_vault_id",
                "failure_claimed_by",
                "failure_lease_generation",
                "last_error_class",
                "safe_error_message",
            ],
        ),
    )
    for statement, column_keys in statements_and_keys:
        statement.compile(dialect=postgresql.dialect(), column_keys=column_keys)


def test_sensitive_gate_rechecks_and_then_locks_a_live_exact_database_lease() -> None:
    initial_sql = _postgresql_sql(current_claim_binding_statement())
    locked_sql = _postgresql_sql(lock_current_claim_binding_statement())

    assert "job.lease_owner =" in initial_sql
    assert "job.lease_generation =" in initial_sql
    assert "job.lease_expires_at > clock_timestamp()" in initial_sql
    assert "for update" not in initial_sql
    assert "job.resource_id =" in locked_sql
    assert "job.policy_epoch =" in locked_sql
    assert "job.source_generation =" in locked_sql
    assert "job.lease_expires_at > clock_timestamp()" in locked_sql
    assert "for update" in locked_sql


def test_processor_scope_is_transaction_local() -> None:
    sql = _postgresql_sql(set_local_vault_statement())

    assert "set_config" in sql
    assert "app.vault_id" in sql
    assert "processor_vault_id" in sql


def test_outbound_execution_query_excludes_unknown_state() -> None:
    statement = begin_outbound_execution_statement()
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = " ".join(str(compiled).lower().split())

    assert OutboundOperationState.PENDING in compiled.params.values()
    assert OutboundOperationState.UNKNOWN not in compiled.params.values()
    assert "execution_generation=(outbound_operation.execution_generation +" in sql
    assert "execution_expires_at=(clock_timestamp() +" in sql
    assert "outbound_operation.attempts < outbound_operation.max_attempts" in sql
    assert "outbound_operation.authorization_id =" in sql
    assert "outbound_operation.authorization_generation =" in sql
    assert "outbound_operation.resource_id =" in sql
    assert "outbound_operation.policy_epoch =" in sql
    assert "outbound_operation.source_generation =" in sql
    assert "outbound_operation.initial_tombstoned =" in sql
    assert "outbound_operation.request_hash =" in sql


def test_crashed_external_work_moves_to_unknown_using_database_clock() -> None:
    execution = expire_uncertain_outbound_executions_statement().compile(
        dialect=postgresql.dialect()
    )
    reconciliation = expire_stale_reconciliations_statement().compile(dialect=postgresql.dialect())
    execution_sql = " ".join(str(execution).lower().split())
    reconciliation_sql = " ".join(str(reconciliation).lower().split())

    assert "execution_expires_at <= clock_timestamp()" in execution_sql
    assert OutboundOperationState.UNKNOWN in execution.params.values()
    assert "reconciliation_expires_at <= clock_timestamp()" in reconciliation_sql
    assert OutboundOperationState.UNKNOWN in reconciliation.params.values()


def test_job_outbox_foreign_key_is_vault_aware() -> None:
    job_ddl = str(CreateTable(Job.__table__).compile(dialect=postgresql.dialect())).lower()
    outbox_ddl = str(
        CreateTable(OutboxEvent.__table__).compile(dialect=postgresql.dialect())
    ).lower()

    assert "foreign key(outbox_event_id, vault_id)" in " ".join(job_ddl.split())
    assert "references outbox_event (id, vault_id)" in " ".join(job_ddl.split())
    assert "unique (id, vault_id)" in " ".join(outbox_ddl.split())
