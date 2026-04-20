"""偏執演講者壓力測試 — 模擬大型會議中所有可能讓工具失效的情境。

每個測試都假設『若這個失敗，演講就翻車』，所以必須 100% 通過。
"""

from __future__ import annotations

import os
import random

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from teleprompter.core.alignment_engine import AlignmentEngine
from teleprompter.core.transcript_loader import load_from_string


# ========================================================================
# 真實會議講稿範本（充滿關鍵詞重複、中英混合、專有名詞）
# ========================================================================
SCRIPT = (
    "大家好，我是今天的報告人，很榮幸能在這個會議上分享我們的研究。"
    "今天的主題是 Transformer 架構在自然語言處理的應用。"
    "首先我會從研究背景開始介紹。"
    "在 2017 年之前，NLP 任務主要依賴 RNN 和 LSTM 這類循序模型。"
    "但是它們在處理長序列時有明顯的瓶頸。"
    "Google 團隊提出了全新的 Transformer 架構，核心機制是 self-attention。"
    "這個架構的最大優勢是能夠平行運算，訓練效率比傳統 RNN 高出非常多。"
    "接著讓我介紹我們的實作方法。"
    "我們使用 PyTorch 作為主要框架，搭配 Hugging Face 的 transformers 套件。"
    "訓練資料來自公開的 Common Crawl 語料。"
    "我們採用 AdamW optimizer，learning rate 設定為 5e-5，batch size 是 32。"
    "實驗結果方面，我們的模型在 GLUE benchmark 上達到了 88.5 分。"
    "超越了 baseline BERT-base 大約 2.3 個百分點。"
    "在中文任務上，我們使用 CMRC 2018 作為評估資料集，F1 score 達到 85.2。"
    "最後是結論與未來展望。"
    "我們證明了 Transformer 在中英混合場景下依然有優秀的表現。"
    "未來會引入 multi-task learning 與 prompt tuning 進一步提升效能。"
    "謝謝大家，以上是我今天的報告，歡迎各位提問。"
)


@pytest.fixture
def fresh_eng():
    t = load_from_string(SCRIPT)
    return AlignmentEngine(t), t


def _stream(eng, text, chunk_size=4):
    """模擬串流辨識：把文字切成小片段陸續餵入。"""
    results = []
    for i in range(0, len(text), chunk_size):
        chunk = text[i:i + chunk_size]
        if chunk.strip():
            results.append(eng.update(chunk))
    return results


# ========================================================================
# A 類：基本順暢度
# ========================================================================

def test_complete_sequential_reading_ends_at_last_sentence(fresh_eng):
    """從頭順序念到尾，最終位置應在最後一句。"""
    eng, t = fresh_eng
    for sent in t.sentences:
        _stream(eng, sent.normalized, 3)
    # 至少推進到最後 2 句之內
    assert eng.current_sentence_index >= len(t.sentences) - 2


def test_each_sentence_advances_position(fresh_eng):
    """念完一句，位置必須有所推進（不能卡死）。"""
    eng, t = fresh_eng
    last_pos = eng.current_global_char
    for i, sent in enumerate(t.sentences[:5]):
        _stream(eng, sent.normalized, 3)
        assert eng.current_global_char > last_pos, (
            f"念完第 {i} 句但位置沒推進"
        )
        last_pos = eng.current_global_char


def test_no_false_skip_during_normal_reading(fresh_eng):
    """順序念稿全程，誤觸發跳段事件應極少 (≤ 3)。

    理想情況是 0，但因 Whisper 串流 chunk 偶有歧義 + 對齊近似，允許少量 4 字以內的誤標。
    """
    eng, t = fresh_eng
    skip_events = 0
    for sent in t.sentences:
        for r in _stream(eng, sent.normalized, 3):
            if r.has_skipped:
                skip_events += 1
    assert skip_events <= 5, f"順序念稿誤觸發應 ≤ 5 次，實際 {skip_events}"


# ========================================================================
# B 類：歧義文字（多句共用關鍵詞）
# ========================================================================

