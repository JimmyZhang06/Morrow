"""Add disposable local diary passage indexes without changing source identities."""

import sqlalchemy as sa

from alembic import context, op
from life_coach.platform.model_registry import load_model_registry
from life_coach.platform.postgres_security import apply_postgres_security
from life_coach.shared.database import Base

revision = "12ab34cd56ef"
down_revision = "c9a7e3f1b204"
branch_labels = None
depends_on = None


def _security() -> None:
    load_model_registry()
    # Reflect the actual revision, including downgrade; earlier schemas have no passages.
    metadata = Base.metadata
    if not context.is_offline_mode():
        metadata = sa.MetaData()
        metadata.reflect(bind=op.get_bind())
    apply_postgres_security(op.get_bind(), metadata)


def upgrade() -> None:
    load_model_registry()
    Base.metadata.tables["diary_index_job"].create(bind=op.get_bind())
    op.add_column("search_projection", sa.Column("lexical_passages", sa.JSON(none_as_null=True)))
    op.create_check_constraint(
        "search_projection_passages_authorized_shape", "search_projection",
        "lexical_passages IS NULL OR (index_policy IN ('lexical', 'both') "
        "AND data_class <> 'highly_sensitive' AND deleted_at IS NULL)",
    )
    _security()


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS life_coach_private.next_diary_index_job()")
    op.drop_table("diary_index_job")
    op.drop_constraint(
        "search_projection_passages_authorized_shape", "search_projection", type_="check",
    )
    op.drop_column("search_projection", "lexical_passages")
    _security()
