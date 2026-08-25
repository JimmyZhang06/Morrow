from __future__ import annotations

import uuid
from dataclasses import fields
from typing import Any, cast

import pytest

from life_coach.ai.contracts import ModelInputKind, RetentionPolicy, SensitivityLevel
from life_coach.jobs.payloads import VaultRequestFingerprint, canonical_request_hash
from life_coach.modules.model_runs.contracts import (
    ModelRunArtifactSpec,
    ModelRunDispatchTicket,
    ModelRunInputSpec,
    ModelRunReceiptSpec,
)

_HMAC_KEY = b"test-only-model-run-hmac-key-material"


def _fingerprint(vault_id: uuid.UUID, value: object) -> VaultRequestFingerprint:
    return canonical_request_hash(
        value,  # type: ignore[arg-type]
        vault_id=vault_id,
        hmac_key=_HMAC_KEY,
    )


def _receipt_spec(vault_id: uuid.UUID) -> ModelRunReceiptSpec:
    return ModelRunReceiptSpec(
        vault_id=vault_id,
        task_type="candidate_insight",
        idempotency_key="modelrun:1001",
        request_hash=_fingerprint(vault_id, {"request": 1}),
        task_definition_hash=_fingerprint(vault_id, {"task": "candidate_insight:v1"}),
        provider="provider:1001",
        model="model:1001",
        model_revision="rev-1",
        prompt_template_version="prompt-v1",
        schema_version="schema-v1",
        pipeline_version="pipeline-v1",
        consent_snapshot_id=f"consent:{'c' * 64}",
        policy_epoch=3,
        source_generation=7,
        actual_sensitivity=SensitivityLevel.SENSITIVE,
        data_residency="region:1001",
        retention_policy=RetentionPolicy.ZERO_RETENTION,
    )


def test_receipt_contract_has_no_content_prompt_or_output_field() -> None:
    names = {field.name for field in fields(ModelRunReceiptSpec)}

    assert names.isdisjoint(
        {
            "content",
            "plaintext",
            "prompt",
            "input",
            "output",
            "response",
            "provider_error",
            "error_message",
        }
    )


def test_input_contract_can_only_represent_technical_reference_and_digest() -> None:
    assert [field.name for field in fields(ModelRunInputSpec)] == [
        "vault_id",
        "kind",
        "object_id",
        "content_fingerprint",
        "ordinal",
    ]
    vault_id = uuid.uuid4()
    value = ModelRunInputSpec(
        vault_id=vault_id,
        kind=ModelInputKind.SOURCE_FRAGMENT,
        object_id=uuid.uuid4(),
        content_fingerprint=_fingerprint(vault_id, {"source": "revision:1001"}),
        ordinal=0,
    )

    assert value.content_fingerprint.startswith("hmac-sha256:v1:")


def test_artifact_contract_can_only_represent_vault_scoped_governed_ids() -> None:
    assert [field.name for field in fields(ModelRunArtifactSpec)] == [
        "vault_id",
        "derived_object_id",
        "memory_claim_id",
        "action_id",
        "artifact_kind",
    ]


@pytest.mark.parametrize(
    "digest",
    ["private diary body", "A" * 64, "a" * 64, "hmac-sha256:v1:short"],
)
def test_input_contract_rejects_plaintext_or_unkeyed_digest(digest: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        ModelRunInputSpec(
            vault_id=uuid.uuid4(),
            kind=ModelInputKind.SOURCE_FRAGMENT,
            object_id=uuid.uuid4(),
            content_fingerprint=cast(Any, digest),
            ordinal=0,
        )

    assert digest not in str(exc_info.value)


def test_input_fingerprint_is_bound_to_its_declared_vault() -> None:
    vault_a, vault_b = uuid.uuid4(), uuid.uuid4()

    with pytest.raises(ValueError, match="repository vault"):
        ModelRunInputSpec(
            vault_id=vault_b,
            kind=ModelInputKind.SOURCE_FRAGMENT,
            object_id=uuid.uuid4(),
            content_fingerprint=_fingerprint(vault_a, {"source": "revision:1001"}),
            ordinal=0,
        )


def test_receipt_fingerprints_are_bound_to_the_declared_vault() -> None:
    vault_a, vault_b = uuid.uuid4(), uuid.uuid4()
    spec = _receipt_spec(vault_a)

    with pytest.raises(ValueError, match="repository vault"):
        ModelRunReceiptSpec(
            **{
                **{field.name: getattr(spec, field.name) for field in fields(spec)},
                "vault_id": vault_b,
            }
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("provider", "private diary body"),
        ("model", "这是正文"),
        ("consent_snapshot_id", "consent from private diary"),
        ("pipeline_version", "private/body"),
    ],
)
def test_receipt_rejects_prose_shaped_persistent_metadata(
    field_name: str,
    value: str,
) -> None:
    spec = _receipt_spec(uuid.uuid4())

    with pytest.raises(ValueError) as exc_info:
        ModelRunReceiptSpec(
            **{
                **{field.name: getattr(spec, field.name) for field in fields(spec)},
                field_name: value,
            }
        )

    assert value not in str(exc_info.value)


def test_dispatch_ticket_rejects_zero_generation() -> None:
    with pytest.raises(ValueError, match="positive"):
        ModelRunDispatchTicket(uuid.uuid4(), uuid.uuid4(), 0)
