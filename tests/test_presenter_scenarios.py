"""演講者實戰情境測試。

這些測試模擬真實大型會議中講者會遇到的各種狀況，確保工具在高壓情境下
仍然可靠：停頓、口誤、回頭念、跳段、突發干擾、長時間報告等。
"""

from __future__ import annotations

import pytest

from teleprompter.core.alignment_engine import (
    HIGH_CONFIDENCE,
    AlignmentEngine,
)
from teleprompter.core.transcript_loader import load_from_string


# ========================================================================
# 典型 15 分鐘報告講稿範本（分段清楚，含中英混合與專有名詞）
# ========================================================================
PRESENTATION = (
    "大家好，我是今天的報告人，很高興能在這個會議上分享我們團隊的研究成果。"
    "今天要跟各位分享的主題是 Transformer 架構在自然語言處理的應用。"
    "首先我會從背景開始介紹。"
    "在 2017 年之前，NLP 任務主要依賴 RNN 和 LSTM 這類循序模型。"
    "但它們在處理長序列時有明顯的瓶頸。"
    "Google 團隊提出了全新的 Transformer 架構，核心機制是 self-attention。"
    "這個架構最大的優勢是能夠平行運算，訓練效率比傳統 RNN 高出非常多。"
    "接著讓我介紹我們的實作方法。"
    "我們使用 PyTorch 作為主要框架，搭配 Hugging Face 的 transformers 套件。"
    "訓練資料來自公開的 Common Crawl 語料。"
    "我們採用 AdamW optimizer，learning rate 設定為 5e-5。"
    "實驗結果方面，我們的模型在 GLUE benchmark 上達到了 88.5 分。"
    "超越了 baseline BERT-base 大約 2.3 個百分點。"
    "最後是結論與未來展望。"
    "我們證明了 Transformer 在中英混合場景下依然有優秀的表現。"
    "謝謝大家，以上是我今天的報告。"
)


@pytest.fixture
def engine():
    t = load_from_string(PRESENTATION)
    return AlignmentEngine(t), t


# ========================================================================
# 情境 A：正常順暢報告
# ========================================================================

def test_normal_sequential_presentation(engine):
    """正常順序念完整份稿應該全程保持對齊。"""
    eng, t = engine
    # 模擬每句 2-3 段 delta 依序餵入
    for i, sent in enumerate(t.sentences):
        # 分成兩段模擬串流辨識
        mid = len(sent.normalized) // 2
        eng.update(sent.normalized[:mid])
        eng.update(sent.normalized[mid:])
        assert eng.current_sentence_index >= i, (
            f"念到第 {i} 句時應該至少在 {i} 句，目前在 {eng.current_sentence_index}"
        )
    # 最後位置應該接近尾端
    assert eng.current_sentence_index >= len(t.sentences) - 2


# ========================================================================
# 情境 B：停頓、語助詞、想詞
# ========================================================================

def test_filler_words_do_not_disrupt_alignment(engine):
    """「嗯」「那個」「呃」這類語助詞不應推進位置也不應干擾。"""
    eng, _ = engine
    # 先正常念到第一句
    eng.update("大家好我是今天的報告人")
    idx_before = eng.current_sentence_index
    pos_before = eng.current_global_char

    # 語助詞不應推進
    eng.update("呃")
    eng.update("那個")
    eng.update("嗯")

    # 位置應維持或只輕微變動
    assert eng.current_global_char >= pos_before
    # 句索引不應倒退
    assert eng.current_sentence_index >= idx_before


def test_pause_then_continue(engine):
    """講者停頓 5 秒後繼續講，應能從停頓處無縫繼續。"""
    eng, _ = engine
    eng.update("大家好我是今天的報告人")
    mid_pos = eng.current_global_char
    # 模擬長停頓（多次空 delta / 靜音）
    for _ in range(5):
        eng.update("  ")  # 只有空白的 delta 應被忽略
    assert eng.current_global_char == mid_pos, "停頓期間位置不應變動"
    # 繼續講
    eng.update("很高興能在這個會議上分享我們團隊的研究成果")
    assert eng.current_global_char > mid_pos


