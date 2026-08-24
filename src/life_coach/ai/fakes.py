"""Deterministic, no-network model provider for pipeline tests."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass

from .contracts import ModelRunSpec, RetentionPolicy, SensitivityLevel
from .provider import (
    ModelProviderRequest,
    OutputValidationIssue,
    _normalize_retention_policies,
    _normalize_technical_ids,
    _require_technical_id,
)


class FakeProviderScriptExhausted(RuntimeError):
    """The fake received more calls than its deterministic script contains."""


@dataclass(frozen=True, slots=True)
class _QueuedOutput:
    value: object
    intentionally_invalid: bool = False


@dataclass(frozen=True, slots=True)
class _QueuedError:
    error: Exception


_ScriptEntry = _QueuedOutput | _QueuedError


@dataclass(frozen=True, slots=True)
class FakeRepairAudit:
    """Trace-safe repair metadata retained by the deterministic fake."""

    attempt: int
    issues: tuple[OutputValidationIssue, ...]
    previous_output_sha256: str


@dataclass(frozen=True, slots=True)
class FakeProviderCall:
    """Minimal call audit which never retains raw imported or model-authored text."""

    run_spec: ModelRunSpec
    source_refs: tuple[str, ...]
    input_sha256: str
    output_schema_sha256: str
    attempt: int
    repair: FakeRepairAudit | None = None


class DeterministicFakeProvider:
    """A FIFO scripted provider that performs no I/O.

    Responses are deep-copied at every boundary.  Calls retain only the audited
    run spec, source references, stable content fingerprints, and sanitized repair
    metadata; raw untrusted input and previous model output are never logged.
    """

    def __init__(
        self,
        responses: Iterable[object | Exception] = (),
        *,
        response_factory: Callable[[ModelProviderRequest], object] | None = None,
        provider_id: str = "fake",
        capabilities: Iterable[str] = ("structured_output",),
        max_sensitivity: SensitivityLevel = SensitivityLevel.HIGHLY_SENSITIVE,
        data_residencies: Iterable[str] = ("local",),
        retention_policies: Iterable[RetentionPolicy] = tuple(RetentionPolicy),
    ) -> None:
        self.provider_id = _require_technical_id(provider_id, "provider_id")
        self.capabilities = _normalize_technical_ids(capabilities, "capabilities")
        if not isinstance(max_sensitivity, SensitivityLevel):
            raise TypeError("max_sensitivity must be a SensitivityLevel")
        self.max_sensitivity = max_sensitivity
        self.data_residencies = _normalize_technical_ids(
            data_residencies,
            "data_residencies",
            require_non_empty=True,
        )
        self.retention_policies = _normalize_retention_policies(retention_policies)
        if response_factory is not None and not callable(response_factory):
            raise TypeError("response_factory must be callable")
        self._response_factory = response_factory
        self._script: deque[_ScriptEntry] = deque()
        self._calls: list[FakeProviderCall] = []
        for item in responses:
            if isinstance(item, Exception):
                self.enqueue_error(item)
            else:
                self.enqueue_response(item)

    @property
    def name(self) -> str:
        """Compatibility alias; policies and audit records use ``provider_id``."""

        return self.provider_id

    @property
    def calls(self) -> tuple[FakeProviderCall, ...]:
        """Return immutable, trace-safe snapshots of the minimal call audit."""

        return tuple(deepcopy(self._calls))

    @property
    def call_count(self) -> int:
        return len(self._calls)

    @property
    def remaining(self) -> int:
        return len(self._script)

    @property
    def script_kinds(self) -> tuple[str, ...]:
        """Expose queue shape for assertions without exposing queued contents."""

        return tuple(
            "error"
            if isinstance(entry, _QueuedError)
            else "invalid"
            if entry.intentionally_invalid
            else "response"
            for entry in self._script
        )

    def enqueue_response(self, response: object) -> DeterministicFakeProvider:
        """Append a successful scripted response and return ``self`` for setup."""

        self._script.append(_QueuedOutput(deepcopy(response)))
        return self

    def enqueue_invalid(self, response: object) -> DeterministicFakeProvider:
        """Append output intended to exercise gateway validation/repair."""

        self._script.append(_QueuedOutput(deepcopy(response), intentionally_invalid=True))
        return self

    def enqueue_error(self, error: Exception) -> DeterministicFakeProvider:
        """Append a provider failure; it is raised only when its turn is reached."""

        if not isinstance(error, Exception):
            raise TypeError("error must be an Exception instance")
        self._script.append(_QueuedError(_clone_exception(error)))
        return self

    # Short aliases make table-driven tests readable while preserving explicit
    # enqueue_* names for discoverability.
    queue_response = enqueue_response
    queue_invalid = enqueue_invalid
    queue_error = enqueue_error

    def clear_calls(self) -> None:
        self._calls.clear()

    def complete(self, request: ModelProviderRequest) -> object:
        """Consume exactly one scripted entry, recording the attempted call."""

        if not isinstance(request, ModelProviderRequest):
            raise TypeError("request must be a ModelProviderRequest")
        self._calls.append(_audit_call(request))
        if not self._script and self._response_factory is not None:
            return deepcopy(self._response_factory(deepcopy(request)))
        if not self._script:
            raise FakeProviderScriptExhausted(
                f"fake provider {self.provider_id!r} has no scripted response remaining"
            )

        entry = self._script.popleft()
        if isinstance(entry, _QueuedError):
            raise _clone_exception(entry.error)
        return deepcopy(entry.value)


# A concise alias for callers that do not need the determinism qualifier.
FakeModelProvider = DeterministicFakeProvider


def _audit_call(request: ModelProviderRequest) -> FakeProviderCall:
    repair = request.repair
    repair_audit = (
        None
        if repair is None
        else FakeRepairAudit(
            attempt=repair.attempt,
            issues=deepcopy(repair.issues),
            previous_output_sha256=_json_sha256(repair.previous_output),
        )
    )
    return FakeProviderCall(
        run_spec=request.run_spec.model_copy(deep=True),
        source_refs=tuple(request.untrusted_input.source_refs),
        input_sha256=_json_sha256(request.untrusted_input.data),
        output_schema_sha256=_json_sha256(request.output_schema),
        attempt=request.attempt,
        repair=repair_audit,
    )


def _json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clone_exception(error: Exception) -> Exception:
    try:
        cloned = deepcopy(error)
    except Exception:
        try:
            cloned = type(error)(*deepcopy(error.args))
        except Exception:
            cloned = RuntimeError(str(error))
    if not isinstance(cloned, Exception):
        return RuntimeError(str(error))
    return cloned
