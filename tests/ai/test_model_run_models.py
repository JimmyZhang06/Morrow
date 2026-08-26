from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Table, UniqueConstraint

from life_coach.modules.model_runs.models import (
    ModelRun,
    ModelRunArtifact,
    ModelRunInput,
    ModelRunState,
)


def test_model_run_receipt_has_no_body_bearing_column() -> None:
    columns = set(ModelRun.__table__.columns.keys())

    assert columns.isdisjoint(
        {
            "content",
            "plaintext",
            "prompt",
            "input",
            "output",
            "response",
            "provider_error",
            "error_message",
            "payload",
        }
    )
    assert {"provider_request_id", "safe_error_code"} <= columns


def test_model_run_input_has_only_content_free_reference_columns() -> None:
    assert set(ModelRunInput.__table__.columns.keys()) == {
        "id",
        "vault_id",
        "model_run_id",
        "kind",
        "object_id",
        "content_fingerprint",
        "ordinal",
    }


def test_model_run_input_foreign_key_binds_run_and_vault_together() -> None:
    table = cast(Table, ModelRunInput.__table__)
    foreign_keys = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]

    assert len(foreign_keys) == 1
    constraint = foreign_keys[0]
    assert [column.name for column in constraint.columns] == ["vault_id", "model_run_id"]
    assert [element.target_fullname for element in constraint.elements] == [
        "model_run.vault_id",
        "model_run.id",
    ]


def test_model_run_artifact_is_content_free_and_vault_binds_all_authorities() -> None:
    table = cast(Table, ModelRunArtifact.__table__)

    assert set(table.columns.keys()) == {
        "id",
        "vault_id",
        "model_run_id",
        "derived_object_id",
        "memory_claim_id",
        "action_id",
        "narrative_generation_id",
        "artifact_kind",
        "created_at",
    }
    foreign_key_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert foreign_key_targets == {
        ("model_run.vault_id", "model_run.id"),
        ("derived_object.vault_id", "derived_object.id"),
        ("memory_claim.vault_id", "memory_claim.id"),
        ("reversible_action.vault_id", "reversible_action.id"),
        ("narrative_generation.vault_id", "narrative_generation.id"),
    }


def test_model_run_artifact_is_one_per_run_and_derived_candidate() -> None:
    table = cast(Table, ModelRunArtifact.__table__)
    unique = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("vault_id", "model_run_id") in unique
    assert ("vault_id", "derived_object_id") in unique


def test_model_run_scoped_idempotency_and_input_ordinals_are_database_unique() -> None:
    run_table = cast(Table, ModelRun.__table__)
    input_table = cast(Table, ModelRunInput.__table__)
    run_unique = {
        tuple(column.name for column in constraint.columns)
        for constraint in run_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    input_unique = {
        tuple(column.name for column in constraint.columns)
        for constraint in input_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("vault_id", "task_type", "idempotency_key") in run_unique
    assert ("vault_id", "model_run_id", "ordinal") in input_unique
    assert ("vault_id", "model_run_id", "kind", "object_id") in input_unique


def test_model_run_states_distinguish_known_failure_from_unknown_outcome() -> None:
    assert {state.value for state in ModelRunState} == {
        "authorized",
        "dispatching",
        "succeeded",
        "failed",
        "unknown",
        "denied",
        "canceled",
    }


def test_model_run_database_shape_requires_bounded_terminal_outcomes() -> None:
    table = cast(Table, ModelRun.__table__)
    checks = "\n".join(
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    )

    assert "state != 'succeeded' OR safe_error_code IS NULL" in checks
    assert "OR safe_error_code IS NOT NULL" in checks
    assert "OR (safe_error_code IS NULL AND provider_request_id IS NULL)" in checks
