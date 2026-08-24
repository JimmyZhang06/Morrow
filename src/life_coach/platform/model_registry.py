"""Explicit model imports used to populate the shared SQLAlchemy metadata."""

from __future__ import annotations

from importlib import import_module

# This is deliberately explicit: importing a new domain model without adding it
# here must fail the migration/metadata parity test instead of silently omitting
# its tables from Alembic.
MODEL_MODULES: tuple[str, ...] = (
    "life_coach.modules.identity.models",
    "life_coach.modules.sources.models",
    "life_coach.modules.consent.models",
    "life_coach.modules.knowledge.models",
    "life_coach.modules.action.models",
    "life_coach.modules.model_runs.models",
    "life_coach.jobs.models",
)


def load_model_registry() -> None:
    """Import every explicitly registered model module exactly once per interpreter."""

    for module_name in MODEL_MODULES:
        import_module(module_name)
