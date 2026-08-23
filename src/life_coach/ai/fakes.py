"""Deterministic, no-network model provider for pipeline tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass

from .contracts import RetentionPolicy, SensitivityLevel
from .provider import ModelProviderRequest


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


class DeterministicFakeProvider:
    """A FIFO scripted provider that performs no I/O.

    Responses and recorded requests are deep-copied at every boundary.  Mutating
    a fixture after enqueueing it, or mutating a returned response/call snapshot,
    therefore cannot change later deterministic behavior.
    """

    def __init__(
        self,
        responses: Iterable[object | Exception] = (),
        *,
        provider_id: str = "fake",
        capabilities: Iterable[str] = ("structured_output",),
        max_sensitivity: SensitivityLevel = SensitivityLevel.HIGHLY_SENSITIVE,
        data_residencies: Iterable[str] = ("local",),
        retention_policies: Iterable[RetentionPolicy] = tuple(RetentionPolicy),
    ) -> None:
        normalized_id = provider_id.strip()
        if not normalized_id:
            raise ValueError("provider_id must not be empty")
        self.provider_id = normalized_id
        self.capabilities = frozenset(capabilities)
        self.max_sensitivity = max_sensitivity
        self.data_residencies = frozenset(data_residencies)
        self.retention_policies = frozenset(retention_policies)
        self._script: deque[_ScriptEntry] = deque()
        self._calls: list[ModelProviderRequest] = []
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
    def calls(self) -> tuple[ModelProviderRequest, ...]:
        """Return immutable snapshots rather than the mutable internal log."""

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
        self._calls.append(deepcopy(request))
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
