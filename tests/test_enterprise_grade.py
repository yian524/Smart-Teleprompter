"""企業級驗收 — 30 年提詞機團隊壓力測試。

每個測試都模擬真實大型會議場景，必須 100% 通過才能交付。
"""

from __future__ import annotations

import os
import time as _time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from teleprompter.core.alignment_engine import AlignmentEngine
from teleprompter.core.transcript_loader import load_from_string


# ============================================================
# 真實會議報告講稿
# ============================================================
SCRIPT = (
    "大家好，我是今天的報告人，很高興能在這個會議上分享我們的研究成果。"
    "今天的主題是 Transformer 架構在自然語言處理的應用。"
    "首先讓我從研究背景開始介紹。"
    "在 2017 年之前，NLP 任務主要依賴 RNN 和 LSTM 這類循序模型。"
    "但是它們在處理長序列時有明顯的瓶頸。"
    "Google 團隊在 Attention Is All You Need 這篇論文中提出了 Transformer 架構。"
    "核心機制是 self-attention，能夠平行運算大幅提升訓練效率。"
    "接著介紹我們的實作方法。"
    "我們使用 PyTorch 作為主要框架，搭配 Hugging Face 的 transformers 套件。"
    "訓練資料來自公開的 Common Crawl 語料。"
    "我們的模型在 GLUE benchmark 上達到了 88.5 分。"
    "超越了 baseline BERT-base 大約 2.3 個百分點。"
    "最後是結論與未來展望。"
    "我們證明了 Transformer 架構在中英混合場景下依然有優秀的表現。"
    "謝謝大家，以上是我今天的報告，歡迎各位提問。"
)


@pytest.fixture
def fresh():
    t = load_from_string(SCRIPT)
    return AlignmentEngine(t), t


# ============================================================
# 情境 1：完整逐句念稿（最常見）
# ============================================================
def test_E01_sequential_full_reading_no_false_skip(fresh):
    eng, t = fresh
    skip_count = 0
    for sent in t.sentences:
        r = eng.update(sent.normalized)
        if r.has_skipped:
            skip_count += 1
    # 完整念稿不應觸發任何跳段（容忍 ≤ 2 次極少數誤判）
    assert skip_count <= 2, f"完整念稿不應觸發漏講，實際 {skip_count}"
    # 應推進到末段
    assert eng.current_sentence_index >= len(t.sentences) - 2


# ============================================================
# 情境 2：Whisper 自動加標點不影響匹配
# ============================================================
def test_E02_whisper_added_punctuation_does_not_break_alignment(fresh):
    eng, _ = fresh
    # Whisper 對停頓自動加句點，使用者實際只說「大家好我是今天的報告人」
    r = eng.update("大家好。我是今天的報告人。")
    assert r.updated, "標點不該破壞匹配"
    assert eng.current_sentence_index == 0


# ============================================================
# 情境 3：Whisper 同音誤辨（核心修正驗證）
# ============================================================
def test_E03_whisper_homophone_error_does_not_mark_skipped():
    """講稿是「實作方法」，Whisper 輸出「十座方法」→ 不應標漏講。"""
    t = load_from_string("接著介紹我們的實作方法。下一段是結論。")
    eng = AlignmentEngine(t)
    # 念第一句但 Whisper 誤辨「實作」為「十座」
    r = eng.update("接著介紹我們的十座方法")
    # 推進到 sent 1 觸發 sent 0 的檢查
    r2 = eng.update("下一段是結論")
    # 「實作」位置應被 phoneme_match 保護，不該被標漏講
    if r2.has_skipped:
        # 即使有標也不該標到「實作」那兩個字
        for s, e in r2.skipped_ranges:
            assert "實作" not in t.full_text[s:e], (
                f"同音誤辨不該標漏講，但 [{s},{e}) = {t.full_text[s:e]!r}"
            )


# ============================================================
# 情境 4：句中漏 1-2 個字（精細漏講）
# ============================================================
def test_E04_missing_short_word_in_middle_marks_red():
    """念了「我們今天討論方法的應用」漏掉「深度學習」(4 字) → 該段應標紅。"""
    t = load_from_string("我們今天討論深度學習方法的應用。下一段是結論。")
    eng = AlignmentEngine(t)
    eng.update("我們今天討論方法的應用")  # 漏「深度學習」
    r = eng.update("下一段是結論")
    assert r.has_skipped, "句中漏字應觸發漏講"
    skipped_text = ""
    for s, e in r.skipped_ranges:
        skipped_text += t.full_text[s:e]
    assert "深度學習" in skipped_text or "學習" in skipped_text, (
        f"應標到漏掉的「深度學習」，實際標到 {skipped_text!r}"
    )


