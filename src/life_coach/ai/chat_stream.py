"""Live chat previews; authorized interrupted prose can be saved as unverified text."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import cast


def partial_answer(content: str) -> str:
    """Decode only a top-level answer string, including incomplete JSON escapes."""
    decoder = json.JSONDecoder(strict=False)
    position = 0
    content = content.lstrip()
    if content.startswith("```") and "\n" in content:
        content = content.split("\n", 1)[1].lstrip()
    if not content.startswith("{"):
        return ""
    position = 1
    try:
        while position < len(content):
            while position < len(content) and content[position] in " \r\n\t,":
                position += 1
            key, position = decoder.raw_decode(content, position)
            while position < len(content) and content[position].isspace():
                position += 1
            if position >= len(content) or content[position] != ":":
                return ""
            position += 1
            while position < len(content) and content[position].isspace():
                position += 1
            if key != "answer":
                _, position = decoder.raw_decode(content, position)
                continue
            if position >= len(content) or content[position] != '"':
                return ""
            start = position
            position += 1
            safe_end = position
            while position < len(content):
                char = content[position]
                if char == '"':
                    tail = content[position + 1 :].lstrip()
                    if (
                        not tail
                        or tail.startswith("}")
                        or re.match(r'^,\s*(?:"[A-Za-z_]+"\s*:|$)', tail)
                    ):
                        return _safe_text(_decode_answer_text(content[start + 1 : position]))
                if char == "\\":
                    if position + 1 >= len(content):
                        break
                    width = 6 if content[position + 1] == "u" else 2
                    if position + width > len(content):
                        break
                    position += width
                else:
                    position += 1
                safe_end = position
            return _safe_text(_decode_answer_text(content[start + 1 : safe_end]))
    except (ValueError, TypeError):
        return ""
    return ""


def _decode_answer_text(raw: str) -> str:
    """Escape literal prose quotes; leave existing JSON escapes unchanged."""
    encoded = []
    escaped = False
    for char in raw:
        if escaped:
            encoded.append(char)
            escaped = False
        elif char == "\\":
            encoded.append(char)
            escaped = True
        elif char == '"' or char in "\n\r\t":
            encoded.append(json.dumps(char)[1:-1])
        else:
            encoded.append(char)
    return cast(str, json.loads('"' + "".join(encoded) + '"'))


def repair_answer_quotes(content: str) -> object | None:
    """Use the first complete JSON boundary; never supply missing fields or structure."""
    start = re.match(r'^\s*\{\s*"answer"\s*:\s*"', content)
    if start is None:
        return None
    for end in list(re.finditer(r'"\s*[,}]', content))[:64]:
        if end.start() < start.end():
            continue
        try:
            answer = _decode_answer_text(content[start.end() : end.start()])
            candidate = json.loads(
                content[: start.end() - 1] + json.dumps(answer) + content[end.start() + 1 :]
            )
            return cast(object, candidate)
        except (ValueError, TypeError):
            continue
    return None


def _safe_text(value: str) -> str:
    # A Unicode surrogate pair may straddle provider chunks.
    return value.encode("utf-16", "surrogatepass").decode("utf-16", "ignore")[:24000]


class ChatStream:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._text = ""
        self._context_hash = ""
        self._closed = False
        self._expires = time.monotonic() + 180

    def bind(self, context_hash: str) -> None:
        with self._lock:
            self._context_hash = context_hash

    def publish(self, text: str) -> None:
        with self._lock:
            if not self._closed:
                self._text = text[:24000]

    def preview(self, context_hash: str) -> str:
        with self._lock:
            if self._closed or time.monotonic() > self._expires:
                return ""
            return self._text if context_hash == self._context_hash else ""

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._text = ""

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed or time.monotonic() > self._expires


current_stream: ContextVar[ChatStream | None] = ContextVar("chat_stream", default=None)
_streams: dict[tuple[uuid.UUID, uuid.UUID], ChatStream] = {}
_lock = threading.Lock()


def get_stream(vault_id: uuid.UUID, turn_id: uuid.UUID) -> ChatStream | None:
    with _lock:
        return _streams.get((vault_id, turn_id))


@contextmanager
def stream_turn(vault_id: uuid.UUID, turn_id: uuid.UUID) -> Iterator[ChatStream]:
    stream = ChatStream()
    key = (vault_id, turn_id)
    with _lock:
        if len(_streams) >= 32:
            raise RuntimeError("chat preview capacity exceeded")
        _streams[key] = stream
    token = current_stream.set(stream)
    try:
        yield stream
    finally:
        stream.close()
        current_stream.reset(token)
        with _lock:
            _streams.pop(key, None)
