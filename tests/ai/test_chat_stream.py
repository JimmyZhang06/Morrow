# ruff: noqa: E501
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from life_coach.ai.chat_stream import get_stream, partial_answer, repair_answer_quotes, stream_turn
from life_coach.ai.compatible import CompatibleChatCompletionsProvider
from life_coach.ai.stepfun import StepFunProviderError
from tests.ai.test_stepfun_provider import _request


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"answer":"hello', "hello"),
        ('{"answer":"a\\n\\u4f', "a\n"),
        ('{"answer":"\\u4f60\\u597d', "你好"),
        ('{"answer":"\\ud83d', ""),
        ('{"answer":"\\ud83d\\ude00', "😀"),
        ('{"nested":{"answer":"private"},"answer":"visible', "visible"),
        ('{"answer":"safe","citations":[', "safe"),
        ('{"answer":"a\\"b', 'a"b'),
        ('{"reasoning":"not for display', ""),
        ('{"answer":"we feel "alike" sometimes', 'we feel "alike" sometimes'),
        ('```json\n{"answer":"first\nsecond', "first\nsecond"),
        ('{"answer":"' + "a" * 7000, "a" * 7000),
    ],
)
def test_partial_json(raw, expected):
    assert partial_answer(raw) == expected


def test_prose_quotes_are_escaped_by_program_without_changing_metadata():
    raw = '{"answer":"We feel "alike".\\nWhy?","citations":[],"title":"A title"}'
    repaired = repair_answer_quotes(raw)
    assert repaired == {"answer": 'We feel "alike".\nWhy?', "citations": [], "title": "A title"}
    assert repair_answer_quotes(raw[:-1]) is None
    assert repair_answer_quotes('{"answer":"unfinished') is None
    assert repair_answer_quotes('{"answer":"valid"} trailing garbage') is None


def chat_request():
    request = _request()
    policy = request.run_spec.policy.model_copy(update={"task_type": "conversation_reply"})
    return replace(request, run_spec=request.run_spec.model_copy(update={"policy": policy}))


def test_stream_emits_preview_before_response_finishes_and_cleans_up():
    vault, turn = uuid4(), uuid4()
    observed = []
    with stream_turn(vault, turn) as channel:
        channel.bind("approved-context")

        class ResponseStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"{\\"answer\\":\\"hello"}}]}\n\n'
                observed.append(channel.preview("approved-context"))
                yield b'data: {"choices":[{"delta":{"content":" world\\"}"},"finish_reason":"stop"}]}\n\n'
                yield b"data: [DONE]\n\n"

        def handler(request):
            assert json.loads(request.content)["stream"] is True
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=ResponseStream()
            )

        provider = CompatibleChatCompletionsProvider(
            api_key=SecretStr("synthetic"),
            model="test",
            base_url="https://fixture.example/v1",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        assert provider.complete(chat_request()) == {"answer": "hello world"}
        assert observed == ["hello"]
        assert channel.preview("different-context") == ""
        assert get_stream(uuid4(), turn) is None
    assert channel.preview("approved-context") == ""
    assert get_stream(vault, turn) is None


@pytest.mark.parametrize(
    "ending", [b"", b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n']
)
def test_interrupted_or_truncated_stream_fails_without_retry(ending):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"{\\"answer\\":\\"draft"}}]}\n\n'
            + ending,
        )

    provider = CompatibleChatCompletionsProvider(
        api_key=SecretStr("synthetic"),
        model="test",
        base_url="https://fixture.example/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with stream_turn(uuid4(), uuid4()), pytest.raises(StepFunProviderError):
        provider.complete(chat_request())
    assert len(calls) == 1


@pytest.mark.parametrize("streaming", [True, False])
def test_length_limit_keeps_prose_in_final_chunk(streaming):
    calls = []

    def handler(request):
        calls.append(request)
        content = '{"answer":"received final words'
        if streaming:
            event = {"choices": [{"delta": {"content": content}, "finish_reason": "length"}]}
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="data: " + json.dumps(event) + "\n\n",
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": content},
                        "finish_reason": "length",
                    }
                ]
            },
        )

    provider = CompatibleChatCompletionsProvider(
        api_key=SecretStr("synthetic"),
        model="test",
        base_url="https://fixture.example/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with stream_turn(uuid4(), uuid4()) as channel:
        channel.bind("context")
        with pytest.raises(StepFunProviderError):
            provider.complete(chat_request())
        assert channel.preview("context") == "received final words"
    assert len(calls) == 1


def test_provider_ignoring_stream_returns_same_response_without_second_call():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":"ok"}'}}]})

    provider = CompatibleChatCompletionsProvider(
        api_key=SecretStr("synthetic"),
        model="test",
        base_url="https://fixture.example/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with stream_turn(uuid4(), uuid4()):
        assert provider.complete(chat_request()) == {"answer": "ok"}
    assert len(calls) == 1


def test_cancel_closes_preview_immediately():
    with stream_turn(uuid4(), uuid4()) as channel:
        channel.bind("context")
        channel.publish("draft")
        channel.close()
        channel.publish("late network data")
        assert channel.closed
        assert channel.preview("context") == ""


@pytest.mark.parametrize("separator", [b"", b"\n", b"\n\n"])
def test_final_stop_frame_without_done_is_consumed(separator):
    payload = json.dumps(
        {"choices": [{"delta": {"content": '{"answer":"complete"}'}, "finish_reason": "stop"}]}
    ).encode()
    provider = CompatibleChatCompletionsProvider(
        api_key=SecretStr("synthetic"),
        model="test",
        base_url="https://fixture.example/v1",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=b"data: " + payload + separator,
                )
            )
        ),
    )
    with stream_turn(uuid4(), uuid4()):
        assert provider.complete(chat_request()) == {"answer": "complete"}


def test_stop_does_not_wait_for_proxy_that_stalls_before_done():
    class CompletedThenDisconnected(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"{\\"answer\\":\\"ok\\"}"},"finish_reason":"stop"}]}\n\n'
            raise httpx.ReadTimeout("proxy stalled after confirmed completion")

    provider = CompatibleChatCompletionsProvider(
        api_key=SecretStr("synthetic"),
        model="test",
        base_url="https://fixture.example/v1",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=CompletedThenDisconnected(),
                )
            )
        ),
    )
    with stream_turn(uuid4(), uuid4()):
        assert provider.complete(chat_request()) == {"answer": "ok"}


def test_literal_whitespace_in_json_string_preserves_text_without_new_call():
    assert CompatibleChatCompletionsProvider._decode_json_content(
        '{"answer":"one\n\tsecond\rline", "citations":[]}'
    ) == {"answer": "one\n\tsecond\rline", "citations": []}
    for invalid in ('{"answer":"unfinished', '{"answer":"ok",}', '{"answer":"bad\x00"}'):
        with pytest.raises(StepFunProviderError):
            CompatibleChatCompletionsProvider._decode_json_content(invalid)
