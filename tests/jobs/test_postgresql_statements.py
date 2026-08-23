from __future__ import annotations

from sqlalchemy.dialects import postgresql

from life_coach.jobs.enums import OutboundOperationState
from life_coach.jobs.repository import (
    begin_outbound_execution_statement,
    claim_job_statement,
    complete_job_statement,
    expire_stale_reconciliations_statement,
    expire_uncertain_outbound_executions_statement,
    heartbeat_job_statement,
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
    assert sql.count("now()") >= 3
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
    assert "job.lease_expires_at > now()" in sql
    assert "lease_expires_at=(now() +" in sql


def test_completion_matches_lease_and_all_authoritative_fence_inputs() -> None:
    sql = _postgresql_sql(complete_job_statement())

    assert "job.lease_generation =" in sql
    assert "job.lease_expires_at > now()" in sql
    assert "job.policy_epoch =" in sql
    assert "job.source_generation =" in sql
    assert "tombstone_clear" in sql
    assert "returning job.id" in sql


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
    assert "execution_expires_at=(now() +" in sql


def test_crashed_external_work_moves_to_unknown_using_database_clock() -> None:
    execution = expire_uncertain_outbound_executions_statement().compile(
        dialect=postgresql.dialect()
    )
    reconciliation = expire_stale_reconciliations_statement().compile(dialect=postgresql.dialect())
    execution_sql = " ".join(str(execution).lower().split())
    reconciliation_sql = " ".join(str(reconciliation).lower().split())

    assert "execution_expires_at <= now()" in execution_sql
    assert OutboundOperationState.UNKNOWN in execution.params.values()
    assert "reconciliation_expires_at <= now()" in reconciliation_sql
    assert OutboundOperationState.UNKNOWN in reconciliation.params.values()