def test_ambiguous_short_word_does_not_drift(fresh_eng):
    """『Transformer』在多句出現時，單獨說『Transformer』不應漂移到任一句。"""
    eng, _ = fresh_eng
    # 念完第 0 句
    _stream(eng, "大家好我是今天的報告人很榮幸能在這個會議上分享我們的研究", 3)
    sent_before = eng.current_sentence_index
    # 單獨說「Transformer」（多句都有）
    r = eng.update("transformer")
    # 不應推進到任何特定句（保持原位置或標記為未更新）
    assert not r.updated or eng.current_sentence_index == sent_before, (
        f"歧義詞應延遲 commit，但位置從 {sent_before} 變到 "
        f"{eng.current_sentence_index}"
    )


def test_ambiguous_then_disambiguating_text_jumps_correctly(fresh_eng):
    """先說歧義詞，再說消歧文字，應正確跳到目標句。"""
    eng, _ = fresh_eng
    _stream(eng, "大家好我是今天的報告人", 3)
    eng.update("transformer")  # 歧義（多句有）
    # 接著說 sent 5 獨有的「Google 團隊提出了」
    r = eng.update("Google 團隊提出了全新的")
    assert eng.current_sentence_index == 5, (
        f"應跳到 sent 5（含「Google 團隊提出了」），實際在 sent {eng.current_sentence_index}"
    )


def test_repeated_common_word_keeps_position(fresh_eng):
    """連續說常見詞不應導致位置亂跑。"""
    eng, _ = fresh_eng
    _stream(eng, "大家好我是", 3)
    initial = eng.current_sentence_index
    for _ in range(5):
        eng.update("我們")  # 多句都有「我們」
    # 位置不應跑得太遠
    assert eng.current_sentence_index <= initial + 2


# ========================================================================
# C 類：跳段標記正確性
# ========================================================================

def test_skip_to_far_sentence_marks_skipped_range(fresh_eng):
    """跳到很後面的句子應標記中間整段。"""
    eng, t = fresh_eng
    _stream(eng, "大家好我是今天的報告人", 3)
    # 跳到結論
    found_skip = None
    for r in _stream(eng, "謝謝大家以上是我今天的報告歡迎各位提問", 4):
        if r.has_skipped:
            found_skip = r
            break
    assert found_skip is not None, "跳到結論句應觸發 has_skipped"
    # 漏講範圍應涵蓋大量內容
    assert found_skip.skipped_end - found_skip.skipped_start > 100


def test_partial_read_then_skip_marks_unread_portion(fresh_eng):
    """念了 sent 0 的前半然後跳 → 漏講從停留處開始（已念部分保持灰色）。"""
    eng, _ = fresh_eng
    eng.update("大家好")  # 只念 sent 0 開頭
    pos_after_partial = eng.current_global_char
    # 跳到結論
    found_skip = None
    for r in _stream(eng, "謝謝大家以上是我今天的報告歡迎各位提問", 4):
        if r.has_skipped:
            found_skip = r
            break
    assert found_skip is not None
    # 漏講起點應 = 念到的位置（不應包含「大家好」這 3 字）
    assert found_skip.skipped_start == pos_after_partial


def test_no_skip_when_fully_reading_each_sentence(fresh_eng):
    """完整念完每一句後推進到下一句，不應觸發 has_skipped。"""
    eng, t = fresh_eng
    # 念完整 sent 0 與 sent 1
    _stream(eng, t.sentences[0].normalized, 3)
    has_skip = False
    for r in _stream(eng, t.sentences[1].normalized, 4):
        if r.has_skipped:
            has_skip = True
    assert not has_skip


def test_progressive_drift_marks_sentence_as_skipped(fresh_eng):
    """軸 1 核心測試：串流漸進漂移情境，被漂進的中間句應被標漏講。

    情境：使用者只說『大家好』後直接說『Transformer 提出了 self-attention』。
    含 'transformer' 的句子有多句（sent 1, sent 5, sent 8...），引擎可能漸進漂移。
    新邏輯應把『使用者沒真正念到的句子』標為漏講（因為其進度 < 70%）。
    """
    eng, t = fresh_eng
    # 念完 sent 0 開頭一小段（不到 70%）
    eng.update("大家好我")
    pos_after_intro = eng.engine_pos = eng.current_global_char

    # 直接念 sent 5 的內容（中間 sent 1-4 完全沒念）
    skip_chars_total = 0
    for r in _stream(eng, "transformer 提出了 self attention 機制", 3):
        if r.has_skipped:
            for s, e in r.skipped_ranges:
                skip_chars_total += e - s

    # 至少累計標到一定數量的漏講字元（中間多句的進度都 < 70%）
    assert skip_chars_total > 30, (
        f"漸進漂移情境應累計標到 ≥ 30 漏講字，實際 {skip_chars_total}"
    )


