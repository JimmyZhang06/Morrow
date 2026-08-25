"""reconcile candidate jobs from authoritative model receipts

Revision ID: e7a1c5d9b204
Revises: c6d2e4f8a913
Create Date: 2026-08-25
"""

from __future__ import annotations

from alembic import op

revision = "e7a1c5d9b204"
down_revision = "c6d2e4f8a913"
branch_labels = None
depends_on = None


_FUNCTION = """
CREATE OR REPLACE FUNCTION life_coach_private.claim_candidate_insight_job(
    p_lease_owner text,
    p_lease_seconds integer
)
RETURNS TABLE(job_id uuid, vault_id uuid, lease_generation integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF p_lease_owner !~ '^([0-9a-f-]{36}|[a-z][a-z0-9_.-]{0,31}:[0-9a-f]{16,64})$'
       OR p_lease_seconds < 1 OR p_lease_seconds > 600 THEN
        RAISE EXCEPTION 'invalid candidate job lease request' USING ERRCODE = '22023';
    END IF;

    -- A ModelRun is the authoritative inference receipt. If a worker completed
    -- that fenced transaction but crashed before Job bookkeeping, converge the
    -- metadata without dispatching the provider a second time.
    UPDATE public.job AS settled
    SET state = 'done',
        lease_owner = NULL,
        lease_expires_at = NULL,
        completed_at = receipt.completed_at,
        last_error_class = NULL,
        safe_error_message = NULL
    FROM public.model_run AS receipt
    WHERE settled.job_type = 'candidate_insight.generate'
      AND settled.state = 'running'
      AND settled.lease_expires_at <= pg_catalog.clock_timestamp()
      AND receipt.vault_id = settled.vault_id
      AND receipt.task_type = 'candidate_insight'
      AND receipt.idempotency_key = settled.idempotency_key
      AND receipt.state = 'succeeded';

    UPDATE public.job AS uncertain
    SET state = 'waiting',
        lease_owner = NULL,
        lease_expires_at = NULL,
        last_error_class = 'external_outcome_unknown',
        safe_error_message = 'external outcome requires reconciliation'
    FROM public.model_run AS receipt
    WHERE uncertain.job_type = 'candidate_insight.generate'
      AND uncertain.state = 'running'
      AND uncertain.lease_expires_at <= pg_catalog.clock_timestamp()
      AND receipt.vault_id = uncertain.vault_id
      AND receipt.task_type = 'candidate_insight'
      AND receipt.idempotency_key = uncertain.idempotency_key
      AND (
          receipt.state = 'unknown'
          OR (receipt.state = 'dispatching'
              AND receipt.dispatch_expires_at <= pg_catalog.clock_timestamp())
      );

    UPDATE public.job AS exhausted
    SET state = 'dead',
        lease_owner = NULL,
        lease_expires_at = NULL,
        completed_at = pg_catalog.clock_timestamp(),
        last_error_class = 'attempts_exhausted',
        safe_error_message = 'maximum processing attempts reached'
    WHERE exhausted.job_type = 'candidate_insight.generate'
      AND exhausted.state = 'running'
      AND exhausted.lease_expires_at <= pg_catalog.clock_timestamp()
      AND exhausted.attempts >= exhausted.max_attempts
      AND NOT EXISTS (
          SELECT 1 FROM public.model_run AS receipt
          WHERE receipt.vault_id = exhausted.vault_id
            AND receipt.task_type = 'candidate_insight'
            AND receipt.idempotency_key = exhausted.idempotency_key
            AND receipt.state IN ('dispatching', 'succeeded', 'unknown')
      );

    RETURN QUERY
    WITH candidate AS (
        SELECT pending.id
        FROM public.job AS pending
        WHERE pending.queue = 'reflection'
          AND pending.job_type = 'candidate_insight.generate'
          AND pending.cancel_requested_at IS NULL
          AND pending.attempts < pending.max_attempts
          AND (
              (pending.state IN ('queued', 'retrying')
               AND pending.run_after <= pg_catalog.clock_timestamp())
              OR
              (pending.state = 'running'
               AND pending.lease_expires_at IS NOT NULL
               AND pending.lease_expires_at <= pg_catalog.clock_timestamp())
          )
        ORDER BY pending.priority DESC, pending.run_after, pending.created_at, pending.id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    UPDATE public.job AS claimed
    SET state = 'running',
        attempts = claimed.attempts + 1,
        lease_owner = p_lease_owner,
        lease_expires_at = pg_catalog.clock_timestamp()
            + pg_catalog.make_interval(secs => p_lease_seconds),
        lease_generation = claimed.lease_generation + 1,
        completed_at = NULL
    FROM candidate
    WHERE claimed.id = candidate.id
    RETURNING claimed.id, claimed.vault_id, claimed.lease_generation;
END
$function$
"""

_CLAIM_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION life_coach_private.claim_candidate_insight_job(
    p_lease_owner text,
    p_lease_seconds integer
)
RETURNS TABLE(job_id uuid, vault_id uuid, lease_generation integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF p_lease_owner !~ '^([0-9a-f-]{36}|[a-z][a-z0-9_.-]{0,31}:[0-9a-f]{16,64})$'
       OR p_lease_seconds < 1 OR p_lease_seconds > 600 THEN
        RAISE EXCEPTION 'invalid candidate job lease request' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    WITH candidate AS (
        SELECT pending.id
        FROM public.job AS pending
        WHERE pending.queue = 'reflection'
          AND pending.job_type = 'candidate_insight.generate'
          AND pending.cancel_requested_at IS NULL
          AND pending.attempts < pending.max_attempts
          AND (
              (pending.state IN ('queued', 'retrying')
               AND pending.run_after <= pg_catalog.clock_timestamp())
              OR
              (pending.state = 'running'
               AND pending.lease_expires_at IS NOT NULL
               AND pending.lease_expires_at <= pg_catalog.clock_timestamp())
          )
        ORDER BY pending.priority DESC, pending.run_after, pending.created_at, pending.id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    UPDATE public.job AS claimed
    SET state = 'running',
        attempts = claimed.attempts + 1,
        lease_owner = p_lease_owner,
        lease_expires_at = pg_catalog.clock_timestamp()
            + pg_catalog.make_interval(secs => p_lease_seconds),
        lease_generation = claimed.lease_generation + 1,
        completed_at = NULL
    FROM candidate
    WHERE claimed.id = candidate.id
    RETURNING claimed.id, claimed.vault_id, claimed.lease_generation;
END
$function$
"""


def upgrade() -> None:
    op.execute(_FUNCTION)
    op.execute(
        "REVOKE ALL ON FUNCTION life_coach_private.claim_candidate_insight_job(text, integer) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "life_coach_private.claim_candidate_insight_job(text, integer) TO life_coach_app"
    )


def downgrade() -> None:
    op.execute(_CLAIM_ONLY_FUNCTION)
    op.execute(
        "REVOKE ALL ON FUNCTION life_coach_private.claim_candidate_insight_job(text, integer) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "life_coach_private.claim_candidate_insight_job(text, integer) TO life_coach_app"
    )