def test_E05_missing_two_chars_marks_red():
    """念了「使用框架」漏掉「PyTorch」(7 字) → 該段標紅。"""
    t = load_from_string("我們使用 PyTorch 框架做訓練。下一句結論。")
    eng = AlignmentEngine(t)
    eng.update("我們使用框架做訓練")  # 漏「PyTorch」
    r = eng.update("下一句結論")
    assert r.has_skipped
    skipped_text = "".join(t.full_text[s:e] for s, e in r.skipped_ranges)
    assert "pytorch" in skipped_text.lower() or "PyTorch" in skipped_text


# ============================================================
# 情境 5：跳段（明確跳到後面段落）
# ============================================================
def test_E06_explicit_skip_to_far_sentence_marks_intermediate(fresh):
    eng, t = fresh
    eng.update("大家好我是今天的報告人")
    # 直接跳到結論
    r = eng.update("謝謝大家以上是我今天的報告歡迎各位提問")
    assert r.has_skipped, "跳段必須觸發漏講"
    # 應標大量內容
    total = sum(e - s for s, e in r.skipped_ranges)
    assert total > 200, f"跳大段應標 > 200 字，實際 {total}"


# ============================================================
# 情境 6：完全靜音 / 無相關內容不應推進
# ============================================================
def test_E07_silence_and_filler_do_not_advance(fresh):
    eng, _ = fresh
    eng.update("大家好我是今天的報告人")
    pos_before = eng.current_global_char
    for noise in ["", "  ", "嗯", "呃", "那個", "對對對", "天氣很好"]:
        eng.update(noise)
    # 位置不應前進超過 5 字
    assert eng.current_global_char - pos_before <= 5


# ============================================================
# 情境 7：Q&A 離稿回答不破壞對齊
# ============================================================
def test_E08_qa_off_script_preserves_alignment(fresh):
    eng, _ = fresh
    eng.update("大家好我是今天的報告人")
    pos_before = eng.current_global_char
    # 離稿回答觀眾問題
    for q in [
        "謝謝您的提問",
        "這是個很好的觀察",
        "我認為主要原因是資料量限制",
        "下次會嘗試更大的訓練集",
    ]:
        eng.update(q)
    # 位置應保持在合理範圍
    assert abs(eng.current_global_char - pos_before) <= 50


# ============================================================
# 情境 8：手動回頭重念
# ============================================================
def test_E09_manual_jump_back_then_continue(fresh):
    eng, _ = fresh
    eng.update("大家好我是今天的報告人")
    eng.update("今天的主題是 transformer")
    cur = eng.current_sentence_index
    # 按上鍵回到上一句
    eng.jump_to_sentence(cur - 1)
    assert eng.current_sentence_index == cur - 1
    # 重念那句
    eng.update("今天的主題是 transformer")
    assert eng.current_sentence_index >= cur - 1


# ============================================================
# 情境 9：長時間穩定（10 分鐘等效 = 200 commits）
# ============================================================
def test_E10_long_session_stable():
    t = load_from_string(SCRIPT)
    eng = AlignmentEngine(t)
    # 模擬念稿 200 次（每次小 chunk）
    for _ in range(10):
        for sent in t.sentences:
            chunks = [sent.normalized[i:i+5] for i in range(0, len(sent.normalized), 5)]
            for chunk in chunks:
                if chunk.strip():
                    eng.update(chunk)
        eng.reset()
    # 應能正常 reset 不崩潰
    assert eng.current_sentence_index == 0
    assert len(eng._recent_buffer) == 0


# ============================================================
# 情境 10：穩定性模式 — Conservative 真的更嚴格
# ============================================================
def test_E11_conservative_mode_higher_thresholds():
    eng = AlignmentEngine(load_from_string("test."))
    eng.apply_stability_mode("conservative")
    assert eng._high_confidence >= 75
    assert eng._mid_confidence >= 65


# ============================================================
# 情境 11：時間穩定性 — 卡住自救可解鎖
# ============================================================
def test_E12_stuck_recovery_unblocks_after_threshold(fresh):
    eng, _ = fresh
    eng.update("大家好我是")
    # 模擬卡 4 秒
    eng._last_commit_time = _time.monotonic() - 4.0
    # 給一個中等信心的辨識（無卡住路徑下會被擋）
    r = eng.update("今天的主題")
    # 卡住路徑下應 commit
    assert r.updated, f"卡 4 秒後應降閾值 commit，實際 reason={r.reason}"


# ============================================================
# 情境 12：拼音保護不會誤把跳段當同音
# ============================================================
def test_E13_phoneme_protection_does_not_mask_real_skip(fresh):
    """跳到不同內容不該因偶有同音字就被視為念過。"""
    eng, t = fresh
    eng.update("大家好我是今天的報告人")
    # 跳到很後面（內容完全不同）
    r = eng.update("謝謝大家以上是我今天的報告歡迎各位提問")
    assert r.has_skipped