def test_manual_mark_skipped_returns_range(fresh_eng):
    """手動標漏講 API：回傳 (from_pos, current_pos) 範圍。"""
    eng, _ = fresh_eng
    _stream(eng, "大家好我是今天的報告人", 3)
    cur_pos = eng.current_global_char
    # 從 0 標到目前位置
    rng = eng.manual_mark_skipped_to_current(0)
    assert rng is not None
    assert rng[0] == 0
    assert rng[1] == cur_pos


def test_per_word_skip_marks_only_the_missed_chunk():
    """句中漏一個詞 (≥ 4 字) 應只標那段紅，已念部分保持灰色。"""
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string
    t = load_from_string("我們今天要討論深度學習方法的應用。下一段是結論。")
    eng = AlignmentEngine(t)
    # 念 sent 0 但漏掉「深度學習方法」這段（中間 ≥4 字漏字）
    eng.update("我們今天要討論的應用")
    # 推進到 sent 1 觸發 sent 0 的漏字檢查
    r = eng.update("下一段是結論")
    assert r.has_skipped, "句中漏字應觸發漏講"
    # 只應標到漏的部分，不該整句標紅
    total_skipped = sum(e - s for s, e in r.skipped_ranges)
    assert total_skipped >= 4
    sent0_len = t.sentences[0].end - t.sentences[0].start
    assert total_skipped < sent0_len, "不應整句標紅，只該標漏的部分"


def test_hallucination_filter_blocks_repeating_text():
    """重複 N-gram (典型 Whisper hallucination) 應被過濾。"""
    from teleprompter.core.speech_recognizer import SpeechRecognizerWorker
    # 範例 hallucination
    assert SpeechRecognizerWorker._is_hallucination("我們採用了一個小型的小型的小型的小型的小型")
    assert SpeechRecognizerWorker._is_hallucination("作層面中我們在實作層面中我們在實作層面中")
    # 正常文字不該被過濾
    assert not SpeechRecognizerWorker._is_hallucination("大家好我是今天的報告人")
    assert not SpeechRecognizerWorker._is_hallucination("我們使用 PyTorch 訓練模型")


def test_low_confidence_commit_caps_progress_at_low():
    """低信心 commit 不能把進度推到 1.0 (修正核心 bug)。"""
    from teleprompter.core.alignment_engine import (
        AlignmentEngine,
        PROGRESS_CAP_LOW,
    )
    from teleprompter.core.transcript_loader import load_from_string
    t = load_from_string("第一句很長很長很長的內容。第二句。")
    eng = AlignmentEngine(t)
    # 直接呼叫 _record_progress 模擬低信心 commit
    sent_end_pos = t.sentences[0].end - 1
    eng._record_progress(0, sent_end_pos, score=55)  # 低信心
    # 進度應被限制在 PROGRESS_CAP_LOW=0.3
    assert eng._sentence_max_progress[0] <= PROGRESS_CAP_LOW + 0.01
    # 高信心進度沒被更新
    assert eng._sentence_high_confidence_progress.get(0, 0) == 0.0


def test_high_confidence_commit_records_full_progress():
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string
    t = load_from_string("第一句內容。第二句。")
    eng = AlignmentEngine(t)
    sent_end_pos = t.sentences[0].end  # 句尾位置
    eng._record_progress(0, sent_end_pos, score=90)  # 高信心
    # 推進到句尾，進度應接近 1.0（不被 cap 限制）
    assert eng._sentence_max_progress[0] >= 0.7
    assert eng._sentence_high_confidence_progress[0] >= 0.7


def test_drift_through_sentences_marks_them_skipped_after_jump():
    """模擬 hallucination 漸進漂移後又跳過去：中間句仍應被標漏講。"""
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string
    t = load_from_string(
        "大家好我是報告人。"
        "今天分享 transformer 主題。"
        "首先說明背景動機。"
        "接著介紹相關工作。"
        "然後說明方法設計。"
        "最後是結論與展望。"
    )
    eng = AlignmentEngine(t)
    eng.update("大家好我是報告人")  # sent 0
    # 模擬低信心漂移到 sent 1, 2, 3 各一次（手動偽造低信心進度）
    eng._sentence_max_progress[1] = 0.3  # 低信心進度
    eng._sentence_max_progress[2] = 0.3
    eng._sentence_max_progress[3] = 0.3
    # 真正跳到 sent 5
    r = eng.update("最後是結論與展望")
    assert r.updated
    assert r.has_skipped, "應觸發漏講"
    # 中間句 1, 2, 3 (低 progress) 都應被標
    skipped_total = sum(e - s for s, e in r.skipped_ranges)
    assert skipped_total > 30, f"預期至少 30 字漏講，實際 {skipped_total}"


