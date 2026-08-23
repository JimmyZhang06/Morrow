"""Provider-neutral model gateway with an explicit untrusted-data boundary.

The gateway deliberately exposes no tool registry or tool execution callback.  A
provider can only return a value which is validated as the requested Pydantic
contract.  This keeps model selection and structured-output repair separate from
application side effects.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError, field_validator

from .contracts import ModelRunSpec, RetentionPolicy, SensitivityLevel

OutputT = TypeVar("OutputT", bound=BaseModel)

_MAX_REPAIR_ATTEMPTS = 3
_TOOL_DIRECTIVE_KEYS = frozenset({"function_call", "function_calls", "tool_call", "tool_calls"})
_SENSITIVITY_RANK = {
    "normal": 0,
    "sensitive": 1,
    "highly_sensitive": 2,
}


class UntrustedModelInput(BaseModel):
    """JSON-compatible user/imported data, never provider instructions.

    The fixed marker makes the trust boundary visible in call recordings and
    provider adapters.  Server-owned task and prompt metadata live on
    :class:`ModelProviderRequest`, not inside this envelope.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    data: JsonValue
    source_refs: tuple[str, ...] = ()
    trust_boundary: Literal["untrusted_data"] = "untrusted_data"

    @field_validator("source_refs")
    @classmethod
    def source_refs_are_non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("source refs must not be blank")
        return value

    @classmethod
    def from_text(cls, text: str, *, source_refs: Iterable[str] = ()) -> UntrustedModelInput:
        """Wrap source text without interpreting or rewriting it."""

        return cls(data=text, source_refs=tuple(source_refs))


@dataclass(frozen=True, slots=True)
class OutputValidationIssue:
    """Sanitized validation feedback suitable for one repair request."""

    location: tuple[str | int, ...]
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class RepairContext:
    """A bounded structured-output repair request.

    ``previous_output`` remains untrusted model data.  Validation feedback omits
    raw input values so a provider adapter does not accidentally promote them to
    trusted instructions.
    """

    attempt: int
    issues: tuple[OutputValidationIssue, ...]
    previous_output: JsonValue


@dataclass(frozen=True, slots=True)
class ModelProviderRequest:
    """Provider-neutral request; intentionally contains no tools field."""

    run_spec: ModelRunSpec
    untrusted_input: UntrustedModelInput
    output_schema: dict[str, Any]
    attempt: int = 0
    repair: RepairContext | None = None


@runtime_checkable
class ModelProvider(Protocol):
    """Small synchronous provider port used by the pure model pipeline."""

    provider_id: str
    capabilities: frozenset[str]
    max_sensitivity: SensitivityLevel
    data_residencies: frozenset[str]
    retention_policies: frozenset[RetentionPolicy]

    def complete(self, request: ModelProviderRequest) -> object:
        """Return structured data (or JSON); never execute application tools."""


class ModelGatewayError(RuntimeError):
    """Base class for deterministic gateway failures."""


class GatewayConfigurationError(ModelGatewayError):
    """The gateway or provider registry is internally inconsistent."""


class ModelPolicyViolation(ModelGatewayError):
    """A run would exceed its provider, capability, or sensitivity policy."""


class ProviderExecutionError(ModelGatewayError):
    """A provider failed before returning a contract-valid output."""

    def __init__(self, provider_id: str, attempt: int) -> None:
        self.provider_id = provider_id
        self.attempt = attempt
        super().__init__(f"provider {provider_id!r} failed on attempt {attempt}")


class ToolDirectiveRejected(ModelGatewayError):
    """A provider tried to return a tool/function-call directive."""

    def __init__(self, path: tuple[str | int, ...]) -> None:
        self.path = path
        rendered = ".".join(str(part) for part in path)
        super().__init__(f"model tool directives are not accepted (at {rendered})")


class StructuredOutputValidationError(ModelGatewayError):
    """All bounded attempts failed strict Pydantic output validation."""

    def __init__(
        self,
        *,
        output_type: type[BaseModel],
        issues: Sequence[OutputValidationIssue],
        attempts: int,
    ) -> None:
        self.output_type = output_type
        self.issues = tuple(issues)
        self.attempts = attempts
        super().__init__(
            f"provider output did not satisfy {output_type.__name__} after {attempts} attempt(s)"
        )


class ProviderOutputDecodeError(ModelGatewayError):
    """A provider returned a value outside the JSON structured-output boundary."""


