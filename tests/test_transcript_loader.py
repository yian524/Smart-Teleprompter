"""TranscriptLoader 單元測試。"""

from __future__ import annotations

from pathlib import Path

import pytest

from teleprompter.core.transcript_loader import (
    Sentence,
    load_from_string,
    load_md,
    load_transcript,
    load_txt,
    normalize_text,
    normalize_with_map,
    split_sentences,
)


def test_normalize_fullwidth_to_halfwidth():
    assert normalize_text("ＡＢＣ１２３") == "abc123"


def test_normalize_removes_punctuation():
    assert normalize_text("你好，世界！") == "你好 世界"


def test_normalize_english_lowercase():
    assert normalize_text("Hello World") == "hello world"


def test_normalize_mixed_zh_en():
    text = "今天介紹 Transformer 模型!"
    norm = normalize_text(text)
    assert "transformer" in norm
    assert "今天介紹" in norm


def test_normalize_with_map_indices():
    raw = "AB，CD"
    norm, mapping = normalize_with_map(raw, base_offset=10)
    # 'a','b',' ','c','d'
    assert norm == "ab cd"
    assert len(mapping) == len(norm)
    # 'a' 對應到原字 'A' 在位置 10
    assert mapping[0] == 10
    # 'b' 對應到原字 'B' 在位置 11
    assert mapping[1] == 11
    # 'c' 對應到原字 'C' 在位置 13
    assert mapping[3] == 13


def test_split_sentences_basic():
    text = "大家好。今天要報告一個很有趣的主題！讓我們開始吧？"
    sentences = split_sentences(text)
    assert len(sentences) == 3
    for s in sentences:
        assert text[s.start:s.end] == s.text


def test_split_sentences_mixed_zh_en():
    text = "我們用 PyTorch 訓練模型。The accuracy is 95%."
    sentences = split_sentences(text)
    assert len(sentences) == 2
    assert "PyTorch" in sentences[0].text
    assert "accuracy" in sentences[1].normalized.lower()


def test_split_sentences_without_terminator():
    text = "沒有標點的一段話"
    sentences = split_sentences(text)
    assert len(sentences) == 1
    assert sentences[0].normalized == "沒有標點的一段話"


def test_sentence_normalized_to_global():
    text = "Hello 世界!"
    sentences = split_sentences(text)
    s = sentences[0]
    # normalized = "hello 世界"
    # 映射應把 normalized[0]='h' → 原文 index 0
    assert s.normalized_to_global(0) == 0
    # 映射最後一字 '界' 的位置（原文 index 7）
    idx_of_world = text.index("界")
    assert s.normalized_to_global(len(s.normalized) - 1) == idx_of_world


def test_load_from_string():
    text = "第一句。第二句！第三句？"
    t = load_from_string(text)
    assert len(t.sentences) == 3
    assert t.full_text == text


def test_load_txt_utf8(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("你好。世界。", encoding="utf-8")
    t = load_transcript(f)
    assert len(t.sentences) == 2


def test_load_md_strips_markdown(tmp_path: Path):
    f = tmp_path / "a.md"
    f.write_text("# 標題\n\n這是**粗體**內容。第二句。", encoding="utf-8")
    raw = load_md(f)
    assert "#" not in raw
    assert "**" not in raw
    t = load_transcript(f)
    assert any("粗體" in s.text for s in t.sentences)


def test_load_unsupported_extension_falls_back_to_txt(tmp_path: Path):
    f = tmp_path / "a.log"
    f.write_text("內容一。內容二。", encoding="utf-8")
    t = load_transcript(f)
    assert len(t.sentences) == 2


def test_file_not_found(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_transcript(tmp_path / "nope.txt")


# ============================================================
# 註解 + 分頁功能
# ============================================================

def test_html_comments_are_stripped():
    from teleprompter.core.transcript_loader import strip_comments
    text = "第一句。<!-- 這是註解 -->第二句。"
    cleaned = strip_comments(text)
    assert "<!--" not in cleaned
    assert "註解" not in cleaned
    assert "第一句" in cleaned
    assert "第二句" in cleaned


def test_multiline_comments_stripped():
    from teleprompter.core.transcript_loader import strip_comments
    text = "第一句。<!-- 這是\n多行\n註解 -->第二句。"
    cleaned = strip_comments(text)
    assert "第一句" in cleaned
    assert "第二句" in cleaned
    assert "註解" not in cleaned


def test_page_separator_creates_pages():
    text = (
        "第一頁第一句。第一頁第二句。\n"
        "---\n"
        "第二頁第一句。第二頁第二句。\n"
        "---\n"
        "第三頁第一句。"
    )
    t = load_from_string(text)
    assert len(t.pages) == 3
    assert t.pages[0].number == 1
    assert t.pages[1].number == 2
    assert t.pages[2].number == 3


def test_no_separator_single_page():
    t = load_from_string("第一句。第二句。第三句。")
    assert len(t.pages) == 1
    assert t.pages[0].number == 1


def test_page_of_sentence_lookup():
    text = (
        "甲頁第一句。甲頁第二句。\n"
        "---\n"
        "乙頁第一句。乙頁第二句。"
    )
    t = load_from_string(text)
    # 前 2 句屬 page 1
    p = t.page_of_sentence(0)
    assert p is not None
    assert p.number == 1
    # 後 2 句屬 page 2
    p2 = t.page_of_sentence(2)
    assert p2 is not None
    assert p2.number == 2


def test_comment_and_page_together():
    text = (
        "<!-- 這段會被忽略 -->\n"
        "第一頁內容。\n"
        "---\n"
        "<!-- 對應 slide 2 -->\n"
        "第二頁內容。"
    )
    t = load_from_string(text)
    assert len(t.pages) == 2
    # 註解不該出現在 sentences
    for s in t.sentences:
        assert "忽略" not in s.text
        assert "slide 2" not in s.text


def test_page_title_extracted_from_heading():
    text = (
        "# 背景介紹\n"
        "這是第一頁內容。\n"
        "---\n"
        "# 方法論\n"
        "這是第二頁內容。"
    )
    t = load_from_string(text)
    assert len(t.pages) == 2
    assert "背景" in t.pages[0].title or "介紹" in t.pages[0].title
    assert "方法" in t.pages[1].title
