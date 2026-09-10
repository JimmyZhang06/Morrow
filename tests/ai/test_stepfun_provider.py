from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from life_coach.ai.compatible import CompatibleChatCompletionsProvider, normalize_base_url
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
    StepFunConnectionError,
    StepFunProviderError,
)

_KEY = "stepfun-secret-value-must-never-escape"
_PRIVATE = "USER_DATA private fragment"


@pytest.mark.parametrize("json_mode", [False, True])
def test_custom_endpoint_uses_minimal_body_and_shared_validation(json_mode: bool) -> None:
    sent = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"value":"ok"}'}}]})

    provider = CompatibleChatCompletionsProvider(
        api_key=SecretStr(_KEY), client=httpx.Client(transport=httpx.MockTransport(respond)),
        base_url="https://models.example/v1/", model="vendor/model:latest", json_mode=json_mode,
    )
    assert provider.complete(_request()) == {"value": "ok"}
    assert str(sent[0].url) == "https://models.example/v1/chat/completions"
    body = json.loads(sent[0].content)
    assert body["model"] == "vendor/model:latest"
    assert ("response_format" in body) is json_mode
    assert "reasoning_effort" not in body
    assert "temperature" not in body
    assert "JSON_SCHEMA=" in body["messages"][0]["content"]
    assert sent[0].headers["authorization"] == f"Bearer {_KEY}"


@pytest.mark.parametrize("url", [
    "http://models.example/v1", "https://key@models.example/v1",
    "https://models.example/v1?key=secret", "https://models.example/v1#secret",
    "https://models.example/v1/chat/completions", "https://models.example\\evil/v1",
])
def test_custom_endpoint_rejects_unsafe_or_complete_urls(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_base_url(url)


def test_custom_endpoint_never_follows_redirect_even_with_redirecting_client() -> None:
    sent = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(307, headers={"location": "https://other.example/v1"})

    provider = CompatibleChatCompletionsProvider(
        api_key=SecretStr(_KEY), model="model", base_url="https://models.example/v1",
        client=httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True),
    )
    with pytest.raises(StepFunProviderError):
        provider.complete(_request())
    assert len(sent) == 1


def test_inactive_subscription_reports_only_a_safe_code() -> None:
    provider = StepFunChatCompletionsProvider(
        api_key=SecretStr(_KEY),
        client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
            400, json={"error": {"message": "you have no active step plan subscription"}}
        ))),
    )
    with pytest.raises(StepFunProviderError) as captured:
        provider.complete(_request())
    assert captured.value.failure_code == "subscription_inactive"
    assert _KEY not in repr(captured.value)


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
    assert "same language as the source text" in messages[0]["content"]
    assert "tentative second-person voice" in messages[0]["content"]
    assert "Never call them 'the user'" in messages[0]["content"]
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


def test_stepfun_marks_connect_failure_as_not_dispatched_without_leaking() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"proxy failed {_PRIVATE}", request=request)

    provider = StepFunChatCompletionsProvider(
        api_key=SecretStr(_KEY),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(StepFunConnectionError) as captured:
        provider.complete(_request())

    assert captured.value.failure_code == "transport_connect_failed"
    assert _PRIVATE not in repr(captured.value)
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


def test_stepfun_accepts_one_exact_fenced_json_object() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '```json\n{"value":"ok"}\n```'}}]},
        )

    provider = StepFunChatCompletionsProvider(
        api_key=SecretStr(_KEY),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert provider.complete(_request()) == {"value": "ok"}


def test_stepfun_classifies_reasoning_only_length_response_without_leaking_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": None, "reasoning_content": _PRIVATE},
                    }
                ]
            },
        )

    provider = StepFunChatCompletionsProvider(
        api_key=SecretStr(_KEY),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(StepFunProviderError) as captured:
        provider.complete(_request())
    assert captured.value.failure_code == "response_content_invalid_length_reasoning_only"
    assert _PRIVATE not in repr(captured.value)
