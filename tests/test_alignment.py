"""AlignmentEngine 單元測試。

驗證：
- 完全匹配 → 立即更新位置
- 輕微口誤 → 仍能對齊
- 中英混合 → 可對齊
- 跳句 → 能找到新位置
- 低信心 → 不更新，位置保持
- 防倒退：講者停頓重念一次時位置不倒退太多
- 手動跳句
"""

from __future__ import annotations

import pytest

from teleprompter.core.alignment_engine import (
    HIGH_CONFIDENCE,
    MID_CONFIDENCE,
    AlignmentEngine,
)
from teleprompter.core.transcript_loader import load_from_string


SAMPLE_TRANSCRIPT = (
    "大家好，今天我要報告人工智慧在醫療影像的應用。"
    "首先讓我們從問題本身談起。"
    "目前的醫療影像診斷仰賴專業醫師逐張判讀。"
    "我們使用 PyTorch 訓練了一個 CNN 模型來做 image classification。"
    "實驗結果顯示準確率達到百分之九十五。"
    "最後是結論與未來展望。"
)


def _engine():
    t = load_from_string(SAMPLE_TRANSCRIPT)
    return AlignmentEngine(t), t


def test_exact_match_advances_position():
    eng, t = _engine()
    initial = eng.current_global_char
    result = eng.update("大家好今天我要報告人工智慧")
    assert result.updated
    assert result.confidence >= HIGH_CONFIDENCE
    assert eng.current_global_char > initial


def test_partial_match_still_updates():
    eng, _ = _engine()
    result = eng.update("今天我要報告人工智慧在醫療影像")
    assert result.updated
    assert result.confidence >= HIGH_CONFIDENCE


def test_mid_confidence_requires_confirmation():
    eng, _ = _engine()
    # 構造一個中等相似度的輸入（大部分正確但有雜訊）
    result1 = eng.update("呃大家好 呃 今天那個報告")
    # 第一次可能不會立即更新（中信心）
    result2 = eng.update("呃大家好 呃 今天那個報告")
    # 兩次之後應更新
    assert result2.updated or result1.updated


def test_low_confidence_ignored():
    eng, _ = _engine()
    initial = eng.current_global_char
    result = eng.update("完全不相關的句子 abcdef 這是雜訊")
    assert not result.updated
    assert eng.current_global_char == initial


def test_mixed_zh_en_alignment():
    eng, _ = _engine()
    # 推進到中英混合句
    eng.update("大家好今天我要報告人工智慧在醫療影像的應用")
    eng.update("首先讓我們從問題本身談起")
    eng.update("目前的醫療影像診斷仰賴專業醫師")
    before = eng.current_global_char
    result = eng.update("我們使用 pytorch 訓練 CNN 模型")
    assert result.updated
    assert eng.current_global_char > before


def test_jump_sentence_forward():
    eng, t = _engine()
    result = eng.jump_to_sentence(3)
    assert result.updated
    assert eng.current_sentence_index == 3
    assert eng.current_global_char == t.sentences[3].start


def test_jump_to_global_char():
    eng, t = _engine()
    # 跳到第二句中間某個位置
    target = t.sentences[2].start + 3
    result = eng.jump_to_global_char(target)
    assert result.updated
    assert eng.current_global_char == target
    assert eng.current_sentence_index == 2


def test_no_backward_jump_on_repeat():
    eng, _ = _engine()
    # 先推進到較後面
    eng.update("大家好今天我要報告人工智慧在醫療影像的應用")
    eng.update("首先讓我們從問題本身談起")
    eng.update("目前的醫療影像診斷仰賴專業醫師逐張判讀")
    forward_pos = eng.current_global_char

    # 模擬講者突然說出前面已經說過的話 (原樣) — 應不讓位置大幅回退
    eng.update("大家好")
    assert eng.current_global_char >= forward_pos - 10  # 容許極小回退


def test_skip_sentence_recoverable():
    eng, t = _engine()
    # 使用者跳過前面幾句，直接念第 5 句的內容
    last_sentence_norm = t.sentences[-1].normalized
    # 要夠長才能觸發跳句容忍
    eng.update("很不相關")  # stagnant 1
    eng.update("也不相關")  # stagnant 2, 觸發 expand
    result = eng.update("最後是結論與未來展望")
    # 應能找到新位置
    assert result.updated
    assert eng.current_sentence_index >= 4


def test_reset_to_beginning():
    eng, _ = _engine()
    eng.update("大家好今天我要報告")
    eng.update("首先讓我們從問題本身談起")
    assert eng.current_global_char > 0
    eng.reset()
    assert eng.current_sentence_index == 0
    assert eng.current_global_char == 0


def test_skip_detection_marks_skipped_range():
    eng, t = _engine()
    # 起始在第 0 句，直接跳到第 4 句的內容
    eng.update("大家好今天我要報告")  # sentence 0
    # 直接念第 4 句的內容（跳過 1, 2, 3）
    result = eng.update("實驗結果顯示準確率達到百分之九十五")
    assert result.updated
    assert result.has_skipped
    # 跳過範圍至少應涵蓋 sent 1-3（若使用者 sent 0 未念完，sent 0 也可能被納入）
    assert result.skipped_start <= t.sentences[1].start
    assert result.skipped_end == t.sentences[4].start


