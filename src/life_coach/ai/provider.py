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
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
)

from .contracts import ModelRunSpec, RetentionPolicy, SensitivityLevel, TechnicalId

OutputT = TypeVar("OutputT", bound=BaseModel)

_MAX_REPAIR_ATTEMPTS = 3
_MAX_TECHNICAL_TOKEN_COUNT = 64
_MAX_SOURCE_REF_COUNT = 256
_MAX_AUDIT_MESSAGE_LENGTH = 512
_INVALID_AUDIT_TOKEN = "invalid_token"
_TOOL_DIRECTIVE_KEYS = frozenset({"function_call", "function_calls", "tool_call", "tool_calls"})
_SENSITIVITY_RANK = {
    "normal": 0,
    "sensitive": 1,
    "highly_sensitive": 2,
}

_TECHNICAL_ID_ADAPTER = TypeAdapter(TechnicalId)


class UntrustedModelInput(BaseModel):
    """JSON-compatible user/imported data, never provider instructions.

    The fixed marker makes the trust boundary visible in call recordings and
    provider adapters.  Server-owned task and prompt metadata live on
    :class:`ModelProviderRequest`, not inside this envelope.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    data: JsonValue
    source_refs: tuple[TechnicalId, ...] = Field(default=(), max_length=_MAX_SOURCE_REF_COUNT)
    trust_boundary: Literal["untrusted_data"] = "untrusted_data"

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

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "location",
            tuple(_audit_location_segment(part) for part in self.location),
        )
        object.__setattr__(self, "error_type", _audit_location_segment(self.error_type))
        object.__setattr__(self, "message", _sanitize_audit_message(self.message))


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


@dataclass(frozen=True, slots=True)
class _ValidatedProviderProfile:
    provider_id: str
    capabilities: frozenset[str]
    max_sensitivity: SensitivityLevel
    data_residencies: frozenset[str]
    retention_policies: frozenset[RetentionPolicy]


class ModelGatewayError(RuntimeError):
    """Base class for deterministic gateway failures."""


class GatewayConfigurationError(ModelGatewayError):
    """The gateway or provider registry is internally inconsistent."""


class ModelPolicyViolation(ModelGatewayError):
    """A run would exceed its provider, capability, or sensitivity policy."""


class ProviderExecutionError(ModelGatewayError):
    """A provider failed before returning a contract-valid output."""

    def __init__(self, provider_id: str, attempt: int) -> None:
        self.provider_id = _audit_location_segment(provider_id)
        self.attempt = attempt
        super().__init__(f"provider {self.provider_id!r} failed on attempt {attempt}")


class ToolDirectiveRejected(ModelGatewayError):
    """A provider tried to return a tool/function-call directive."""

    def __init__(self, path: tuple[str | int, ...]) -> None:
        self.path = tuple(_audit_location_segment(part) for part in path)
        rendered = ".".join(str(part) for part in self.path)
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

        profile = _safe_provider_profile(provider)
        provider_id = profile.provider_id
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
                "untrusted input contains unaudited source refs: "
                + _summarize_technical_ids(unaudited_refs)
            )

        provider = self._provider_for(validated_spec)
        repair: RepairContext | None = None
        last_issues: tuple[OutputValidationIssue, ...] = ()
        output_schema = deepcopy(output_type.model_json_schema())
        trace_field_names = _schema_property_names(output_schema)

        for attempt in range(self._max_repair_attempts + 1):
            request = ModelProviderRequest(
                run_spec=validated_spec.model_copy(deep=True),
                untrusted_input=validated_input.model_copy(deep=True),
                output_schema=deepcopy(output_schema),
                attempt=attempt,
                repair=repair,
            )
            provider_failed = False
            raw_output: object = None
            try:
                raw_output = provider.complete(request)
            except Exception:
                # Provider exceptions are untrusted too: they may contain request
                # bodies, credentials, or arbitrary adapter diagnostics.  Raise the
                # stable wrapper outside the active exception handler so neither a
                # cause nor an implicit context retains the raw exception object.
                provider_failed = True
            if provider_failed:
                raise ProviderExecutionError(validated_spec.provider, attempt)

            decode_failed = False
            decode_message = "provider output is not valid structured JSON"
            decoded_output: JsonValue = None
            try:
                decoded_output = _decode_provider_output(
                    raw_output,
                    trace_field_names=trace_field_names,
                )
            except ProviderOutputDecodeError as exc:
                decode_failed = True
                decode_message = str(exc)
            if decode_failed:
                last_issues = (
                    OutputValidationIssue(
                        location=(),
                        error_type="json_type",
                        message=decode_message,
                    ),
                )
                if attempt >= self._max_repair_attempts:
                    raise StructuredOutputValidationError(
                        output_type=output_type,
                        issues=last_issues,
                        attempts=attempt + 1,
                    )
                repair = RepairContext(
                    attempt=attempt + 1,
                    issues=last_issues,
                    previous_output=_json_safe(raw_output),
                )
                continue

            tool_path = _find_tool_directive(
                decoded_output,
                trace_field_names=trace_field_names,
            )
            if tool_path is not None:
                # There is intentionally no execution path and no tool callback.
                raise ToolDirectiveRejected(tool_path)

            validation_failed = False
            try:
                return _validate_output(output_type, decoded_output)
            except ValidationError as exc:
                validation_failed = True
                last_issues = _validation_issues(
                    exc,
                    trace_field_names=trace_field_names,
                )
            if validation_failed:
                if attempt >= self._max_repair_attempts:
                    raise StructuredOutputValidationError(
                        output_type=output_type,
                        issues=last_issues,
                        attempts=attempt + 1,
                    )
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

        profile = _safe_provider_profile(provider)
        if profile.provider_id != provider_id:
            raise GatewayConfigurationError("registered provider_id changed after registration")

        required = {_string_value(value) for value in policy.required_capabilities}
        available = set(profile.capabilities)
        missing = sorted(required - available)
        if missing:
            raise ModelPolicyViolation(
                f"provider {provider_id!r} lacks required capabilities: "
                + _summarize_technical_ids(missing)
            )

        actual_rank = _sensitivity_rank(spec.actual_sensitivity)
        policy_rank = _sensitivity_rank(policy.max_sensitivity)
        provider_rank = _sensitivity_rank(profile.max_sensitivity)
        if actual_rank > policy_rank:
            raise ModelPolicyViolation("run sensitivity exceeds the task policy")
        if actual_rank > provider_rank:
            raise ModelPolicyViolation("run sensitivity exceeds the provider capability")
        if spec.data_residency not in profile.data_residencies:
            raise ModelPolicyViolation("run residency is not supported by the provider")
        if spec.retention_policy not in profile.retention_policies:
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
    validated: ModelRunSpec | None = None
    try:
        payload = _require_json_value(
            spec.model_dump(mode="json", round_trip=True, warnings="none")
        )
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        candidate = ModelRunSpec.model_validate_json(encoded, strict=True)
        _validate_run_spec_technical_metadata(candidate)
        validated = candidate
    except Exception:
        pass
    if validated is None:
        # Raise outside the handler: even ``raise ... from None`` retains the
        # untrusted exception in ``__context__``.
        raise ModelPolicyViolation("model run spec failed boundary revalidation")
    return validated


def _validated_untrusted_input(input_data: UntrustedModelInput) -> UntrustedModelInput:
    validated: UntrustedModelInput | None = None
    try:
        payload = _require_json_value(
            input_data.model_dump(mode="json", round_trip=True, warnings="none")
        )
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        validated = UntrustedModelInput.model_validate_json(encoded, strict=True)
    except Exception:
        pass
    if validated is None:
        raise ModelPolicyViolation("untrusted input failed boundary revalidation")
    return validated


def _decode_provider_output(
    value: object,
    *,
    trace_field_names: frozenset[str],
) -> JsonValue:
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
    return _require_json_value(value, trace_field_names=trace_field_names)


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def _require_json_value(
    value: object,
    path: str = "$",
    *,
    trace_field_names: frozenset[str] | None = None,
) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProviderOutputDecodeError(f"non-finite number at {path}")
        return value
    if isinstance(value, list):
        return [
            _require_json_value(
                nested,
                f"{path}[{index}]",
                trace_field_names=trace_field_names,
            )
            for index, nested in enumerate(value)
        ]
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ProviderOutputDecodeError(f"non-string object key at {path}")
            safe_key = _trace_key(key, trace_field_names)
            result[key] = _require_json_value(
                nested,
                f"{path}.{safe_key}",
                trace_field_names=trace_field_names,
            )
        return result
    type_name = _safe_type_name(value)
    raise ProviderOutputDecodeError(f"non-JSON value at {path}: {type_name}")


def _validation_issues(
    error: ValidationError,
    *,
    trace_field_names: frozenset[str],
) -> tuple[OutputValidationIssue, ...]:
    return tuple(
        OutputValidationIssue(
            location=tuple(
                part if isinstance(part, int) else _trace_key(str(part), trace_field_names)
                for part in item["loc"]
            ),
            error_type="schema_validation",
            message="provider output failed schema validation",
        )
        for item in error.errors(include_url=False, include_context=False, include_input=False)
    )


def _find_tool_directive(
    value: object,
    path: tuple[str | int, ...] = (),
    *,
    trace_field_names: frozenset[str],
) -> tuple[str | int, ...] | None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", round_trip=True)
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().casefold()
            next_path = (*path, _trace_key(str(key), trace_field_names))
            if normalized in _TOOL_DIRECTIVE_KEYS:
                return next_path
            found = _find_tool_directive(
                nested,
                next_path,
                trace_field_names=trace_field_names,
            )
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested in enumerate(value):
            found = _find_tool_directive(
                nested,
                (*path, index),
                trace_field_names=trace_field_names,
            )
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
            type_name = _safe_type_name(value)
            return f"<non-json:{type_name}>"
    if isinstance(value, Mapping):
        return {str(key): _json_safe(nested) for key, nested in value.items()}
    if isinstance(value, Sequence):
        return [_json_safe(nested) for nested in value]
    type_name = _safe_type_name(value)
    return f"<non-json:{type_name}>"


def _string_value(value: object) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _sensitivity_rank(value: object) -> int:
    normalized = _string_value(value).strip().casefold().replace("-", "_")
    rank = _SENSITIVITY_RANK.get(normalized)
    if rank is None:
        raise GatewayConfigurationError("unknown sensitivity level")
    return rank


def _require_technical_id(value: object, field_name: str) -> str:
    """Validate a bounded ASCII identifier without echoing rejected input."""

    try:
        return _TECHNICAL_ID_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        raise ValueError(
            f"{field_name} must be a 1-128 character ASCII technical identifier"
        ) from None


def _normalize_technical_ids(
    values: Iterable[object],
    field_name: str,
    *,
    require_non_empty: bool = False,
    max_count: int = _MAX_TECHNICAL_TOKEN_COUNT,
) -> frozenset[str]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError(f"{field_name} must be a collection of technical identifiers")
    materialized = tuple(values)
    if len(materialized) > max_count:
        raise ValueError(f"{field_name} contains too many technical identifiers")
    if require_non_empty and not materialized:
        raise ValueError(f"{field_name} must not be empty")
    return frozenset(_require_technical_id(value, f"{field_name} item") for value in materialized)


def _normalize_retention_policies(
    values: Iterable[object],
    field_name: str = "retention_policies",
) -> frozenset[RetentionPolicy]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError(f"{field_name} must be a collection of retention policies")
    materialized = tuple(values)
    if not materialized or len(materialized) > len(RetentionPolicy):
        raise ValueError(f"{field_name} has an invalid number of values")
    if any(not isinstance(value, RetentionPolicy) for value in materialized):
        raise TypeError(f"{field_name} must contain RetentionPolicy values")
    return frozenset(value for value in materialized if isinstance(value, RetentionPolicy))


def _validated_provider_profile(provider: ModelProvider) -> _ValidatedProviderProfile:
    provider_id = _require_technical_id(provider.provider_id, "provider_id")
    capabilities = _normalize_technical_ids(provider.capabilities, "capabilities")
    if not isinstance(provider.max_sensitivity, SensitivityLevel):
        raise TypeError("max_sensitivity must be a SensitivityLevel")
    data_residencies = _normalize_technical_ids(
        provider.data_residencies,
        "data_residencies",
        require_non_empty=True,
    )
    retention_policies = _normalize_retention_policies(provider.retention_policies)
    return _ValidatedProviderProfile(
        provider_id=provider_id,
        capabilities=capabilities,
        max_sensitivity=provider.max_sensitivity,
        data_residencies=data_residencies,
        retention_policies=retention_policies,
    )


def _safe_provider_profile(provider: ModelProvider) -> _ValidatedProviderProfile:
    """Validate untrusted adapter metadata without retaining its exceptions."""

    profile: _ValidatedProviderProfile | None = None
    with suppress(Exception):
        profile = _validated_provider_profile(provider)
    if profile is None:
        raise GatewayConfigurationError("provider has invalid technical metadata")
    return profile


def _validate_run_spec_technical_metadata(spec: ModelRunSpec) -> None:
    scalar_fields = (
        (spec.run_id, "run_id"),
        (spec.vault_id, "vault_id"),
        (spec.provider, "provider"),
        (spec.model, "model"),
        (spec.model_revision, "model_revision"),
        (spec.prompt_template_version, "prompt_template_version"),
        (spec.schema_version, "schema_version"),
        (spec.pipeline_version, "pipeline_version"),
        (spec.consent_snapshot_id, "consent_snapshot_id"),
        (spec.data_residency, "data_residency"),
        (spec.policy.task_type, "task_type"),
        (spec.policy.input_schema.name, "input_schema.name"),
        (spec.policy.input_schema.version, "input_schema.version"),
        (spec.policy.output_schema.name, "output_schema.name"),
        (spec.policy.output_schema.version, "output_schema.version"),
    )
    for value, field_name in scalar_fields:
        _require_technical_id(value, field_name)
    _normalize_technical_ids(
        spec.policy.required_capabilities,
        "required_capabilities",
    )
    _normalize_technical_ids(
        spec.policy.allowed_providers,
        "allowed_providers",
        require_non_empty=True,
    )
    _normalize_technical_ids(
        spec.policy.data_residency,
        "policy.data_residency",
        require_non_empty=True,
    )
    if len(spec.input_refs) > _MAX_SOURCE_REF_COUNT:
        raise ValueError("input_refs contains too many references")
    for ref in spec.input_refs:
        _require_technical_id(ref.vault_id, "input_ref.vault_id")
        _require_technical_id(ref.object_id, "input_ref.object_id")


def _schema_property_names(schema: Mapping[str, object]) -> frozenset[str]:
    """Collect only bounded ASCII field names from a server-owned JSON schema."""

    names: set[str] = set()
    pending: list[object] = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, Mapping):
            properties = value.get("properties")
            if isinstance(properties, Mapping):
                for key in properties:
                    if isinstance(key, str):
                        with suppress(ValueError):
                            names.add(_require_technical_id(key, "schema property"))
            pending.extend(value.values())
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            pending.extend(value)
    return frozenset(names)


def _trace_key(value: str, trusted_field_names: frozenset[str] | None) -> str | int:
    if trusted_field_names is None:
        return _audit_location_segment(value)
    normalized = value.strip().casefold()
    if normalized in _TOOL_DIRECTIVE_KEYS:
        return normalized
    if value in trusted_field_names:
        return value
    return _INVALID_AUDIT_TOKEN


def _audit_location_segment(value: str | int) -> str | int:
    if isinstance(value, int):
        return value
    try:
        return _require_technical_id(value, "audit location")
    except ValueError:
        return _INVALID_AUDIT_TOKEN


def _sanitize_audit_message(value: str) -> str:
    single_line = "".join(
        character if 0x20 <= ord(character) <= 0x7E else "?" for character in value
    )
    return single_line[:_MAX_AUDIT_MESSAGE_LENGTH]


def _safe_type_name(value: object) -> str:
    candidate = f"{type(value).__module__}.{type(value).__qualname__}"
    try:
        return _require_technical_id(candidate, "type name")
    except ValueError:
        return "non_json_object"


def _summarize_technical_ids(values: Sequence[str], *, limit: int = 8) -> str:
    visible = list(values[:limit])
    summary = ", ".join(visible)
    if len(values) > limit:
        summary += f", and {len(values) - limit} more"
    return summary
