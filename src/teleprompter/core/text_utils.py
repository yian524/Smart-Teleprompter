"""文字處理工具：拼音化、組合相似度評分。

核心觀察：
- 繁/簡（實作/实作）、同音字（實作/實做）、Whisper 誤辨同音字（實作/十座/食作）
  在字元層面無法匹配，但在拼音層面完全一致。
- 因此對齊時同時計算字元相似度與拼音相似度，取較高者。
- 英文字母保留原樣（小寫），只對中文字元轉拼音。
"""

from __future__ import annotations

import re
from functools import lru_cache

from pypinyin import Style, lazy_pinyin
from rapidfuzz import fuzz

_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]+")
_ENGLISH_RE = re.compile(r"[A-Za-z0-9]+")
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9]+")


def pinyin_tokens_with_positions(normalized: str) -> list[tuple[str, int]]:
    """把 normalized 文字切成 pinyin token，附帶每個 token 在原 normalized 中的結束位置。

    - 每個中文字 → 1 個拼音 token
    - 每個英文/數字段 → 1 個小寫 token
    - 空白/其他字元被忽略
    回傳: [(token, end_pos_in_normalized), ...]
    """
    if not normalized:
        return []
    results: list[tuple[str, int]] = []
    for m in _TOKEN_RE.finditer(normalized):
        segment = m.group(0)
        end_pos = m.end()
        if _CHINESE_RE.fullmatch(segment):
            py = lazy_pinyin(segment, style=Style.NORMAL, errors="ignore")
            results.append((py[0] if py else segment, end_pos))
        else:
            results.append((segment.lower(), end_pos))
    return results


@lru_cache(maxsize=4096)
def to_pinyin_form(text: str) -> str:
    """把文字轉成拼音化的可比對形式。

    - 中文連續段轉為「空白分隔的拼音」（e.g. "實作" -> "shi zuo"）
    - 英文/數字連續段保留並轉小寫
    - 其他字元略過
    - 段與段之間以單一空白分隔
    """
    if not text:
        return ""
    tokens: list[str] = []
    pos = 0
    for m in re.finditer(r"[\u4e00-\u9fff]+|[A-Za-z0-9]+", text):
        if m.start() > pos:
            pass  # 忽略中間的標點/空白，由最後 join 補上空白
        segment = m.group(0)
        if _CHINESE_RE.fullmatch(segment):
            # 轉拼音
            py_parts = lazy_pinyin(segment, style=Style.NORMAL, errors="ignore")
            tokens.extend(py_parts)
        else:
            tokens.append(segment.lower())
        pos = m.end()
    return " ".join(tokens)


def combined_ratio(a: str, b: str) -> float:
    """結合「原字元 partial_ratio」與「拼音 partial_ratio」，取較高者。

    - 原字元比對適合英文與罕見字
    - 拼音比對處理繁/簡/同音字/Whisper 同音誤辨
    """
    if not a or not b:
        return 0.0
    char_score = fuzz.partial_ratio(a, b)
    py_a = to_pinyin_form(a)
    py_b = to_pinyin_form(b)
    if py_a and py_b:
        pinyin_score = fuzz.partial_ratio(py_a, py_b)
    else:
        pinyin_score = 0.0
    return max(char_score, pinyin_score)
