"""TEST ONLY: real desktop database/API with a deterministic HTTP transport fixture."""
# ruff: noqa: RUF001

import json
import runpy
import time
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
            "kind": "preference",
            "statement": "你可能喜欢提前写下要点再表达。",
            "uncertainty": "这只是一条记录带来的猜测; 需要你确认。",
            "evidence": [
                {
                    "source_fragment_id": fragment["source_fragment_id"],
                    "quote_start": 0,
                    "quote_end": min(len(fragment["text"]), 30),
                }
            ],
        }
    elif envelope["task_type"] == "conversation_reply":
        materials = envelope["data"]["context"]["diary_fragments"]
        fragments = [
            value
            for value in envelope["data"]["fragments"]
            if value["source_fragment_id"] in materials
        ]
        question = envelope["data"]["context"]["question"]
        reviews = envelope["data"]["context"].get("reviewed_memories", [])
        if question == "核对已确认认识":
            assert len(reviews) == 1 and reviews[0]["review"] == "confirm"
        if question == "核对纠正后的认识":
            assert len(reviews) == 1 and reviews[0]["review"] == "correct"
            assert reviews[0]["statement"] == "我只在陌生场合需要提前写要点。"
            assert reviews[0]["version"] == 2
            assert all(item["assistant"] is None for item in envelope["data"]["context"]["history"])
        if question == "等待取消测试":
            time.sleep(2)
        if question.startswith("继续"):
            assert envelope["data"]["context"]["history"]
        result = {
            "answer": "从选定记录可以看到一个值得继续核对的线索。",
            "uncertainty": "合成测试; 仅依据所选日记。",
            "title": "从经历中寻找线索",
            "citations": [
                {
                    "source_fragment_id": value["source_fragment_id"],
                    "quote": "不存在的原话" if "无效引用" in question else value["text"][:30],
                }
                for value in fragments[:1]
            ],
        }
        if envelope["data"]["context"].get("care_allowed") and "关怀验证" in question:
            result["care_letter"] = (
                "读到你说最近一直很累，我想给你留一小段安静的时间。\n\n"
                "今天不必把所有事情都处理好。如果你愿意，可以先放下一件不着急的事，"
                "再慢慢说说最让你挂心的是什么。"
            )

    elif envelope["task_type"] == "life_line_synthesis":
        result = {
            "overview": "这批材料里，提前整理和留出停顿是两条可以继续核对的线索。",
            "themes": [{
                "title": "在表达前整理自己的想法",
                "interpretation": "从你认可的认识出发，提前写要点可能帮助你更清楚地表达。",
                "supporting_ordinals": [1],
                "counterexample_ordinals": [],
                "counterpoint": "熟悉的场合可能不需要这一步，需要补充其他情境。",
                "uncovered_period": "目前没有材料说明这是否是长期习惯。",
            }],
        }
    elif envelope["task_type"] == "memoir_chapter":
        result = {
            "title": "给表达留一点准备的时间",
            "body": (
                "这段文字只回看你已经认可的一条认识：提前写下要点，可能让表达更轻松。"
                "它不是关于你整个人的结论。\n\n一个小的准备动作，或许可以帮助你找到想说的话。"
                "我们还不知道它在熟悉的场合是否同样有用，也没有材料解释更早的经历。"
                "这些空白先留在这里，等你愿意时，再用新的记录慢慢补上。"
            ),
            "uncertainty": "仅依据本次已确认材料整理，未覆盖的经历保持空白。",
            "citation_ordinals": [1],
        }
    else:
        assert envelope["task_type"] == "reversible_action"
        result = {
            "title": "写下一次表达的要点",
            "description": "用五分钟写下想说的三句话。",
            "rationale": "观察提前整理能否让表达更轻松。",
            "exit_plan": "随时停止并删除草稿。",
            "estimated_minutes": 5,
        }
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                }
            ]
        },
    )


def initialize(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
    kwargs["transport"] = httpx.MockTransport(respond)
    _original_init(self, *args, **kwargs)


if __name__ == "__main__":
    httpx.Client.__init__ = initialize  # type: ignore[method-assign]
    runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/desktop_backend.py"), run_name="__main__"
    )
