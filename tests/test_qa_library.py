"""QA Library 測試。"""

from __future__ import annotations

from pathlib import Path

import pytest

from teleprompter.core.qa_library import (
    QAItem,
    QALibrary,
    load_qa,
    parse_qa_from_text,
)


def test_qa_library_empty_match():
    lib = QALibrary()
    assert lib.match("any question") is None


def test_qa_library_exact_match():
    lib = QALibrary([
        QAItem("什麼是 transformer", "一種深度學習架構"),
        QAItem("訓練資料多大", "100GB 語料"),
    ])
    m = lib.match("什麼是 transformer")
    assert m is not None
    assert "深度學習" in m.item.answer
    assert m.score >= 90


def test_qa_library_fuzzy_match():
    lib = QALibrary([
        QAItem("訓練用什麼資料", "Common Crawl 語料"),
        QAItem("方法基於什麼架構", "Transformer"),
    ])
    # 問法不完全一樣但應能匹配
    m = lib.match("資料來源是什麼")
    assert m is not None


def test_qa_library_pinyin_match():
    """即使用不同字但同音也能匹配。"""
    lib = QALibrary([
        QAItem("實作用什麼框架", "PyTorch"),
    ])
    # Whisper 把「實作」辨識為「十座」
    m = lib.match("十座用什麼框架")
    assert m is not None
    assert m.score >= 60


def test_parse_qa_from_markdown():
    text = (
        "Q: 什麼是 Transformer\n"
        "A: 一種基於 self-attention 的架構\n"
        "\n"
        "Q: 訓練資料\n"
        "A: Common Crawl\n"
        "約 100GB\n"
    )
    lib = parse_qa_from_text(text)
    assert len(lib) == 2
    assert "self-attention" in lib.items[0].answer
    assert "Common Crawl" in lib.items[1].answer
    assert "100GB" in lib.items[1].answer  # 延續行


def test_parse_qa_json(tmp_path: Path):
    f = tmp_path / "qa.json"
    f.write_text(
        '[{"q":"題一","a":"答一"},{"question":"題二","answer":"答二"}]',
        encoding="utf-8",
    )
    lib = load_qa(f)
    assert len(lib) == 2


def test_qa_match_confidence_flag():
    lib = QALibrary([
        QAItem("主題是什麼", "AI"),
        QAItem("你好", "哈囉"),
    ])
    # 清楚匹配
    m = lib.match("主題是什麼")
    assert m is not None
    assert m.is_confident


def test_qa_top_k():
    lib = QALibrary([
        QAItem("q1", "a1"),
        QAItem("q2", "a2"),
        QAItem("q3", "a3"),
    ])
    top = lib.top_k("q", k=2)
    assert len(top) == 2


def test_qa_ignores_empty_query():
    lib = QALibrary([QAItem("q1", "a1")])
    assert lib.match("") is None
    assert lib.match("   ") is None


def test_qa_file_not_found(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_qa(tmp_path / "no.json")


def test_qa_markdown_with_chinese_colon():
    """中文冒號也支援。"""
    text = "Q：請問有多少參數\nA：約 175B\n"
    lib = parse_qa_from_text(text)
    assert len(lib) == 1
    assert "175B" in lib.items[0].answer