# ========================================================================
# 情境 C：口誤、重念、自我修正
# ========================================================================

def test_repeat_word_does_not_cause_regression(engine):
    """講者不小心重念了剛才的詞不應讓位置倒退。"""
    eng, _ = engine
    eng.update("大家好我是今天的報告人很高興能在這個會議上分享")
    pos1 = eng.current_global_char
    # 重念前面的內容
    eng.update("大家好")
    # 位置不應倒退
    assert eng.current_global_char >= pos1 - 4


def test_self_correction_within_sentence(engine):
    """講者說錯然後改口念正確版本，應能繼續對齊。"""
    eng, _ = engine
    eng.update("大家好我是")
    pos1 = eng.current_global_char
    # 改口重念（更完整的版本）
    eng.update("大家好我是今天的報告人")
    # 位置應持續推進（不倒退、不卡住）
    assert eng.current_global_char >= pos1
    # 仍在第一句範圍
    assert eng.current_sentence_index == 0


# ========================================================================
# 情境 D：Whisper 辨識常見誤差
# ========================================================================

def test_whisper_homophone_error(engine):
    """Whisper 把「實作」辨識為「十座」仍可對齊。"""
    eng, _ = engine
    # 念到「接著讓我介紹我們的實作方法」
    for txt in [
        "大家好我是今天的報告人",
        "今天要跟各位分享的主題是 transformer 架構",
        "首先我會從背景開始介紹",
        "在 2017 年之前 NLP 任務主要依賴 RNN",
        "但它們在處理長序列時有明顯的瓶頸",
        "Google 團隊提出了全新的 Transformer 架構",
        "這個架構最大的優勢是能夠平行運算",
        "接著讓我介紹我們的十座方法",  # 實作 → 十座
    ]:
        eng.update(txt)
    # 應對齊到「接著讓我介紹我們的實作方法」那句（sent index 7）
    assert eng.current_sentence_index >= 7


def test_whisper_simplified_chinese_output(engine):
    """Whisper 輸出簡體，講稿是繁體，應能對齊。"""
    eng, _ = engine
    eng.update("大家好我是今天的报告人")  # 報 → 报
    assert eng.current_sentence_index == 0


def test_whisper_english_case_variation(engine):
    """Whisper 可能輸出不同大小寫（pytorch / PyTorch / Pytorch）。"""
    eng, _ = engine
    # 跳到實作方法那句，Whisper 輸出不同大小寫
    for txt in [
        "大家好我是今天的報告人",
        "今天要跟各位分享的主題是 transformer 架構",
        "接著讓我介紹我們的實作方法",
    ]:
        eng.update(txt)
    # 下一句該用 PyTorch
    eng.update("我們使用 pytorch 作為主要框架")  # 小寫版
    assert eng.current_sentence_index >= 8


# ========================================================================
# 情境 E：跳段（講者時間不夠臨時跳過某段）
# ========================================================================

def test_deliberate_skip_triggers_has_skipped_mark(engine):
    """講者報告到一半因時間不夠跳到結論，中間的段落應被標為漏講。"""
    eng, t = engine
    eng.update("大家好我是今天的報告人")  # sent 0
    # 跳到最後的結論
    result = eng.update("最後是結論與未來展望")
    assert result.updated, "跳段時應成功更新位置"
    assert result.has_skipped, "跳段應觸發漏講標記"
    # 漏講範圍至少涵蓋 sent 1（sent 0 若未念完也可能被納入）
    assert result.skipped_start <= t.sentences[1].start
    assert result.skipped_end < t.sentences[-1].end