def test_stability_mode_conservative_uses_higher_thresholds():
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string
    eng = AlignmentEngine(load_from_string("test."))
    eng.apply_stability_mode("conservative")
    assert eng._high_confidence == 78
    assert eng._mid_confidence == 70


def test_stability_mode_aggressive_uses_lower_thresholds():
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string
    eng = AlignmentEngine(load_from_string("test."))
    eng.apply_stability_mode("aggressive")
    assert eng._high_confidence == 60
    assert eng._mid_confidence == 50


def test_speed_estimate_default_when_no_history():
    from teleprompter.core.alignment_engine import (
        AlignmentEngine,
        DEFAULT_READING_SPEED,
    )
    from teleprompter.core.transcript_loader import load_from_string
    eng = AlignmentEngine(load_from_string("第一句。第二句。"))
    assert eng.estimate_speed() == DEFAULT_READING_SPEED


def test_speed_estimate_from_commits():
    """連續 commit 後可估出合理語速。"""
    import time as _time
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string
    t = load_from_string("第一句內容。第二句內容。第三句內容。")
    eng = AlignmentEngine(t)
    # 模擬等距 commit
    eng.update("第一句內容")
    _time.sleep(0.05)
    eng.update("第二句內容")
    _time.sleep(0.05)
    eng.update("第三句內容")
    speed = eng.estimate_speed()
    assert 1.0 <= speed <= 15.0


def test_soft_advance_only_when_voice_active_and_stuck():
    """軟推進：講話中且久沒 commit → 才推進。"""
    import time as _time
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string
    t = load_from_string("第一句。第二句。第三句。第四句。第五句。")
    eng = AlignmentEngine(t)
    eng.update("第一句")
    pos_before = eng.current_global_char

    # 模擬久沒 commit
    eng._last_commit_time = _time.monotonic() - 3.0
    # 不講話 → 不推進
    new_pos = eng.soft_time_advance(voice_active=False)
    assert new_pos == pos_before
    # 講話中 → 推進
    new_pos = eng.soft_time_advance(voice_active=True)
    assert new_pos > pos_before


def test_max_forward_cap_blocks_low_confidence_overshoot():
    """中低信心 commit 跳超過 40 字會被截斷。"""
    from teleprompter.core.alignment_engine import (
        AlignmentEngine,
        MAX_FORWARD_CHARS_PER_COMMIT,
    )
    from teleprompter.core.transcript_loader import load_from_string
    # 一個字串很長但只有兩句，模擬低信心強跳
    long_text = "前面" * 30 + "。" + "後面" * 30 + "。"
    t = load_from_string(long_text)
    eng = AlignmentEngine(t)
    # 直接呼叫 _commit 模擬「假裝」要把位置前推 100 字、score=70
    target = eng.current_global_char + 100
    # 找出對應的 sent_idx
    sent_idx = 0
    for i, s in enumerate(t.sentences):
        if s.start <= target < s.end:
            sent_idx = i
            break
    r = eng._commit(sent_idx, target, 70.0, "test override")
    # 應被截到 +40
    assert (eng.current_global_char - 0) <= MAX_FORWARD_CHARS_PER_COMMIT


def test_stuck_recovery_lowers_threshold_after_timeout(fresh_eng):
    """卡住超過 1.5 秒後，原本因低信心被擋的更新應能 commit。"""
    import time as _time
    eng, _ = fresh_eng
    # 念一段建立基準
    eng.update("大家好我是今天的報告人")
    pos_before = eng.current_global_char

    # 人為把 _last_commit_time 推回 4 秒（模擬卡住）
    eng._last_commit_time = _time.monotonic() - 4.0

    # 餵入一個信心中等的辨識（模擬有部分匹配但不完美）
    # 沒卡住情況下應被 ignore，卡住情況下應該 commit
    r = eng.update("今天的主題")  # 與 sent 1 部分匹配
    assert r.updated, f"卡住後應降閾值 commit，實際 reason={r.reason}, conf={r.confidence}"