def test_no_skip_when_previous_sentence_fully_read():
    """前一句已念完整時，推進到下一句不應觸發 has_skipped。"""
    eng, t = _engine()
    # 念完整第一句（含所有字元）
    eng.update(t.sentences[0].normalized)
    result = eng.update(t.sentences[1].normalized)
    assert result.updated
    assert not result.has_skipped, (
        f"前句已念完進度應 ≥ 70%，不應標漏講；實際 ranges={result.skipped_ranges}"
    )


def test_no_skip_on_manual_jump():
    eng, _ = _engine()
    # 手動跳句不應該回傳 has_skipped（manual jump 走的是另一條路徑）
    result = eng.jump_to_sentence(3)
    assert result.updated
    assert not result.has_skipped


# ---- 拼音比對相關 ----

def test_pinyin_matches_simplified_chinese():
    """簡體/繁體同一發音應能對齊。"""
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string

    script = "我們今天來介紹實作的細節。接著是結論。"
    t = load_from_string(script)
    eng = AlignmentEngine(t)
    # Whisper 輸出簡體「实作」應能對齊到繁體「實作」
    result = eng.update("我們今天來介紹实作的細節")
    assert result.updated
    assert result.confidence >= 70


def test_pinyin_matches_homophone_typo():
    """Whisper 誤辨同音字（實作→十座）應能對齊。"""
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string

    script = "第一段是背景介紹。第二段說明實作方法。第三段是結果。"
    t = load_from_string(script)
    eng = AlignmentEngine(t)
    # 講者說「第二段說明實作方法」Whisper 可能輸出「第二段說明十座方法」
    result = eng.update("第二段說明十座方法")
    assert result.updated
    assert result.sentence_index == 1
    assert result.confidence >= 70


def test_pinyin_matches_mixed_en_zh_homophone():
    """中英夾雜 + 同音字誤辨。"""
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string

    script = "我們用 PyTorch 做實作。效果很好。"
    t = load_from_string(script)
    eng = AlignmentEngine(t)
    # 使用者說「我們用 PyTorch 做實作」Whisper 輸出「我們用 pytorch 做食作」
    result = eng.update("我們用 pytorch 做食作")
    assert result.updated
    assert result.sentence_index == 0
    assert result.confidence >= 70


def test_buffer_trims_after_high_commit_so_position_advances():
    """驗證修剪 buffer 後，下一句的短 delta 能推進到下一句而非卡在上一句。"""
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string

    script = "第一句內容。第二句內容。第三句內容。"
    t = load_from_string(script)
    eng = AlignmentEngine(t)
    # 先念完第一句
    eng.update("第一句內容")
    idx_after_first = eng.current_sentence_index
    # 接著短 delta 念第二句
    eng.update("第二句")
    eng.update("內容")
    # 此時應已推進至第二句或之後
    assert eng.current_sentence_index >= 1
    assert eng.current_sentence_index >= idx_after_first


# ---- 跳段保護：短辨識文字 + 關鍵詞重複 不應大幅跳段 ----

def test_short_common_word_does_not_cause_big_jump():
    """講稿多句都含「方法」，使用者只說「方法」兩字不應跳到很後面。"""
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string

    script = (
        "第一段介紹方法。"
        "第二段詳述方法細節。"
        "第三段比較方法與其他做法。"
        "第四段總結方法優缺點。"
        "第五段探討方法限制。"
    )
    t = load_from_string(script)
    eng = AlignmentEngine(t)
    # 正常念到第一句
    eng.update("第一段介紹方法")
    start_idx = eng.current_sentence_index
    # 使用者只丟出「方法」兩字 → 不應因多句含「方法」而跳到第 4、5 段
    eng.update("方法")
    assert eng.current_sentence_index - start_idx <= 2, (
        f"不應因短詞重複大跳，從 {start_idx} 跳到 {eng.current_sentence_index}"
    )


def test_proximity_prefers_next_sentence_over_far_similar():
    """下一句與很遠一句分數相近時應優先選下一句。"""
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string

    script = (
        "這是第一段內容。"
        "接著介紹第二段內容。"
        "第三段是數據分析。"
        "第四段是效能評估。"
        "這裡再次說明內容。"
    )
    t = load_from_string(script)
    eng = AlignmentEngine(t)
    eng.update("這是第一段內容")
    # 講者繼續念第二句
    eng.update("接著介紹第二段內容")
    # 應該推進到第二句，而非跳到最後那個「再次說明內容」的句子
    assert eng.current_sentence_index == 1


def test_legitimate_skip_triggers_has_skipped():
    """講者明確跳段（念後面的句子且與當前差距大）應觸發漏講標記。"""
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string

    script = (
        "大家好我要介紹主題。"
        "首先談背景動機。"
        "接著說明相關工作。"
        "再來是方法設計。"
        "最後是結論與未來展望。"
    )
    t = load_from_string(script)
    eng = AlignmentEngine(t)
    eng.update("大家好我要介紹主題")
    assert eng.current_sentence_index == 0
    # 直接跳到第五句（跳過 1, 2, 3）
    result = eng.update("最後是結論與未來展望")
    assert result.updated
    assert result.sentence_index == 4
    assert result.has_skipped, "跳段應被偵測並回傳 has_skipped"
    # 被標記漏講的是中間三句
    assert result.skipped_start == t.sentences[1].start
    assert result.skipped_end == t.sentences[4].start
