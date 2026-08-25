"""Production composition for the single evidence-backed candidate task."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

import httpx
from pydantic import JsonValue

from life_coach.ai.contracts import RetentionPolicy, SensitivityLevel
from life_coach.ai.fakes import DeterministicFakeProvider
from life_coach.ai.provider import ModelGateway, ModelProvider, ModelProviderRequest
from life_coach.ai.stepfun import STEPFUN_PROVIDER_ID, StepFunChatCompletionsProvider
from life_coach.application.candidate_insight import (
    CANDIDATE_INSIGHT_TASK_TYPE,
    CandidateInsightOutput,
    CandidateInsightPersister,
)
from life_coach.application.candidate_insight_command import CandidateInsightRuntime
from life_coach.application.candidate_insight_safety import (
    CandidateInsightMemorySafetyClassifier,
)
from life_coach.application.model_gateway import (
    GovernedModelGateway,
    KnowledgeAuthorizationSnapshotAdapter,
    ModelTaskDefinition,
    SourceConsentAuthority,
)
from life_coach.application.model_runtime import (
    GovernedModelRuntime,
    ModelRunFingerprintFactory,
)
from life_coach.application.source_entries import (
    ProtectedSourceFragmentPlaintextReader,
    SourceContentProtector,
)
from life_coach.modules.consent.models import ConsentPurpose
from life_coach.platform.auth import ProductionSessionFactory
from life_coach.platform.settings import Settings

_FAKE_PROVIDER_ID = "zero-retention-provider"


@dataclass(frozen=True, slots=True)
class CandidateRuntimeComposition:
    """Runtime plus an optional owned HTTP client for application shutdown."""

    runtime: CandidateInsightRuntime
    http_client: httpx.Client | None = None


def build_candidate_runtime(
    *,
    settings: Settings,
    sessions: ProductionSessionFactory,
    protector: SourceContentProtector,
) -> CandidateRuntimeComposition | None:
    """Build only the registered local fake or StepFun provider."""

    if settings.model_provider == "disabled":
        return None
    if settings.model_run_hmac_key is None:
        raise ValueError("candidate runtime requires model_run_hmac_key")

    owned_client: httpx.Client | None = None
    provider: ModelProvider
    if settings.model_provider == "deterministic-fake":
        provider = DeterministicFakeProvider(
            response_factory=_deterministic_candidate,
            provider_id=_FAKE_PROVIDER_ID,
            data_residencies=("eu",),
            retention_policies=(RetentionPolicy.ZERO_RETENTION,),
        )
        task = _candidate_task(
            provider=_FAKE_PROVIDER_ID,
            model="deterministic-candidate-v1",
            model_revision="v1",
            data_residency="eu",
            retention_policy=RetentionPolicy.ZERO_RETENTION,
            provider_retention_days=0,
            latency_budget_ms=2_000,
            cost_budget=Decimal("0"),
        )
    elif settings.model_provider == STEPFUN_PROVIDER_ID:
        if settings.stepfun_api_key is None:
            raise ValueError("StepFun candidate runtime requires an API key")
        owned_client = httpx.Client(follow_redirects=False, trust_env=False)
        provider = StepFunChatCompletionsProvider(
            api_key=settings.stepfun_api_key,
            client=owned_client,
            model=settings.stepfun_model,
            base_url=settings.stepfun_base_url,
            timeout_seconds=settings.stepfun_timeout_seconds,
        )
        task = _candidate_task(
            provider=STEPFUN_PROVIDER_ID,
            model=settings.stepfun_model,
            model_revision="step-plan-v1",
            data_residency="apac",
            retention_policy=RetentionPolicy.PROVIDER_MANAGED,
            provider_retention_days=None,
            latency_budget_ms=int(settings.stepfun_timeout_seconds * 1_000),
            cost_budget=Decimal("0.02"),
        )
    else:
        raise ValueError("configured model provider is not supported")

    secret = settings.model_run_hmac_key.get_secret_value().encode("utf-8")
    safety_key = hmac.new(secret, b"candidate-safety-v1", hashlib.sha256).digest()
    source_authority = SourceConsentAuthority(ProtectedSourceFragmentPlaintextReader(protector))
    gateway = GovernedModelGateway(
        gateway=ModelGateway((provider,)),
        source_authority=source_authority,
        tasks=(task,),
    )
    persister = CandidateInsightPersister(
        source_authority=source_authority,
        authorization_verifier=KnowledgeAuthorizationSnapshotAdapter(),
        safety_classifier=CandidateInsightMemorySafetyClassifier(safety_key),
    )
    runtime = GovernedModelRuntime(
        sessions=sessions,
        gateway=gateway,
        fingerprints=ModelRunFingerprintFactory(secret),
        result_persister=persister,
    )
    return CandidateRuntimeComposition(runtime=runtime, http_client=owned_client)


def _candidate_task(
    *,
    provider: str,
    model: str,
    model_revision: str,
    data_residency: str,
    retention_policy: RetentionPolicy,
    provider_retention_days: int | None,
    latency_budget_ms: int,
    cost_budget: Decimal,
) -> ModelTaskDefinition:
    return ModelTaskDefinition(
        task_type=CANDIDATE_INSIGHT_TASK_TYPE,
        consent_purpose=ConsentPurpose.LONG_TERM_INFERENCE,
        provider=provider,
        model=model,
        model_revision=model_revision,
        prompt_template_version="candidate-insight-v3",
        schema_version="1",
        pipeline_version="candidate-pipeline-v1",
        required_capabilities=frozenset({"structured_output"}),
        data_residency=data_residency,
        retention_policy=retention_policy,
        provider_retention_days=provider_retention_days,
        provider_training_use_enabled=False,
        max_sensitivity=SensitivityLevel.SENSITIVE,
        output_type=CandidateInsightOutput,
        latency_budget_ms=latency_budget_ms,
        cost_budget=cost_budget,
    )


def _deterministic_candidate(request: ModelProviderRequest) -> object:
    """Create a stable local-only candidate from the first authorized fragment."""

    data = request.untrusted_input.data
    if not isinstance(data, dict):
        raise RuntimeError("deterministic candidate input is unavailable")
    fragments = data.get("fragments")
    if not isinstance(fragments, list) or not fragments:
        raise RuntimeError("deterministic candidate input is unavailable")
    first = fragments[0]
    if not isinstance(first, dict):
        raise RuntimeError("deterministic candidate input is unavailable")
    fragment_id = first.get("source_fragment_id")
    text = first.get("text")
    if not isinstance(fragment_id, str) or not isinstance(text, str) or not text:
        raise RuntimeError("deterministic candidate input is unavailable")
    start = len(text) - len(text.lstrip())
    end = min(len(text), start + 160)
    if end <= start:
        raise RuntimeError("deterministic candidate input is unavailable")
    excerpt = text[start:end]
    return cast(
        JsonValue,
        {
            "kind": "goal",
            "statement": f"你可能想继续推进这件事: {excerpt}",
            "uncertainty": "这只是基于当前一条记录的候选认识; 需要你的确认。",
            "evidence": [
                {
                    "source_fragment_id": fragment_id,
                    "quote_start": start,
                    "quote_end": end,
                }
            ],
        },
    )


__all__ = ["CandidateRuntimeComposition", "build_candidate_runtime"]