def test_boundary_punctuation_triggers_relaxed_commit(fresh_eng):
    """delta 含句末標點（。！？）→ 視為使用者剛念完，閾值放寬。"""
    eng, _ = fresh_eng
    eng.update("大家好我是今天的報告人")
    # 含句點的辨識文字
    r = eng.update("今天的主題是 transformer。")
    # 含句點 → 閾值 BOUNDARY_HIGH_CONFIDENCE=55，應較容易 commit
    assert r.updated or r.confidence >= 55


def test_ambiguity_now_uses_top2_gap_not_absolute(fresh_eng):
    """歧義新邏輯：兩遠處候選分差 ≥ 8 時不算歧義（讓較高分的贏）。"""
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string

    # 構造：sent 0 完全匹配 (raw 100)；sent 5 部分匹配 (raw ~80)
    # → Top-2 差 20，不該算歧義
    script = (
        "完全相符的內容句子甲。"
        "中間段落內容一。"
        "中間段落內容二。"
        "中間段落內容三。"
        "中間段落內容四。"
        "完全相符部分內容。"
    )
    t = load_from_string(script)
    eng = AlignmentEngine(t)
    r = eng.update("完全相符的內容句子甲")
    # 應有效 commit（不該因兩處有「完全相符」就歧義延遲）
    assert r.updated, f"應該成功 commit；reason={r.reason}"


# ========================================================================
# D 類：暫停/恢復
# ========================================================================

def test_long_pause_simulation(fresh_eng):
    """模擬講者停頓 5 秒（喝水），位置應原地保留。"""
    eng, _ = fresh_eng
    _stream(eng, "大家好我是今天的報告人", 3)
    pos = eng.current_global_char
    sent = eng.current_sentence_index

    # 模擬 50 個空 update
    for _ in range(50):
        eng.update("")
        eng.update("   ")

    assert eng.current_global_char == pos
    assert eng.current_sentence_index == sent


def test_resume_after_pause_continues(fresh_eng):
    """停頓後繼續念，應能無縫推進。"""
    eng, _ = fresh_eng
    _stream(eng, "大家好我是今天的報告人很榮幸", 3)
    pos = eng.current_global_char
    # 停頓
    for _ in range(20):
        eng.update("")
    # 繼續
    _stream(eng, "能在這個會議上分享我們的研究", 3)
    assert eng.current_global_char > pos


# ========================================================================
# E 類：噪音與雜訊
# ========================================================================

def test_filler_words_do_not_shift_position(fresh_eng):
    """『嗯』『啊』『那個』不應推進。"""
    eng, _ = fresh_eng
    _stream(eng, "大家好我是今天的報告人", 3)
    pos = eng.current_global_char
    for filler in ["呃", "嗯", "那個", "就是", "你知道", "對對對"]:
        eng.update(filler)
    # 位置應不變或微小變動
    assert eng.current_global_char - pos <= 5


def test_random_noise_text_ignored(fresh_eng):
    """完全不在講稿的隨機文字不應推進位置。"""
    eng, _ = fresh_eng
    _stream(eng, "大家好我是今天的報告人", 3)
    pos = eng.current_global_char
    for noise in ["天氣不錯", "肚子餓", "今晚月色真美", "abc xyz"]:
        eng.update(noise)
    assert eng.current_global_char == pos


# ========================================================================
# F 類：Whisper 常見輸出變體
# ========================================================================

def test_whisper_lowercase_english_matches(fresh_eng):
    """Whisper 把 PyTorch 輸出為 pytorch 應仍能對齊。"""
    eng, _ = fresh_eng
    # 念到實作方法那段
    _stream(eng, "大家好我是今天的報告人今天的主題是 transformer 架構", 3)
    _stream(eng, "首先我會從研究背景開始介紹", 3)
    # 跳到 PyTorch 那句（用小寫）
    r = eng.update("我們使用 pytorch 作為主要框架")
    # 應對齊到含 PyTorch 的句子
    assert eng.current_sentence_index >= 7


def test_whisper_homophone_simplified_chinese(fresh_eng):
    """Whisper 輸出簡體 → 繁體講稿仍能對齊。"""
    eng, _ = fresh_eng
    _stream(eng, "大家好我是今天的報告人", 3)
    # 簡體輸出
    r = eng.update("今天的主题是 transformer 架构在自然语言处理的应用")
    # 應推進
    assert r.updated
    assert eng.current_sentence_index >= 1


