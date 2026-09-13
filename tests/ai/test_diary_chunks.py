import hashlib

import pytest

from life_coach.ai.diary_chunks import CHUNK_SIZE, split_diary


@pytest.mark.parametrize("text", ["", "今天很开心。", "中🙂e\u0301\n" * 400, "长" * 2000])
def test_chunks_cover_original_without_rewriting_unicode(text):
    passages = split_diary(text)
    covered = set()
    for p in passages:
        assert 0 <= p.start < p.end <= len(text)
        assert p.end - p.start <= CHUNK_SIZE
        assert p.text_hash == hashlib.sha256(text[p.start:p.end].encode()).hexdigest()
        covered.update(range(p.start, p.end))
    assert covered == set(range(len(text)))
    assert passages == split_diary(text)


def test_sentence_boundary_and_overlap():
    text = "甲" * 340 + "。" + "乙" * 500
    first, second = split_diary(text)[:2]
    assert first.end == 341
    assert second.start < first.end