class ModelGateway:
    """Validate a run policy, invoke one provider, and parse strict output."""

    def __init__(
        self,
        providers: Iterable[ModelProvider] = (),
        *,
        max_repair_attempts: int = 1,
    ) -> None:
        if not 0 <= max_repair_attempts <= _MAX_REPAIR_ATTEMPTS:
            raise ValueError(f"max_repair_attempts must be between 0 and {_MAX_REPAIR_ATTEMPTS}")
        self._max_repair_attempts = max_repair_attempts
        self._providers: dict[str, ModelProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: ModelProvider) -> None:
        """Register a provider under its auditable provider id."""

        provider_id = provider.provider_id.strip()
        if not provider_id:
            raise GatewayConfigurationError("provider_id must not be empty")
        if provider_id in self._providers:
            raise GatewayConfigurationError(f"duplicate provider_id: {provider_id!r}")
        self._providers[provider_id] = provider

    def run(
        self,
        spec: ModelRunSpec,
        input_data: UntrustedModelInput,
        output_type: type[OutputT],
    ) -> OutputT:
        """Run one audited spec and return only a validated Pydantic value.

        Raw strings/mappings are intentionally not accepted as ``input_data``;
        callers must explicitly mark source/import content as untrusted.
        """

        if not isinstance(input_data, UntrustedModelInput):
            raise TypeError("input_data must be an UntrustedModelInput")
        if not isinstance(output_type, type) or not issubclass(output_type, BaseModel):
            raise TypeError("output_type must be a Pydantic BaseModel class")

        validated_spec = _validated_run_spec(spec)
        validated_input = _validated_untrusted_input(input_data)
        self._validate_schema_binding(validated_spec, output_type)
        audited_refs = {ref.object_id for ref in validated_spec.input_refs}
        unaudited_refs = sorted(set(validated_input.source_refs) - audited_refs)
        if unaudited_refs:
            raise ModelPolicyViolation(
                "untrusted input contains unaudited source refs: " + ", ".join(unaudited_refs)
            )

        provider = self._provider_for(validated_spec)
        repair: RepairContext | None = None
        last_issues: tuple[OutputValidationIssue, ...] = ()
        output_schema = deepcopy(output_type.model_json_schema())

        for attempt in range(self._max_repair_attempts + 1):
            request = ModelProviderRequest(
                run_spec=validated_spec.model_copy(deep=True),
                untrusted_input=validated_input.model_copy(deep=True),
                output_schema=deepcopy(output_schema),
                attempt=attempt,
                repair=repair,
            )
            try:
                raw_output = provider.complete(request)
            except Exception as exc:
                raise ProviderExecutionError(provider.provider_id, attempt) from exc

            try:
                decoded_output = _decode_provider_output(raw_output)
            except ProviderOutputDecodeError as exc:
                last_issues = (
                    OutputValidationIssue(
                        location=(),
                        error_type="json_type",
                        message=str(exc),
                    ),
                )
                if attempt >= self._max_repair_attempts:
                    raise StructuredOutputValidationError(
                        output_type=output_type,
                        issues=last_issues,
                        attempts=attempt + 1,
                    ) from exc
                repair = RepairContext(
                    attempt=attempt + 1,
                    issues=last_issues,
                    previous_output=_json_safe(raw_output),
                )
                continue

            tool_path = _find_tool_directive(decoded_output)
            if tool_path is not None:
                # There is intentionally no execution path and no tool callback.
                raise ToolDirectiveRejected(tool_path)

            try:
                return _validate_output(output_type, decoded_output)
            except ValidationError as exc:
                last_issues = _validation_issues(exc)
                if attempt >= self._max_repair_attempts:
                    raise StructuredOutputValidationError(
                        output_type=output_type,
                        issues=last_issues,
                        attempts=attempt + 1,
                    ) from exc
                repair = RepairContext(
                    attempt=attempt + 1,
                    issues=last_issues,
                    previous_output=decoded_output,
                )

        # The loop bounds make this unreachable and keep type checkers honest.
        raise StructuredOutputValidationError(
            output_type=output_type,
            issues=last_issues,
            attempts=self._max_repair_attempts + 1,
        )

    @staticmethod
    def _validate_schema_binding(
        spec: ModelRunSpec,
        output_type: type[BaseModel],
    ) -> None:
        policy = spec.policy
        input_schema = UntrustedModelInput.model_json_schema()
        output_schema = output_type.model_json_schema()
        if policy.input_schema.json_schema is None:
            if policy.input_schema.name != UntrustedModelInput.__name__:
                raise ModelPolicyViolation("task input schema is not UntrustedModelInput")
        elif policy.input_schema.json_schema != input_schema:
            raise ModelPolicyViolation("task input JSON schema differs from the gateway contract")
        if policy.output_schema.json_schema is None:
            if policy.output_schema.name != output_type.__name__:
                raise ModelPolicyViolation("requested output type differs from the audited schema")
        elif policy.output_schema.json_schema != output_schema:
            raise ModelPolicyViolation(
                "requested output JSON schema differs from the audited schema"
            )
        if output_type.model_config.get("extra") != "forbid":
            raise GatewayConfigurationError("structured output models must set extra='forbid'")

    def _provider_for(self, spec: ModelRunSpec) -> ModelProvider:
        policy = spec.policy
        provider_id = spec.provider
        allowed_providers = {_string_value(value) for value in policy.allowed_providers}
        if provider_id not in allowed_providers:
            raise ModelPolicyViolation(f"provider {provider_id!r} is not allowed for this task")

        provider = self._providers.get(provider_id)
        if provider is None:
            raise ModelPolicyViolation(f"provider {provider_id!r} is not registered")

        required = {_string_value(value) for value in policy.required_capabilities}
        available = {_string_value(value) for value in provider.capabilities}
        missing = sorted(required - available)
        if missing:
            raise ModelPolicyViolation(
                f"provider {provider_id!r} lacks required capabilities: {', '.join(missing)}"
            )

        actual_rank = _sensitivity_rank(spec.actual_sensitivity)
        policy_rank = _sensitivity_rank(policy.max_sensitivity)
        provider_rank = _sensitivity_rank(provider.max_sensitivity)
        if actual_rank > policy_rank:
            raise ModelPolicyViolation("run sensitivity exceeds the task policy")
        if actual_rank > provider_rank:
            raise ModelPolicyViolation("run sensitivity exceeds the provider capability")
        try:
            provider_residencies = provider.data_residencies
            provider_retention = provider.retention_policies
        except AttributeError as exc:
            raise GatewayConfigurationError(
                f"provider {provider_id!r} is missing its data-handling profile"
            ) from exc
        if spec.data_residency not in provider_residencies:
            raise ModelPolicyViolation("run residency is not supported by the provider")
        if spec.retention_policy not in provider_retention:
            raise ModelPolicyViolation("run retention is not supported by the provider")
        return provider


