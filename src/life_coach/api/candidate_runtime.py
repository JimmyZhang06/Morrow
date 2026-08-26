"""Production composition for the single evidence-backed candidate task."""
# ruff: noqa: RUF001

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
from life_coach.application.action_generation import (
    ACTION_PROMPT_TEMPLATE_VERSION,
    REVERSIBLE_ACTION_TASK_TYPE,
    ActionMemoryContextAuthority,
    ReversibleActionOutput,
    ReversibleActionPersister,
)
from life_coach.application.candidate_insight import (
    CANDIDATE_INSIGHT_TASK_TYPE,
    CandidateInsightOutput,
    CandidateInsightPersister,
)
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
    RoutingModelResultPersister,
)
from life_coach.application.narrative_generation import (
    LIFE_LINE_TASK_TYPE,
    MEMOIR_CHAPTER_TASK_TYPE,
    NARRATIVE_PROMPT_TEMPLATE_VERSION,
    LifeLineOutput,
    MemoirChapterOutput,
    NarrativeContextAuthority,
    NarrativeResultPersister,
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

    runtime: GovernedModelRuntime
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
            response_factory=_deterministic_response,
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
        proxy_url = (
            settings.stepfun_proxy_url.get_secret_value()
            if settings.stepfun_proxy_url is not None
            else None
        )
        owned_client = httpx.Client(
            follow_redirects=False,
            trust_env=False,
            proxy=proxy_url,
        )
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
    narrative_authority = NarrativeContextAuthority()
    gateway = GovernedModelGateway(
        gateway=ModelGateway((provider,)),
        source_authority=source_authority,
        tasks=(
            task,
            _action_task_from(task),
            _narrative_task_from(task, LIFE_LINE_TASK_TYPE, LifeLineOutput, narrative_authority),
            _narrative_task_from(
                task,
                MEMOIR_CHAPTER_TASK_TYPE,
                MemoirChapterOutput,
                narrative_authority,
            ),
        ),
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
        result_persister=RoutingModelResultPersister(
            {
                CANDIDATE_INSIGHT_TASK_TYPE: persister,
                REVERSIBLE_ACTION_TASK_TYPE: ReversibleActionPersister(),
                LIFE_LINE_TASK_TYPE: NarrativeResultPersister(narrative_authority),
                MEMOIR_CHAPTER_TASK_TYPE: NarrativeResultPersister(narrative_authority),
            }
        ),
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


def _action_task_from(candidate: ModelTaskDefinition) -> ModelTaskDefinition:
    return ModelTaskDefinition(
        task_type=REVERSIBLE_ACTION_TASK_TYPE,
        consent_purpose=ConsentPurpose.PASSIVE_QA,
        provider=candidate.provider,
        model=candidate.model,
        model_revision=candidate.model_revision,
        prompt_template_version=ACTION_PROMPT_TEMPLATE_VERSION,
        schema_version="1",
        pipeline_version="action-pipeline-v1",
        required_capabilities=frozenset({"structured_output"}),
        data_residency=candidate.data_residency,
        retention_policy=candidate.retention_policy,
        provider_retention_days=candidate.provider_retention_days,
        provider_training_use_enabled=False,
        max_sensitivity=SensitivityLevel.SENSITIVE,
        output_type=ReversibleActionOutput,
        latency_budget_ms=candidate.latency_budget_ms,
        cost_budget=candidate.cost_budget,
        context_authority=ActionMemoryContextAuthority(),
    )


def _narrative_task_from(
    candidate: ModelTaskDefinition,
    task_type: str,
    output_type: type[LifeLineOutput] | type[MemoirChapterOutput],
    authority: NarrativeContextAuthority,
) -> ModelTaskDefinition:
    return ModelTaskDefinition(
        task_type=task_type,
        consent_purpose=ConsentPurpose.NARRATIVE,
        provider=candidate.provider,
        model=candidate.model,
        model_revision=candidate.model_revision,
        prompt_template_version=NARRATIVE_PROMPT_TEMPLATE_VERSION,
        schema_version="1",
        pipeline_version="narrative-pipeline-v1",
        required_capabilities=frozenset({"structured_output"}),
        data_residency=candidate.data_residency,
        retention_policy=candidate.retention_policy,
        provider_retention_days=candidate.provider_retention_days,
        provider_training_use_enabled=False,
        max_sensitivity=SensitivityLevel.SENSITIVE,
        output_type=output_type,
        latency_budget_ms=candidate.latency_budget_ms,
        cost_budget=candidate.cost_budget,
        context_authority=authority,
    )


def _deterministic_response(request: ModelProviderRequest) -> object:
    if request.run_spec.policy.task_type == REVERSIBLE_ACTION_TASK_TYPE:
        return _deterministic_action(request)
    if request.run_spec.policy.task_type == LIFE_LINE_TASK_TYPE:
        return _deterministic_life_line(request)
    if request.run_spec.policy.task_type == MEMOIR_CHAPTER_TASK_TYPE:
        return _deterministic_memoir(request)
    return _deterministic_candidate(request)


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


def _deterministic_action(request: ModelProviderRequest) -> object:
    data = request.untrusted_input.data
    if not isinstance(data, dict):
        raise RuntimeError("deterministic action input is unavailable")
    context = data.get("context")
    if not isinstance(context, dict):
        raise RuntimeError("deterministic action input is unavailable")
    statement = context.get("memory_statement")
    if not isinstance(statement, str) or not statement.strip():
        raise RuntimeError("deterministic action input is unavailable")
    return cast(
        JsonValue,
        {
            "title": "找一个最小的现实例子",
            "description": (
                f"用 8 分钟写下一个与“{statement[:80]}”有关的具体情境，"
                "并标记它更支持还是更反驳这条认识。"
            ),
            "rationale": (
                "把已经认可的理解放回一个具体情境中检验，"
                "而不是把它当成固定结论。"
            ),
            "exit_plan": (
                "随时停下并删除草稿；不联系他人、不花钱，"
                "也不创建外部安排。"
            ),
            "estimated_minutes": 8,
        },
    )


def _deterministic_life_line(request: ModelProviderRequest) -> object:
    materials = _deterministic_narrative_materials(request)
    return cast(
        JsonValue,
        {
            "overview": "这些记录呈现出一种反复把复杂事情缩小、再开始行动的可能倾向。",
            "themes": [
                {
                    "title": "先缩小，再开始",
                    "interpretation": "你可能更容易在问题被缩成一个可见的小步骤后开始行动。",
                    "supporting_ordinals": [materials[0]["ordinal"]],
                    "counterexample_ordinals": [],
                    "counterpoint": "目前材料还不能说明这种方式适用于所有情境。",
                    "uncovered_period": "没有记录覆盖的时期保持空白，不据此推断。",
                }
            ],
        },
    )


def _deterministic_memoir(request: ModelProviderRequest) -> object:
    materials = _deterministic_narrative_materials(request)
    statement = materials[0]["statement"]
    return cast(
        JsonValue,
        {
            "title": "从一个小步骤开始",
            "body": (
                f"这段时期里，你留下过这样的理解：“{statement}”。它不是对整个人生的总结，"
                "但像一个可以核对的路标：当事情显得复杂时，把它缩小成一个看得见的动作，"
                "可能让开始变得容易一些。现有材料只覆盖了少数时刻，因此这里保留空白，"
                "不替没有记录的日子补写原因或结论。"
            ),
            "uncertainty": "这一章只依据当前已确认材料，时间空白与相反经历仍有待补充。",
            "citation_ordinals": [materials[0]["ordinal"]],
        },
    )


def _deterministic_narrative_materials(request: ModelProviderRequest) -> list[dict[str, object]]:
    data = request.untrusted_input.data
    if not isinstance(data, dict):
        raise RuntimeError("deterministic narrative input is unavailable")
    context = data.get("context")
    if not isinstance(context, dict):
        raise RuntimeError("deterministic narrative input is unavailable")
    materials = context.get("materials")
    if not isinstance(materials, list) or not materials or not isinstance(materials[0], dict):
        raise RuntimeError("deterministic narrative input is unavailable")
    return cast(list[dict[str, object]], materials)


__all__ = ["CandidateRuntimeComposition", "build_candidate_runtime"]