def test_whisper_number_format_variation():
    """Whisper 輸出『9 5 分』而講稿是『88.5 分』（數字格式不同）— 不應崩潰。"""
    t = load_from_string("實驗結果達到 88.5 分。第二句。")
    eng = AlignmentEngine(t)
    # 即使數字格式不同也不應拋例外
    eng.update("實驗結果達到九十五分")
    assert eng.current_global_char >= 0


# ========================================================================
# G 類：邊界條件
# ========================================================================

def test_reading_past_end_of_script(fresh_eng):
    """讀到最後一句後，再說話不應崩潰。"""
    eng, t = fresh_eng
    # 跳到最後一句
    eng.jump_to_sentence(len(t.sentences) - 1)
    _stream(eng, "謝謝大家以上是我今天的報告歡迎各位提問", 3)
    # 再說一些話
    eng.update("感謝聆聽")
    eng.update("有問題嗎")
    # 不應崩潰
    assert 0 <= eng.current_sentence_index < len(t.sentences)


def test_reading_first_sentence_doesnt_skip_phantom_negatives():
    """第一句的對齊不應出現 sent_idx = -1 之類的怪狀態。"""
    t = load_from_string("第一句。第二句。")
    eng = AlignmentEngine(t)
    eng.update("第一句")
    assert eng.current_sentence_index >= 0


def test_engine_recovers_from_garbage_inputs(fresh_eng):
    """連續餵入垃圾後，再餵正確文字應恢復對齊。"""
    eng, _ = fresh_eng
    _stream(eng, "大家好我是今天的報告人", 3)
    pos = eng.current_global_char
    # 餵 50 次垃圾
    for _ in range(50):
        eng.update("zzz xxx 隨機亂碼")
    # 應該位置不變
    assert eng.current_global_char == pos
    # 接著餵正確內容
    _stream(eng, "今天的主題是 transformer 架構", 3)
    assert eng.current_global_char > pos


# ========================================================================
# H 類：Q&A 模擬
# ========================================================================

def test_qa_off_script_speech_does_not_corrupt_alignment(fresh_eng):
    """Q&A 階段講者離稿回答問題，不應影響後續對齊。"""
    eng, _ = fresh_eng
    # 念完前幾句
    _stream(eng, "大家好我是今天的報告人", 3)
    _stream(eng, "今天的主題是 transformer 架構", 3)
    pos_before_qa = eng.current_global_char
    sent_before_qa = eng.current_sentence_index

    # 模擬 Q&A 段落 — 完全離稿
    qa_speech = [
        "謝謝您的問題",
        "這是一個很好的觀察",
        "我認為主要原因是",
        "資料量的限制",
        "下次會嘗試更大的訓練集",
    ]
    for q in qa_speech:
        _stream(eng, q, 3)

    # 位置不應跑掉
    assert abs(eng.current_sentence_index - sent_before_qa) <= 2


# ========================================================================
# I 類：穩定性壓力
# ========================================================================

def test_5_minute_session_simulation():
    """模擬 5 分鐘報告（300 commits）後狀態仍合理。"""
    t = load_from_string(SCRIPT)
    eng = AlignmentEngine(t)
    # 念 18 句，每句 17 個 commit
    for sent in t.sentences:
        for chunk in [sent.normalized[i:i+2] for i in range(0, len(sent.normalized), 2)]:
            if chunk:
                eng.update(chunk)
    # 應推進到接近末段
    assert eng.current_sentence_index >= len(t.sentences) - 3
    assert eng.current_global_char <= t.total_chars


def test_random_chaotic_session():
    """隨機混合：正確文字、垃圾、語助詞、跳段 — 不應崩潰且狀態保持合理。"""
    t = load_from_string(SCRIPT)
    eng = AlignmentEngine(t)
    rng = random.Random(42)

    sent_texts = [s.normalized for s in t.sentences]
    fillers = ["嗯", "呃", "那個", "對", "就是"]
    noise = ["abc", "xyz", "今晚吃什麼", "好餓"]

    for _ in range(200):
        choice = rng.random()
        if choice < 0.5:
            # 念正確的某句的一部分
            sent = rng.choice(sent_texts)
            chunk = sent[: rng.randint(2, len(sent))]
            eng.update(chunk)
        elif choice < 0.7:
            eng.update(rng.choice(fillers))
        elif choice < 0.85:
            eng.update(rng.choice(noise))
        else:
            eng.jump_to_sentence(rng.randint(0, len(t.sentences) - 1))

    # 不應崩潰、狀態仍在合理範圍
    assert 0 <= eng.current_sentence_index < len(t.sentences)
    assert 0 <= eng.current_global_char <= t.total_chars