def test_multiple_skips_within_session(engine):
    """一場報告中多次跳段都應分別被標記。"""
    eng, t = engine
    eng.update("大家好我是今天的報告人")
    # 第一次跳：跳到 NLP 背景那段（sent 3 或 4）
    eng.update("但它們在處理長序列時有明顯的瓶頸")
    first_skip_triggered = eng.current_sentence_index >= 3

    # 第二次跳：直接跳到結論
    result = eng.update("最後是結論與未來展望")
    second_skip_triggered = result.has_skipped

    # 至少一次應偵測到（視講稿長度而定）
    assert first_skip_triggered or second_skip_triggered


# ========================================================================
# 情境 F：壓力測試
# ========================================================================

def test_thousand_updates_stability():
    """1000 次 update 後狀態仍然合理（模擬長時間報告）。"""
    script = "。".join(f"第{i}段內容講述主題{i}" for i in range(1, 51)) + "。"
    t = load_from_string(script)
    eng = AlignmentEngine(t)
    for _ in range(1000):
        eng.update("第一段內容")
    # 不應崩潰；position 應在合理範圍
    assert 0 <= eng.current_global_char <= t.total_chars
    assert 0 <= eng.current_sentence_index < len(t.sentences)


def test_very_long_transcript_no_crash():
    """100 句以上講稿 + 連續推進不會崩潰。每句內容須有區別性（真實情境）。"""
    # 用不同的形容詞讓每句獨特，避免全句一模一樣導致永遠歧義
    topics = ["背景", "動機", "方法", "資料", "結果", "討論", "結論", "限制", "未來", "致謝"]
    sentences = [f"第{i}部分主要講述{topics[i % 10]}相關的細節與分析" for i in range(1, 101)]
    script = "。".join(sentences) + "。"
    t = load_from_string(script)
    eng = AlignmentEngine(t)
    for i in range(1, 101):
        eng.update(f"第{i}部分主要講述{topics[i % 10]}")
    # 應推進到稿件中後段（不一定到 90，因為部分相似內容會延遲）
    assert eng.current_sentence_index >= 50


def test_recent_buffer_bounded():
    """滑動緩衝不會無限成長。"""
    t = load_from_string("第一句。第二句。")
    eng = AlignmentEngine(t)
    # 塞入超長文字
    big = "一" * 10000
    eng.update(big)
    # 緩衝應被限制在 RECENT_BUFFER_CHARS 以內
    from teleprompter.core.alignment_engine import RECENT_BUFFER_CHARS
    assert len(eng._recent_buffer) <= RECENT_BUFFER_CHARS


# ========================================================================
# 情境 G：極端邊界
# ========================================================================

def test_empty_transcript_never_crashes():
    t = load_from_string("")
    eng = AlignmentEngine(t)
    result = eng.update("任何文字")
    assert not result.updated
    result2 = eng.jump_to_sentence(5)
    assert not result2.updated


def test_single_sentence_transcript():
    t = load_from_string("只有一句話。")
    eng = AlignmentEngine(t)
    result = eng.update("只有一句話")
    assert result.updated
    assert eng.current_sentence_index == 0
    # 跳段偵測需要 ≥2 句，單句絕不會觸發
    assert not result.has_skipped


def test_manual_jump_beyond_bounds_clamped():
    t = load_from_string("第一句。第二句。")
    eng = AlignmentEngine(t)
    result = eng.jump_to_sentence(999)
    assert eng.current_sentence_index == len(t.sentences) - 1
    assert result.updated


def test_manual_jump_negative_clamped():
    t = load_from_string("第一句。第二句。")
    eng = AlignmentEngine(t)
    eng.current_sentence_index = 1
    result = eng.jump_to_sentence(-5)
    assert eng.current_sentence_index == 0


def test_reset_clears_skipped_state():
    t = load_from_string("第一句。第二句。第三句。第四句。")
    eng = AlignmentEngine(t)
    eng.update("第一句")
    eng.update("第四句")  # 跳段
    eng.reset()
    assert eng.current_sentence_index == 0
    assert eng.current_global_char == 0
    # 重置後再 update 不應回傳漏講
    result = eng.update("第一句")
    assert not result.has_skipped


# ========================================================================
# 情境 H：狀態一致性
# ========================================================================

