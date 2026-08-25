"""add durable background-job request context

Revision ID: c6d2e4f8a913
Revises: a9c4e7b2d615
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c6d2e4f8a913"
down_revision = "a9c4e7b2d615"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job", sa.Column("requested_by_principal_id", sa.Uuid(), nullable=True))
    op.add_column("job", sa.Column("membership_generation", sa.Integer(), nullable=True))
    op.add_column("job", sa.Column("expected_resource_revision", sa.Integer(), nullable=True))
    op.add_column(
        "job", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_job_requested_by_principal",
        "job",
        "principal",
        ["requested_by_principal_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_job_job_requester_membership_shape"),
        "job",
        "(requested_by_principal_id IS NULL AND membership_generation IS NULL) OR "
        "(requested_by_principal_id IS NOT NULL AND membership_generation > 0)",
    )
    op.create_check_constraint(
        op.f("ck_job_job_expected_resource_revision_positive"),
        "job",
        "expected_resource_revision IS NULL OR expected_resource_revision > 0",
    )
    op.execute(
        """
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
    )
    op.execute(
        "REVOKE ALL ON FUNCTION life_coach_private.claim_candidate_insight_job(text, integer) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "life_coach_private.claim_candidate_insight_job(text, integer) TO life_coach_app"
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "life_coach_private.claim_candidate_insight_job(text, integer)"
    )
    op.drop_constraint(
        op.f("ck_job_job_expected_resource_revision_positive"),
        "job",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_job_job_requester_membership_shape"),
        "job",
        type_="check",
    )
    op.drop_constraint("fk_job_requested_by_principal", "job", type_="foreignkey")
    op.drop_column("job", "cancel_requested_at")
    op.drop_column("job", "expected_resource_revision")
    op.drop_column("job", "membership_generation")
    op.drop_column("job", "requested_by_principal_id")