def _validate_output[OutputT: BaseModel](
    output_type: type[OutputT],
    decoded_output: JsonValue,
) -> OutputT:
    encoded = json.dumps(
        decoded_output,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return output_type.model_validate_json(encoded, strict=True)


def _validated_run_spec(spec: ModelRunSpec) -> ModelRunSpec:
    try:
        payload = _require_json_value(
            spec.model_dump(mode="json", round_trip=True, warnings="none")
        )
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        return ModelRunSpec.model_validate_json(encoded, strict=True)
    except (ProviderOutputDecodeError, ValidationError, ValueError) as exc:
        raise ModelPolicyViolation("model run spec failed boundary revalidation") from exc


def _validated_untrusted_input(input_data: UntrustedModelInput) -> UntrustedModelInput:
    try:
        payload = _require_json_value(
            input_data.model_dump(mode="json", round_trip=True, warnings="none")
        )
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        return UntrustedModelInput.model_validate_json(encoded, strict=True)
    except (ProviderOutputDecodeError, ValidationError, ValueError) as exc:
        raise ModelPolicyViolation("untrusted input failed boundary revalidation") from exc


def _decode_provider_output(value: object) -> JsonValue:
    if isinstance(value, BaseModel):
        try:
            value = value.model_dump(mode="json", round_trip=True, warnings="none")
        except (TypeError, ValueError) as exc:
            raise ProviderOutputDecodeError(
                "provider model output cannot be serialized as JSON"
            ) from exc
    if isinstance(value, (str, bytes, bytearray)):
        try:
            text = (
                bytes(value).decode("utf-8", errors="strict")
                if isinstance(value, (bytes, bytearray))
                else value
            )
            value = json.loads(text, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ProviderOutputDecodeError("provider output is not valid JSON") from exc
    return _require_json_value(value)


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def _require_json_value(value: object, path: str = "$") -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProviderOutputDecodeError(f"non-finite number at {path}")
        return value
    if isinstance(value, list):
        return [
            _require_json_value(nested, f"{path}[{index}]") for index, nested in enumerate(value)
        ]
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ProviderOutputDecodeError(f"non-string object key at {path}")
            result[key] = _require_json_value(nested, f"{path}.{key}")
        return result
    type_name = f"{type(value).__module__}.{type(value).__qualname__}"
    raise ProviderOutputDecodeError(f"non-JSON value at {path}: {type_name}")


def _validation_issues(error: ValidationError) -> tuple[OutputValidationIssue, ...]:
    return tuple(
        OutputValidationIssue(
            location=tuple(item["loc"]),
            error_type=item["type"],
            message=item["msg"],
        )
        for item in error.errors(include_url=False, include_context=False, include_input=False)
    )


def _find_tool_directive(
    value: object, path: tuple[str | int, ...] = ()
) -> tuple[str | int, ...] | None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", round_trip=True)
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().casefold()
            next_path = (*path, str(key))
            if normalized in _TOOL_DIRECTIVE_KEYS:
                return next_path
            found = _find_tool_directive(nested, next_path)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested in enumerate(value):
            found = _find_tool_directive(nested, (*path, index))
            if found is not None:
                return found
    return None


def _json_safe(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "<non-finite-number>"
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, BaseModel):
        try:
            return _json_safe(value.model_dump(mode="python", round_trip=True, warnings="none"))
        except (TypeError, ValueError):
            type_name = f"{type(value).__module__}.{type(value).__qualname__}"
            return f"<non-json:{type_name}>"
    if isinstance(value, Mapping):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, Sequence):
        return [_json_safe(nested) for nested in value]
    type_name = f"{type(value).__module__}.{type(value).__qualname__}"
    return f"<non-json:{type_name}>"


def _string_value(value: object) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _sensitivity_rank(value: object) -> int:
    normalized = _string_value(value).strip().casefold().replace("-", "_")
    try:
        return _SENSITIVITY_RANK[normalized]
    except KeyError as exc:
        raise GatewayConfigurationError(f"unknown sensitivity level: {normalized!r}") from exc