# ============================================================
# 情境 13：state 不變量
# ============================================================
def test_E14_state_invariants_after_random_ops(fresh):
    import random
    eng, t = fresh
    rng = random.Random(42)
    for _ in range(300):
        op = rng.choice(["upd_correct", "upd_noise", "jump", "reset"])
        if op == "upd_correct":
            sent = rng.choice(t.sentences)
            chunk = sent.normalized[: rng.randint(2, len(sent.normalized))]
            eng.update(chunk)
        elif op == "upd_noise":
            eng.update(rng.choice(["嗯", "abc", "天氣不錯", "1234"]))
        elif op == "jump":
            eng.jump_to_sentence(rng.randint(0, len(t.sentences) - 1))
        elif op == "reset":
            eng.reset()
    # 不變量
    assert 0 <= eng.current_sentence_index < len(t.sentences)
    assert 0 <= eng.current_global_char <= t.total_chars
    assert len(eng._recent_buffer) <= 60
    assert all(0 <= idx < len(t.sentences) for idx in eng._sentence_max_progress)


# ============================================================
# 情境 14：Whisper 標點插入不誤觸發跳段
# ============================================================
def test_E15_whisper_punctuation_in_middle_does_not_jump(fresh):
    eng, _ = fresh
    eng.update("大家好我是今天的報告人")
    pos_before = eng.current_global_char
    sent_before = eng.current_sentence_index
    # Whisper 在中間吐了句點然後繼續
    eng.update("。。。今天的主題是 transformer")
    # 應正常推進到 sent 1，不該因標點誤觸發跳段
    assert eng.current_sentence_index <= sent_before + 1


# ============================================================
# 情境 15：高信心進度防護（hallucination 推不動位置）
# ============================================================
def test_E16_hallucination_simulation_does_not_corrupt_state(fresh):
    eng, _ = fresh
    eng.update("大家好我是今天的報告人")
    pos_before = eng.current_global_char
    # 模擬連續餵入 hallucination 樣式文字
    for _ in range(10):
        eng.update("我們採用了一個小型的小型的小型的小型的小型")
    # hallucination 不該造成位置大幅前進
    advance = eng.current_global_char - pos_before
    assert advance <= 30, f"hallucination 不該推 30 字以上，實際 {advance}"


# ============================================================
# 情境 16：實戰 — 部分念 + 跳段 + 回頭
# ============================================================
def test_E19_short_word_jump_command_works():
    """念短詞「歡迎」應能跳到「歡迎各位提問」（即使距離很遠）。

    場景：使用者只念「歡迎」，引擎應辨識為跳段指令，移到含「歡迎」的句子。
    """
    t = load_from_string(
        "大家好。"
        "今天分享主題。"
        "首先介紹背景。"
        "接著說明方法。"
        "然後展示結果。"
        "最後是結論。"
        "歡迎各位提問。"
    )
    eng = AlignmentEngine(t)
    eng.update("大家好")  # 在 sent 0
    # 念「歡迎」（2 字，距離 6 句的大跳段）
    r = eng.update("歡迎")
    # 期望：能跳到含「歡迎」的句子（sent 6）
    assert eng.current_sentence_index >= 5, (
        f"短詞「歡迎」應能跳到「歡迎各位提問」，實際 sent={eng.current_sentence_index}"
    )


def test_E21_exception_safety_update_never_crashes(fresh):
    """update() 即使遇到內部異常也不該讓呼叫者崩潰。"""
    eng, _ = fresh
    # 餵入各種可能觸發內部 bug 的異常輸入
    weird_inputs = [None, "", "   ", "\x00\x01", "🎉" * 100, "a" * 10000]
    for inp in weird_inputs:
        try:
            if inp is None:
                continue  # None 會在更上層被擋
            r = eng.update(inp)
            assert r is not None
        except Exception as e:
            pytest.fail(f"update({inp!r}) 不應拋例外: {e}")


def test_E23_max_forward_sentences_limits_jump():
    """設定 max_forward_sentences=3 後，開頭說「結尾句」不應跳到末段。"""
    t = load_from_string(
        "第一句。第二句。第三句。第四句。第五句。"
        "第六句。第七句。第八句。第九句。結尾專屬句。"
    )
    eng = AlignmentEngine(t)
    eng.set_max_forward_range(max_sentences=3)
    eng.update("第一句")
    # 試圖跳到第 10 句（距離 9）
    r = eng.update("結尾專屬句")
    # 應被擋下，位置不該跳到 sent 9
    assert eng.current_sentence_index < 5, (
        f"max_forward=3 應擋下大跳段，實際 sent={eng.current_sentence_index}"
    )