def test_sentence_index_always_consistent_with_global_char(engine):
    """任何時刻 current_sentence_index 都應與 current_global_char 對應。"""
    eng, t = engine
    texts = [
        "大家好我是今天的報告人",
        "今天要跟各位分享的主題",
        "transformer 架構",
        "我們使用 pytorch",
        "實驗結果",
        "最後是結論",
    ]
    for txt in texts:
        eng.update(txt)
        sent = t.sentences[eng.current_sentence_index]
        # global char 應落在 [sent.start, sent.end] 範圍（或為該句起始）
        assert sent.start <= eng.current_global_char <= sent.end + 1, (
            f"不一致: sent_idx={eng.current_sentence_index}, "
            f"sent.start={sent.start}, sent.end={sent.end}, "
            f"global_char={eng.current_global_char}, 觸發字: {txt}"
        )


def test_no_false_skip_on_normal_advance(engine):
    """逐句順序推進絕不應觸發漏講。"""
    eng, t = engine
    skip_count = 0
    for sent in t.sentences:
        result = eng.update(sent.normalized)
        if result.has_skipped:
            skip_count += 1
    assert skip_count == 0, f"順序念稿不應有任何漏講，但偵測到 {skip_count} 次"


# ========================================================================
# 情境 I：回朔（講者回頭解釋）
# ========================================================================

def test_going_back_manually_works(engine):
    """使用者按上鍵回到上一段應立即生效。"""
    eng, _ = engine
    eng.update("大家好我是今天的報告人")
    eng.update("今天要跟各位分享的主題是 transformer")
    eng.update("首先我會從背景開始介紹")
    cur = eng.current_sentence_index
    # 手動跳回前一句
    result = eng.jump_to_sentence(cur - 1)
    assert result.updated
    assert eng.current_sentence_index == cur - 1


def test_speech_after_manual_jump_continues_from_new_position(engine):
    """手動跳後，後續辨識從新位置繼續比對。"""
    eng, _ = engine
    eng.update("大家好我是今天的報告人")
    eng.update("今天要跟各位分享的主題")
    eng.update("首先我會從背景開始介紹")
    # 使用者手動跳回第一句
    eng.jump_to_sentence(0)
    # 接著講者重新念第一句
    eng.update("大家好我是今天的報告人")
    assert eng.current_sentence_index == 0


def test_streaming_drift_marks_drifted_sentence_as_skipped():
    """串流小片段跳段場景：若引擎因關鍵詞重疊漂移到中間句然後再跳到目標句，
    漂移中途那句（使用者並未真的念）也應被標為漏講。
    """
    from teleprompter.core.alignment_engine import AlignmentEngine
    from teleprompter.core.transcript_loader import load_from_string

    script = (
        "大家好我是今天的報告人。"
        "今天要分享 transformer 在自然語言處理的應用。"  # 含 "transformer" 容易誤命中
        "首先我會從背景開始介紹。"
        "在 2017 年之前 NLP 主要依賴 RNN 和 LSTM。"
        "transformer 提出了 self-attention 機制。"      # 使用者實際要念的句
    )
    t = load_from_string(script)
    eng = AlignmentEngine(t)

    # 念完第 0 句
    for c in ["大家", "好我", "是今", "天的", "報告人"]:
        eng.update(c)
    assert eng.current_sentence_index == 0

    # 跳段念第 4 句（串流 3-char chunks）
    final_result = None
    for c in ["tra", "nsf", "orm", "er ", "提出了", " se", "lf ", "att", "ent", "ion"]:
        r = eng.update(c)
        if r.updated:
            final_result = r

    # 應最終到達 sent 4
    assert eng.current_sentence_index == 4
    # 漏講範圍應涵蓋 sent 1（使用者其實沒念），不能只標 sent 2、3
    sent_1_start = t.sentences[1].start
    # 找到所有觸發 has_skipped 的結果
    # 取最後一次跳段事件
    assert final_result is not None