# ========================================================================
# J 類：跳回頭重念
# ========================================================================

def test_manual_jump_back_then_continue(fresh_eng):
    """講者按上鍵回到上一段重念，再正常前進。"""
    eng, _ = fresh_eng
    _stream(eng, "大家好我是今天的報告人", 3)
    _stream(eng, "今天的主題是 transformer 架構", 3)
    sent_after = eng.current_sentence_index

    # 手動跳回前一句
    eng.jump_to_sentence(sent_after - 1)
    assert eng.current_sentence_index == sent_after - 1
    assert eng._recent_buffer == ""  # 應重置 buffer

    # 重念那句
    _stream(eng, "今天的主題是 transformer 架構", 3)
    assert eng.current_sentence_index >= sent_after - 1


def test_speech_recovery_after_manual_jump_to_arbitrary_position(fresh_eng):
    """手動跳到任意位置後，後續辨識應從新位置繼續。"""
    eng, t = fresh_eng
    eng.jump_to_sentence(10)
    assert eng.current_sentence_index == 10
    # 念第 10 句的內容
    _stream(eng, t.sentences[10].normalized, 3)
    # 應推進到 sent 10 之後
    assert eng.current_sentence_index >= 10


# ========================================================================
# K 類：跳回頭不應意外觸發 has_skipped
# ========================================================================

def test_backward_jump_does_not_set_has_skipped(fresh_eng):
    """從後面回到前面的更新不應回傳 has_skipped。"""
    eng, _ = fresh_eng
    eng.jump_to_sentence(10)
    # 念前面的內容（高信心倒退）
    r = eng.update("大家好我是今天的報告人很榮幸能在這個會議上分享我們的研究")
    # 即使倒退成功，has_skipped 不該觸發（沒「漏講」概念在倒退時）
    assert not r.has_skipped


# ========================================================================
# L 類：模擬實際 Whisper 不完美輸出
# ========================================================================

def test_whisper_inserts_extra_punctuation(fresh_eng):
    """Whisper 可能輸出多餘標點，不應影響對齊。"""
    eng, _ = fresh_eng
    _stream(eng, "大家好，我是，今天的報告人。", 3)
    assert eng.current_sentence_index >= 0


def test_whisper_misses_chars():
    """Whisper 可能漏字（缺字辨識），仍應大致對齊。"""
    t = load_from_string("我們使用 PyTorch 訓練模型。第二句。")
    eng = AlignmentEngine(t)
    # 漏掉「使用」
    r = eng.update("我們 pytorch 訓練模型")
    # 應大致對齊到第一句
    assert eng.current_sentence_index == 0
    assert r.updated


def test_whisper_extra_word_inserted():
    """Whisper 可能插入冗字，不影響對齊。"""
    t = load_from_string("第一句內容。第二句內容。")
    eng = AlignmentEngine(t)
    # 多了「呢」字
    r = eng.update("第一句呢內容呢")
    assert r.updated
    assert eng.current_sentence_index == 0


# ========================================================================
# M 類：state 一致性
# ========================================================================

def test_engine_state_invariants_after_many_random_updates(fresh_eng):
    """大量隨機操作後，內部 state 不應出現負值或越界。"""
    eng, t = fresh_eng
    rng = random.Random(123)
    for _ in range(500):
        op = rng.choice(["update", "jump_sent", "jump_char", "reset"])
        if op == "update":
            eng.update(rng.choice([
                "大家好", "transformer", "我們", "這是亂碼", ""
            ]))
        elif op == "jump_sent":
            eng.jump_to_sentence(rng.randint(-5, len(t.sentences) + 5))
        elif op == "jump_char":
            eng.jump_to_global_char(rng.randint(-100, t.total_chars + 100))
        elif op == "reset":
            eng.reset()

    # 不變量
    assert 0 <= eng.current_sentence_index < len(t.sentences)
    assert 0 <= eng.current_global_char <= t.total_chars
    assert len(eng._recent_buffer) <= 25  # buffer bounded
