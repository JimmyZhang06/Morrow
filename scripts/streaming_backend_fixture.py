# ruff: noqa: RUF001
"""TEST ONLY: paced SSE response over synthetic desktop model transport."""

import json
import runpy
import time
from pathlib import Path
from typing import Any

import httpx
from compatible_backend_fixture import respond

_original_init = httpx.Client.__init__


def streaming_response(request: httpx.Request) -> httpx.Response:
    response = respond(request)
    body = json.loads(request.content)
    if not body.get("stream"):
        return response
    envelope = json.loads(body["messages"][1]["content"].removeprefix("USER_DATA="))
    question = envelope["data"]["context"]["question"]
    payload = json.loads(response.json()["choices"][0]["message"]["content"])
    payload["answer"] = (
        "这是一段用于验证逐字输出的合成回答。你会先看到开头，再看到后续内容，而引用只在最终校验后出现。"
    )
    if question.startswith("请接着"):
        history = envelope["data"]["context"]["history"]
        assert history[-1]["assistant_state"] == "incomplete_unverified"
        assert history[-1]["assistant"]["answer"]
        payload["answer"] = "这是接着上文的内容，先前收到的正文已经提供给模型。"
    content = json.dumps(payload, ensure_ascii=False)

    class PacedStream(httpx.SyncByteStream):
        def __iter__(self):
            for offset in range(0, len(content), 8):
                time.sleep(0.16)
                if "中断" in question and offset > 40:
                    return
                event = {"choices": [{"delta": {"content": content[offset : offset + 8]}}]}
                yield ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
            if "输出截断" in question:
                yield b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
                return
            if "末段" in question:
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
                return
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=PacedStream())


def initialize(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
    kwargs["transport"] = httpx.MockTransport(streaming_response)
    _original_init(self, *args, **kwargs)


if __name__ == "__main__":
    httpx.Client.__init__ = initialize  # type: ignore[method-assign]
    runpy.run_path(str(Path(__file__).with_name("desktop_backend.py")), run_name="__main__")