def test_E24_no_limit_allows_full_range():
    """max_forward=0 (不限制) 時允許跳到任意位置。"""
    t = load_from_string(
        "第一句。第二句。第三句。第四句。第五句。"
        "第六句。第七句。第八句。第九句。結尾專屬句。"
    )
    eng = AlignmentEngine(t)
    eng.set_max_forward_range(max_sentences=0)
    eng.update("第一句")
    r = eng.update("結尾專屬句")
    # 無限制 → 應成功跳到 sent 9
    assert eng.current_sentence_index >= 8


def test_E25_same_sentence_big_jump_marks_skipped():
    """同句內大跳進（使用者跳過句中好幾字）應即時標紅。"""
    t = load_from_string("這是一個很長很長很長的句子包含許多內容需要仔細閱讀。")
    eng = AlignmentEngine(t)
    # 先念句首
    eng.update("這是一個")
    # 跳到句尾念（跨過「很長很長很長的句子包含許多內容」）
    r = eng.update("仔細閱讀")
    # 前進超過 10 字 → 應觸發同句即時標紅
    if r.updated:
        skipped_text = "".join(t.full_text[s:e] for s, e in r.skipped_ranges)
        assert len(skipped_text) > 0, "同句大跳應即時標紅"


def test_E22_constants_ordering_no_inverted_logic():
    """關鍵閾值常數順序必須合理，避免邏輯倒序。"""
    from teleprompter.core.alignment_engine import (
        HIGH_CONFIDENCE,
        MID_CONFIDENCE,
        BOUNDARY_HIGH_CONFIDENCE,
        STUCK_SOFT_HIGH_CONFIDENCE,
        STUCK_HARD_HIGH_CONFIDENCE,
        STUCK_SOFT_MID_CONFIDENCE,
    )
    # 一般 HIGH > 其他所有降低閾值
    assert HIGH_CONFIDENCE > BOUNDARY_HIGH_CONFIDENCE
    assert HIGH_CONFIDENCE > STUCK_SOFT_HIGH_CONFIDENCE
    assert HIGH_CONFIDENCE > STUCK_HARD_HIGH_CONFIDENCE
    # 軟卡 >= 硬卡
    assert STUCK_SOFT_HIGH_CONFIDENCE >= STUCK_HARD_HIGH_CONFIDENCE
    # MID > 卡住 MID
    assert MID_CONFIDENCE > STUCK_SOFT_MID_CONFIDENCE
    # HIGH 和 MID 有合理差距（至少 5 分）
    assert HIGH_CONFIDENCE - MID_CONFIDENCE >= 5


def test_E20_short_word_with_punctuation_from_whisper():
    """Whisper 輸出「歡迎。」應與「歡迎各位提問」匹配（標點被剝除）。"""
    from teleprompter.core.speech_recognizer import SpeechRecognizerWorker
    # 模擬 Whisper 輸出含標點的短詞
    raw = "歡迎。"
    cleaned = SpeechRecognizerWorker._strip_punctuation(raw)
    assert cleaned == "歡迎"
    # 接著測對齊
    t = load_from_string("第一句內容。歡迎各位提問。")
    eng = AlignmentEngine(t)
    eng.update("第一句內容")
    r = eng.update(cleaned)  # 餵入剝除標點後的「歡迎」
    assert eng.current_sentence_index == 1, (
        f"剝標點後的「歡迎」應匹配第二句，實際 sent={eng.current_sentence_index}"
    )


def test_E18_jump_into_middle_of_next_sentence_marks_pre_cursor():
    """使用者念完 sent 0 的「大家好」後，直接跳到 sent 1 中段的「自然語言處理」。
    sent 1 開頭到「自然語言處理」之前的內容應該被標紅（使用者跳過）。
    """
    t = load_from_string(
        "大家好我是今天的報告人。"
        "今天要分享主題是 Transformer 架構在自然語言處理的應用。"
        "下一段是結論。"
    )
    eng = AlignmentEngine(t)
    eng.update("大家好")  # 只念前面
    # 跳到 sent 1 中段念「自然語言處理」
    r = eng.update("自然語言處理")
    assert r.has_skipped, "跳到中段應觸發漏講標記"
    skipped_text = "".join(t.full_text[s:e] for s, e in r.skipped_ranges)
    # 應標到 sent 1 開頭的「今天要分享主題是 Transformer 架構在」
    assert "Transformer" in skipped_text or "架構" in skipped_text, (
        f"sent 1 開頭未念部分應標紅，實際 skipped={skipped_text!r}"
    )


def test_E17_realistic_partial_skip_back_workflow(fresh):
    eng, _ = fresh
    # 念 sent 0 一半
    eng.update("大家好我是")
    # 跳到結論
    r1 = eng.update("謝謝大家以上是我今天的報告")
    assert r1.has_skipped
    # 回頭重新從中段念
    eng.jump_to_sentence(7)
    eng.update("接著介紹我們的實作方法")
    assert eng.current_sentence_index >= 7
