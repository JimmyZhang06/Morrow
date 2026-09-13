"""Deterministic diary passages with original Unicode offsets, never new evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from life_coach.ai.retrieval import tokenize_lexical

CHUNK_VERSION = "morrow-cjk-passages-v2"
CHUNK_SIZE = 600
CHUNK_OVERLAP = 80


@dataclass(frozen=True)
class DiaryPassage:
    start: int
    end: int
    text_hash: str
    terms: tuple[str, ...]


def split_diary(text: str) -> list[DiaryPassage]:
    """Prefer sentence ends; retain original whitespace and overlap for boundaries."""
    passages = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        if end < len(text):
            boundary = max(text.rfind(char, start + CHUNK_SIZE // 2, end)
                           for char in "\n。\uff01\uff1f.!?")
            if boundary >= 0:
                end = boundary + 1
        passage = text[start:end]
        passages.append(DiaryPassage(
            start, end, hashlib.sha256(passage.encode("utf-8")).hexdigest(),
            tuple(sorted(set(tokenize_lexical(passage)))),
        ))
        if end == len(text):
            break
        start = end - CHUNK_OVERLAP
    return passages
