"""text_utils 單元測試（拼音化、組合相似度）。"""

from __future__ import annotations

import pytest

from teleprompter.core.text_utils import (
    combined_ratio,
    pinyin_tokens_with_positions,
    to_pinyin_form,
)


# ==== to_pinyin_form ====

def test_pinyin_empty():
    assert to_pinyin_form("") == ""


def test_pinyin_pure_chinese():
    assert to_pinyin_form("實作") == "shi zuo"


def test_pinyin_simplified_same_as_traditional():
    # 繁體/簡體/同音字/Whisper 同音誤辨 都必須對映到相同拼音
    assert to_pinyin_form("實作") == to_pinyin_form("实作")
    assert to_pinyin_form("實作") == to_pinyin_form("實做")
    assert to_pinyin_form("實作") == to_pinyin_form("十座")
    assert to_pinyin_form("實作") == to_pinyin_form("食作")


def test_pinyin_mixed_zh_en():
    result = to_pinyin_form("使用 PyTorch 實作")
    # 應該包含英文與拼音
    assert "pytorch" in result
    assert "shi zuo" in result
    assert "shi yong" in result


def test_pinyin_keeps_numbers():
    result = to_pinyin_form("GPT 4")
    assert "gpt" in result
    assert "4" in result


def test_pinyin_ignores_punctuation():
    assert "，" not in to_pinyin_form("你好，世界！")
    assert to_pinyin_form("你好，世界！") == to_pinyin_form("你好 世界")


def test_pinyin_with_spaces_normalized():
    # 多個空白與全形空白都應被統一處理
    result = to_pinyin_form("實   作")
    assert result == "shi zuo"


def test_pinyin_cache_idempotent():
    # 重複呼叫結果必須一致（有 LRU 快取）
    a = to_pinyin_form("人工智慧")
    b = to_pinyin_form("人工智慧")
    assert a == b


# ==== combined_ratio ====

def test_combined_ratio_exact_char_match():
    assert combined_ratio("大家好", "大家好") == 100.0


def test_combined_ratio_zero_for_empty():
    assert combined_ratio("", "anything") == 0.0
    assert combined_ratio("anything", "") == 0.0


def test_combined_ratio_pinyin_rescue():
    # 「實作」與「十座」字元完全不同，拼音相同 → 合併評分應該很高
    score = combined_ratio("實作", "十座")
    assert score >= 80


def test_combined_ratio_different_meaning_different_sound():
    # 「方法」與「結果」字不同、音也不同 → 分數應很低
    score = combined_ratio("方法", "結果")
    assert score < 50


def test_combined_ratio_partial_substring():
    # partial_ratio 在「短字串是長字串子集」時應給高分
    assert combined_ratio("實作", "我們使用實作方法") >= 80


# ==== pinyin_tokens_with_positions ====

def test_pinyin_tokens_empty():
    assert pinyin_tokens_with_positions("") == []


def test_pinyin_tokens_pure_chinese_positions():
    tokens = pinyin_tokens_with_positions("實作")
    # 每個漢字一個 token，end 位置遞增
    assert len(tokens) == 2
    assert tokens[0] == ("shi", 1)
    assert tokens[1] == ("zuo", 2)


def test_pinyin_tokens_mixed():
    # "我用 pytorch" → ["wo", "yong", "pytorch"] with positions in normalized string
    tokens = pinyin_tokens_with_positions("我用 pytorch")
    token_strs = [t for t, _ in tokens]
    assert "wo" in token_strs
    assert "yong" in token_strs
    assert "pytorch" in token_strs
