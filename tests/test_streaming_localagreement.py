"""驗證串流辨識器的 LocalAgreement 演算法（不依賴實際 Whisper 模型）。"""

from __future__ import annotations

from teleprompter.core.speech_recognizer import (
    _common_prefix_len_chars,
    _common_prefix_normalized,
)


# ==== _common_prefix_len_chars ====

def test_lcp_basic():
    assert _common_prefix_len_chars("abc", "abd") == 2
    assert _common_prefix_len_chars("abc", "abcdef") == 3
    assert _common_prefix_len_chars("", "anything") == 0


def test_lcp_identical_strings():
    s = "今天我要報告"
    assert _common_prefix_len_chars(s, s) == len(s)


def test_lcp_chinese():
    assert _common_prefix_len_chars("今天我要報告", "今天我要說明") == 4


def test_lcp_completely_different():
    assert _common_prefix_len_chars("abc", "xyz") == 0


# ==== _common_prefix_normalized — 對 a 字串友善的回傳 ====

def test_normalized_lcp_case_insensitive():
    """大小寫差異不影響共同前綴判定。"""
    n = _common_prefix_normalized("Hello world", "hello WORLD")
    assert n == len("Hello world")


def test_normalized_lcp_punctuation_treated_as_separator():
    """標點被當作分隔符（標準化為空白）— 中斷 LCP 是預期行為。

    這個設計避免把不同詞之間的「相同字尾 + 不同標點」誤判為同一個 token。
    """
    a = "今天，我要報告"
    b = "今天我要說明"
    n = _common_prefix_normalized(a, b)
    # 預期：共同前綴 = 「今天」(2 chars in a，因為 a[2]=逗號變空白，b[2]=我)
    assert n == 2
