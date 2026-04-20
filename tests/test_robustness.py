"""魯棒性測試 — 模擬真實報告中的異常與錯誤情境。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from teleprompter.config import AppConfig, load_config, save_config
from teleprompter.core.alignment_engine import AlignmentEngine
from teleprompter.core.transcript_loader import (
    Transcript,
    load_from_string,
    load_transcript,
    normalize_text,
    split_sentences,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ============================================================
# 講稿載入魯棒性
# ============================================================

def test_unicode_fullwidth_punctuation(tmp_path: Path):
    """全形標點符號應正確處理。"""
    text = "這是第一句。這是第二句！這是第三句？"
    f = tmp_path / "fw.txt"
    f.write_text(text, encoding="utf-8")
    t = load_transcript(f)
    assert len(t.sentences) == 3


def test_mixed_zh_en_punctuation(tmp_path: Path):
    """中英標點混用。"""
    text = "Hello world. 這是中文。Mixed sentence! 完了？"
    f = tmp_path / "mix.txt"
    f.write_text(text, encoding="utf-8")
    t = load_transcript(f)
    assert len(t.sentences) >= 3


def test_very_long_single_sentence(tmp_path: Path):
    """無標點的超長段落應作為一個句子處理。"""
    text = "這是" + "一個非常非常非常" * 100 + "長的句子"
    f = tmp_path / "long.txt"
    f.write_text(text, encoding="utf-8")
    t = load_transcript(f)
    assert len(t.sentences) == 1


def test_empty_lines_in_transcript(tmp_path: Path):
    """空行不應產生空句。"""
    text = "第一句。\n\n\n第二句。\n\n第三句。"
    f = tmp_path / "empty.txt"
    f.write_text(text, encoding="utf-8")
    t = load_transcript(f)
    assert len(t.sentences) == 3
    for s in t.sentences:
        assert s.normalized != ""


def test_only_punctuation_returns_no_sentences(tmp_path: Path):
    """純標點檔案應返回 0 句（不崩潰）。"""
    f = tmp_path / "punct.txt"
    f.write_text("。。。！！！？？？", encoding="utf-8")
    t = load_transcript(f)
    assert len(t.sentences) == 0


def test_emoji_in_transcript(tmp_path: Path):
    """Emoji 不應導致崩潰（雖然 Whisper 通常不會輸出 emoji）。"""
    text = "今天的主題很有趣 🎉。讓我們開始 🚀。"
    f = tmp_path / "emoji.txt"
    f.write_text(text, encoding="utf-8")
    t = load_transcript(f)
    assert len(t.sentences) == 2


def test_normalize_preserves_no_invalid_state():
    """各種奇怪輸入的 normalize 都不應拋例外。"""
    weird_inputs = [
        "", "   ", "\n\n", "\t\t", "。。。",
        "🎉🚀", "ＡＢＣ１２３", "中Eng混", "!@#$%^&*()",
        "\\n\\r\\t", "1234567890",
    ]
    for inp in weird_inputs:
        out = normalize_text(inp)
        # 只驗證不拋例外，輸出格式合理即可
        assert isinstance(out, str)


def test_sentences_indices_consistent(tmp_path: Path):
    """每個 Sentence 的 start/end 應與全文 char 索引對齊。"""
    text = "第一句內容。第二句內容！第三句內容？"
    t = load_from_string(text)
    for s in t.sentences:
        # 全文中 [start, end] 範圍的文字應等於句子文字
        assert text[s.start : s.end] == s.text


# ============================================================
# 對齊引擎異常輸入
# ============================================================

def test_alignment_handles_emoji_input():
    t = load_from_string("第一句。第二句。")
    eng = AlignmentEngine(t)
    result = eng.update("🎉🚀😀")
    # emoji 標準化後為空 → 不應更新
    assert not result.updated


def test_alignment_handles_pure_whitespace():
    t = load_from_string("第一句。第二句。")
    eng = AlignmentEngine(t)
    result = eng.update("    \t\n   ")
    assert not result.updated


def test_alignment_handles_extremely_long_input():
    t = load_from_string("第一句。第二句。")
    eng = AlignmentEngine(t)
    # 100k 字輸入不應崩潰
    long_text = "第一句" * 10000
    result = eng.update(long_text)
    # 結果合理：不崩潰，分數合理
    assert result.confidence >= 0


def test_alignment_handles_mostly_punctuation():
    t = load_from_string("第一句。第二句。")
    eng = AlignmentEngine(t)
    result = eng.update("。。。！！！，，，")
    assert not result.updated  # 純標點不應推進


def test_alignment_handles_only_english_when_script_chinese():
    """講稿純中文，使用者意外講英文不應崩潰。"""
    t = load_from_string("第一句中文。第二句中文。")
    eng = AlignmentEngine(t)
    result = eng.update("hello world this is english")
    # 應該不更新（低信心）但不崩潰
    assert isinstance(result.updated, bool)


def test_alignment_handles_numbers_as_recognized():
    """Whisper 可能把「百分之九十五」辨識為「95%」。"""
    t = load_from_string("準確率達到百分之九十五。")
    eng = AlignmentEngine(t)
    # 數字不會因為格式不同而崩潰
    eng.update("準確率達到 95")
    # 不一定 match，但不崩潰
    assert eng.current_global_char >= 0


# ============================================================
# 配置持久化
# ============================================================

def test_config_default_values(qapp):
    """預設值應合理。"""
    cfg = AppConfig()
    assert cfg.font_size >= 16
    assert cfg.target_duration_sec > 0
    assert cfg.model_size in (
        "tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"
    )


def test_config_save_load_roundtrip(qapp, tmp_path):
    """save → load 後值應該保留。"""
    cfg = AppConfig(font_size=48, target_duration_sec=1234)
    save_config(cfg)
    loaded = load_config()
    assert loaded.font_size == 48
    assert loaded.target_duration_sec == 1234


def test_config_milestones_tuple_roundtrip(qapp):
    """里程碑 tuple 轉字串再讀回應該還原。"""
    cfg = AppConfig(milestone_marks_sec=(600, 300, 60))
    save_config(cfg)
    loaded = load_config()
    assert loaded.milestone_marks_sec == (600, 300, 60)


# ============================================================
# 對齊引擎連續穩定性（模擬 30 分鐘報告 = 約 600 commit）
# ============================================================

def test_30min_simulated_session_no_degradation():
    """模擬 30 分鐘報告（600 commits）狀態仍正確。每句須有獨特內容（真實情境）。"""
    # 每句用真正不同的內容，避免關鍵詞重複造成永遠歧義
    unique_sentences = [
        "大家好我是今天的報告人",
        "今天要分享研究成果",
        "首先介紹背景動機",
        "接著說明相關工作",
        "然後闡述問題定義",
        "我們的方法設計基於注意力機制",
        "實驗資料來自公開語料庫",
        "訓練環境使用 PyTorch 框架",
        "實驗結果超越基準模型",
        "錯誤分析發現三類問題",
        "效能評估在多個測試集進行",
        "參數敏感度分析也有進行",
        "消融實驗驗證各元件貢獻",
        "視覺化結果可看出注意力分布",
        "跟以前的工作有幾點不同",
        "我們採用了全新的訓練策略",
        "模型規模比先前小 30%",
        "推論速度快了將近兩倍",
        "記憶體使用量也下降",
        "適合部署在邊緣裝置上",
        "在中文任務表現特別好",
        "跨語言遷移也有效果",
        "我們的貢獻有三點",
        "第一是新架構提出",
        "第二是大規模實驗",
        "第三是開源實作",
        "限制是計算資源仍高",
        "未來會嘗試知識蒸餾",
        "也會研究動態稀疏化",
        "感謝大家聆聽",
    ]
    script = "。".join(unique_sentences) + "。"
    t = load_from_string(script)
    eng = AlignmentEngine(t)

    for sent in t.sentences:
        chunks = [sent.normalized[j:j+3] for j in range(0, len(sent.normalized), 3)]
        for chunk in chunks:
            if chunk:
                eng.update(chunk)

    # 應推進到中後段（不需要完全到最後，因為有些句子可能歧義延遲）
    assert eng.current_sentence_index >= len(t.sentences) // 3
    from teleprompter.core.alignment_engine import RECENT_BUFFER_CHARS
    assert len(eng._recent_buffer) <= RECENT_BUFFER_CHARS


# ============================================================
# 跳段次數累積測試
# ============================================================

def test_many_skip_events_in_session():
    """報告中多次跳段都應正確標記。"""
    sentences = [
        "大家好我是報告人",
        "首先介紹研究背景",
        "接著討論相關工作",
        "現在說明方法設計",
        "下一步是實驗環境",
        "我們使用 PyTorch 框架",
        "資料集是 GLUE benchmark",
        "實驗結果非常理想",
        "錯誤分析有三個重點",
        "效能評估顯示優勢",
        "與基準比較有顯著改善",
        "結論是這個方法有效",
        "未來工作有三個方向",
        "感謝聆聽歡迎提問",
    ]
    script = "。".join(sentences) + "。"
    t = load_from_string(script)
    eng = AlignmentEngine(t)

    skip_count = 0
    # 順序念第 0, 4, 8, 12 句（每次跳 4 句）
    for target in [0, 4, 8, 12]:
        result = eng.update(sentences[target])
        if result.updated and result.has_skipped:
            skip_count += 1

    # target 4, 8, 12 三次都從前個位置跳 4 句 → 至少 3 次跳段
    assert skip_count >= 3, f"預期 ≥ 3 次跳段被偵測，實際 {skip_count}"


# ============================================================
# 讀檔案編碼魯棒性
# ============================================================

def test_load_big5_encoded_file(tmp_path: Path):
    """嘗試讀取 Big5 編碼的繁中檔案。"""
    text = "繁體中文內容。第二句。"
    f = tmp_path / "big5.txt"
    f.write_bytes(text.encode("big5"))
    t = load_transcript(f)
    assert len(t.sentences) == 2


def test_load_utf8_bom(tmp_path: Path):
    """UTF-8 BOM 開頭的檔案應能正確讀取。"""
    text = "BOM 開頭測試。第二句。"
    f = tmp_path / "bom.txt"
    f.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    t = load_transcript(f)
    assert len(t.sentences) == 2


# ============================================================
# 字元對齊邊界
# ============================================================

def test_char_align_returns_valid_position():
    """_char_align 回傳的位置永遠在合法範圍內。"""
    t = load_from_string("第一句內容。第二句內容。")
    eng = AlignmentEngine(t)
    pos, consumed = eng._char_align(0, "第一句")
    assert 0 <= pos <= t.sentences[0].end
    assert consumed >= 0


def test_char_align_with_empty_recognized():
    """空的辨識文字不應崩潰。"""
    t = load_from_string("第一句。第二句。")
    eng = AlignmentEngine(t)
    pos, consumed = eng._char_align(0, "")
    assert pos == t.sentences[0].start
    assert consumed == 0


def test_char_align_with_completely_unrelated_text():
    """完全不相關的文字應不推進位置。"""
    t = load_from_string("第一句中文內容。第二句中文。")
    eng = AlignmentEngine(t)
    pos, consumed = eng._char_align(0, "abcdefghijk")
    # 應回傳 sent.start（沒命中）
    assert pos == t.sentences[0].start
