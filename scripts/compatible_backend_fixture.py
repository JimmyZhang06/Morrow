"""TEST ONLY: real desktop database/API with a deterministic HTTP transport fixture."""

import json
import runpy
from pathlib import Path
from typing import Any

import httpx

_original_init = httpx.Client.__init__


def respond(request: httpx.Request) -> httpx.Response:
    assert str(request.url) == "https://model-fixture.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer synthetic-compatible-test"
    body = json.loads(request.content)
    assert body["model"] == "vendor/model:latest"
    envelope = json.loads(body["messages"][1]["content"].removeprefix("USER_DATA="))
    if envelope["task_type"] == "candidate_insight":
        fragment = envelope["data"]["fragments"][0]
        result = {
            "kind": "preference", "statement": "你可能喜欢提前写下要点再表达。",
            "uncertainty": "这只是一条记录带来的猜测; 需要你确认。",
            "evidence": [{"source_fragment_id": fragment["source_fragment_id"],
                          "quote_start": 0, "quote_end": min(len(fragment["text"]), 30)}],
        }
    else:
        assert envelope["task_type"] == "reversible_action"
        result = {
            "title": "写下一次表达的要点", "description": "用五分钟写下想说的三句话。",
            "rationale": "观察提前整理能否让表达更轻松。", "exit_plan": "随时停止并删除草稿。",
            "estimated_minutes": 5,
        }
    return httpx.Response(200, json={"choices": [{"message": {
        "content": json.dumps(result, ensure_ascii=False),
    }}]})


def initialize(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
    kwargs["transport"] = httpx.MockTransport(respond)
    _original_init(self, *args, **kwargs)


if __name__ == "__main__":
    httpx.Client.__init__ = initialize  # type: ignore[method-assign]
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/desktop_backend.py"),
                   run_name="__main__")
