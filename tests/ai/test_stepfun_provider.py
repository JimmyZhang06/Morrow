from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from life_coach.ai.contracts import (
    ModelInputKind,
    ModelInputRef,
    ModelRunSpec,
    ModelTaskPolicy,
    RetentionPolicy,
    SchemaRef,
    SensitivityLevel,
)
from life_coach.ai.provider import ModelProviderRequest, UntrustedModelInput
from life_coach.ai.stepfun import (
    STEPFUN_DEFAULT_BASE_URL,
    STEPFUN_PROVIDER_ID,
    StepFunChatCompletionsProvider,
    StepFunProviderError,
)

_KEY = "stepfun-secret-value-must-never-escape"
_PRIVATE = "USER_DATA private fragment"


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str


def _request() -> ModelProviderRequest:
    policy = ModelTaskPolicy(
        task_type="candidate_insight",
        required_capabilities=frozenset({"structured_output"}),
        allowed_providers=frozenset({STEPFUN_PROVIDER_ID}),
        data_residency=frozenset({"apac"}),
        retention_policy=RetentionPolicy.PROVIDER_MANAGED,
        max_sensitivity=SensitivityLevel.SENSITIVE,
        input_schema=SchemaRef(name="UntrustedModelInput", version="1"),
        output_schema=SchemaRef(name="Output", version="1"),
        latency_budget_ms=20_000,
        cost_budget=Decimal("0.05"),
    )
    spec = ModelRunSpec(
        run_id="run-1",
        vault_id="vault-1",
        policy=policy,
        provider=STEPFUN_PROVIDER_ID,
        model="step-3.7-flash",
        model_revision="step-3.7-flash",
        prompt_template_version="candidate-v1",
        schema_version="1",
        pipeline_version="candidate-v1",
        consent_snapshot_id="consent-1",
        policy_epoch=1,
        source_generation=1,
        actual_sensitivity=SensitivityLevel.SENSITIVE,
        data_residency="apac",
        retention_policy=RetentionPolicy.PROVIDER_MANAGED,
        input_refs=(
            ModelInputRef(
                vault_id="vault-1",
                kind=ModelInputKind.SOURCE_FRAGMENT,
                object_id="fragment-1",
            ),
        ),
    )
    return ModelProviderRequest(
        run_spec=spec,
        untrusted_input=UntrustedModelInput(
            data={"fragments": [{"source_fragment_id": "fragment-1", "text": _PRIVATE}]},
            source_refs=("fragment-1",),
        ),
        output_schema=_Output.model_json_schema(),
    )


def test_stepfun_uses_only_approved_endpoint_and_json_mode() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "response-1",
                "choices": [{"message": {"role": "assistant", "content": '{"value":"ok"}'}}],
            },
        )

    provider = StepFunChatCompletionsProvider(
        api_key=SecretStr(_KEY),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert provider.complete(_request()) == {"value": "ok"}
    assert captured["url"] == f"{STEPFUN_DEFAULT_BASE_URL}/chat/completions"
    assert captured["authorization"] == f"Bearer {_KEY}"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["response_format"] == {"type": "json_object"}
    assert body["stream"] is False
    assert body["n"] == 1
    messages = body["messages"]
    assert isinstance(messages, list)
    assert "Never call tools" in messages[0]["content"]
    assert _PRIVATE not in messages[0]["content"]
    assert _PRIVATE in messages[1]["content"]


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.stepfun.com/step_plan/v1",
        "https://example.com/step_plan/v1",
        "https://api.stepfun.com/step_plan/v1?key=leak",
    ],
)
def test_stepfun_rejects_unapproved_base_urls(base_url: str) -> None:
    with pytest.raises(ValueError, match="approved Step Plan origin"):
        StepFunChatCompletionsProvider(
            api_key=SecretStr(_KEY),
            client=httpx.Client(),
            base_url=base_url,
        )


def test_stepfun_scrubs_remote_failures_and_secrets() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad key {_KEY}; private={_PRIVATE}")

    provider = StepFunChatCompletionsProvider(
        api_key=SecretStr(_KEY),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(StepFunProviderError) as captured:
        provider.complete(_request())

    rendered = repr(captured.value) + str(captured.value)
    assert _KEY not in rendered
    assert _PRIVATE not in rendered
    assert captured.value.__context__ is None


def test_stepfun_rejects_invalid_json_response_without_leaking_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": _PRIVATE}}]},
        )

    provider = StepFunChatCompletionsProvider(
        api_key=SecretStr(_KEY),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(StepFunProviderError) as captured:
        provider.complete(_request())
    assert _PRIVATE not in repr(captured.value)
