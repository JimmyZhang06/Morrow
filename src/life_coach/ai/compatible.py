"""User-configured desktop Chat Completions transport, with no residency promises."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from life_coach.ai.provider import ModelProviderRequest
from life_coach.ai.stepfun import StepFunChatCompletionsProvider

COMPATIBLE_PROVIDER_ID = "desktop-compatible"


def normalize_base_url(value: str) -> str:
    """Accept HTTPS API roots only; never credentials or redirect destinations."""
    parsed = urlsplit(value)
    if (
        len(value) > 2048
        or any(char.isspace() or ord(char) < 32 for char in value)
        or "\\" in value
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "?" in value
        or "#" in value
        or parsed.path.rstrip("/").endswith("/chat/completions")
    ):
        raise ValueError("configure an HTTPS API base URL without credentials or query")
    _ = parsed.port
    return value.rstrip("/")


class CompatibleChatCompletionsProvider(StepFunChatCompletionsProvider):
    """Reuse bounded parsing, evidence prompts and safe transport failure handling."""

    provider_id = COMPATIBLE_PROVIDER_ID
    data_residencies = frozenset({"unspecified"})
    __slots__ = ("_json_mode",)

    def __init__(
        self, *, api_key: SecretStr, client: httpx.Client, model: str,
        base_url: str, json_mode: bool = False,
    ) -> None:
        # The dedicated Step Plan adapter keeps its original strict allowlist.
        super().__init__(api_key=api_key, client=client, model=model, timeout_seconds=60)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model):
            raise ValueError("invalid model identifier")
        if any(char.isspace() for char in api_key.get_secret_value()):
            raise ValueError("invalid API credential")
        self._base_url = normalize_base_url(base_url)
        self._json_mode = json_mode

    @property
    def revision(self) -> str:
        """Bind receipts to destination and wire options without storing the URL."""
        value = f"{self._base_url}|{self._json_mode}"
        return "compat-v1-" + hashlib.sha256(value.encode()).hexdigest()[:32]

    def _request_body(self, request: ModelProviderRequest) -> dict[str, object]:
        body: dict[str, object] = {
            "model": self._model, "messages": self._messages(request), "stream": False,
        }
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        return body
