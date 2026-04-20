"""端對端整合測試 — 模擬實際報告完整流程（不需真實麥克風/Whisper 模型）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from teleprompter.config import AppConfig
from teleprompter.core.transcript_loader import load_from_string, load_transcript
from teleprompter.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _mock_modal_dialogs(monkeypatch):
    """全域 mock 所有可能 modal 阻塞的對話框，避免測試卡死。"""
    from PySide6.QtWidgets import QMessageBox, QFileDialog, QInputDialog
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: 0)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: 0)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: 0)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *a, **k: ("", False))
    monkeypatch.setattr(QInputDialog, "getInt", lambda *a, **k: (0, False))


@pytest.fixture
def main_window(qapp):
    cfg = AppConfig()
    win = MainWindow(cfg)
    yield win
    # 清理
    try:
        win.audio.stop()
        win.recognizer.stop()
        win.timer_ctrl.pause()
        win.close()
    except Exception:
        pass


# ========================================================================
# 完整報告流程模擬
# ========================================================================

PRESENTATION = (
    "大家好，我是今天的報告人。"
    "今天要分享 Transformer 在自然語言處理的應用。"
    "首先我會從背景開始介紹。"
    "在 2017 年之前，NLP 主要依賴 RNN 和 LSTM。"
    "Transformer 提出了 self-attention 機制。"
    "接著介紹我們的實作方法。"
    "我們使用 PyTorch 訓練 BERT 模型。"
    "實驗結果在 GLUE 達到 88.5 分。"
    "最後是結論與未來展望。"
)


def test_full_presentation_flow(main_window):
    """模擬講者順序念完整份稿全程不出錯。"""
    win = main_window
    t = load_from_string(PRESENTATION)
    win._apply_transcript(t, source_path="")

    # 模擬講者依序念出每句（每句分 2 段串流 commit）
    for sent in t.sentences:
        text = sent.normalized
        win._on_text_committed(text[: len(text) // 2])
        win._on_text_committed(text[len(text) // 2 :])

    # 應推進到接近末段
    assert win.engine.current_sentence_index >= len(t.sentences) - 2


def test_skip_during_presentation_marks_skipped_visually(main_window):
    """報告中跳段，PrompterView 應收到漏講標記。"""
    win = main_window
    t = load_from_string(PRESENTATION)
    win._apply_transcript(t, source_path="")

    win._on_text_committed("大家好我是今天的報告人")
    # 直接跳到結論
    win._on_text_committed("最後是結論與未來展望")
    # PrompterView 的漏講範圍應有內容
    assert len(win.view._skipped_ranges) > 0


def test_pause_resume_does_not_lose_state(main_window):
    """暫停後續講不會遺失對齊狀態。"""
    win = main_window
    t = load_from_string(PRESENTATION)
    win._apply_transcript(t, source_path="")

    win._on_text_committed("大家好我是今天的報告人")
    win._on_text_committed("今天要分享 transformer")
    pos_before_pause = win.engine.current_global_char
    sent_before_pause = win.engine.current_sentence_index

    # 模擬暫停（不啟動實際 audio thread）
    win.timer_ctrl.pause()

    # 繼續報告
    win._on_text_committed("首先我會從背景開始介紹")
    assert win.engine.current_global_char >= pos_before_pause
    assert win.engine.current_sentence_index >= sent_before_pause


def test_manual_jump_during_presentation(main_window):
    """講者手動跳句後，後續辨識從新位置繼續。"""
    win = main_window
    t = load_from_string(PRESENTATION)
    win._apply_transcript(t, source_path="")

    win._on_text_committed("大家好我是今天的報告人")
    # 手動跳到第 5 句
    win._jump_relative(4)
    target_idx = win.engine.current_sentence_index
    assert target_idx >= 4

    # 繼續講後續內容
    win._on_text_committed("接著介紹我們的實作方法")
    assert win.engine.current_sentence_index >= target_idx


def test_clear_skipped_marks_works(main_window):
    """清除漏講按鈕可重置所有標記。"""
    win = main_window
    t = load_from_string(PRESENTATION)
    win._apply_transcript(t, source_path="")

    win._on_text_committed("大家好我是今天的報告人")
    win._on_text_committed("最後是結論與未來展望")
    assert len(win.view._skipped_ranges) > 0
    win._clear_skipped()
    assert win.view._skipped_ranges == []


def test_reset_to_top_clears_everything(main_window):
    """回頂功能應重置位置與所有漏講標記。"""
    win = main_window
    t = load_from_string(PRESENTATION)
    win._apply_transcript(t, source_path="")

    win._on_text_committed("大家好我是今天的報告人")
    win._on_text_committed("最後是結論與未來展望")
    win._reset_position()

    assert win.engine.current_sentence_index == 0
    assert win.engine.current_global_char == 0
    assert win.view._skipped_ranges == []


def test_reload_transcript_resets_state(main_window):
    """重新載入講稿應完全清空舊狀態。"""
    win = main_window
    t1 = load_from_string(PRESENTATION)
    win._apply_transcript(t1, source_path="")
    win._on_text_committed("大家好我是")
    win._on_text_committed("最後是結論")
    assert len(win.view._skipped_ranges) > 0 or win.engine.current_sentence_index > 0

    # 載入新講稿
    new_text = "新的講稿。第二句。"
    t2 = load_from_string(new_text)
    win._apply_transcript(t2, source_path="")
    assert win.engine.current_sentence_index == 0
    assert win.engine.current_global_char == 0
    assert win.view._skipped_ranges == []


# ========================================================================
# 多格式載入端對端
# ========================================================================

def test_txt_file_loading(main_window, tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("第一句。第二句。第三句。", encoding="utf-8")
    win = main_window
    win.load_file(str(f))
    assert win.transcript is not None
    assert len(win.transcript.sentences) == 3


def test_md_file_loading(main_window, tmp_path: Path):
    f = tmp_path / "a.md"
    f.write_text("# 標題\n\n這是 **粗體** 內容。第二句。", encoding="utf-8")
    win = main_window
    win.load_file(str(f))
    assert win.transcript is not None
    assert any("粗體" in s.text for s in win.transcript.sentences)


def test_invalid_file_path_handled(main_window, tmp_path: Path, monkeypatch):
    """不存在的檔案應顯示錯誤而非崩潰。"""
    from PySide6.QtWidgets import QMessageBox
    # mock 掉 modal dialog 避免測試卡死
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: 0)
    win = main_window
    nonexistent = tmp_path / "doesnotexist.txt"
    # 不應拋例外（內部會 catch 並顯示 dialog → 已被 mock）
    win.load_file(str(nonexistent))


# ========================================================================
# 計時器在報告流程中的整合
# ========================================================================

def test_timer_progress_reflects_engine_position(main_window):
    """講稿進度應隨對齊引擎位置變化。"""
    win = main_window
    t = load_from_string(PRESENTATION)
    win._apply_transcript(t, source_path="")

    # 初始進度應為 0
    assert win._script_progress() == 0.0

    # 念到中間
    for sent in t.sentences[:5]:
        win._on_text_committed(sent.normalized)
    progress = win._script_progress()
    assert 0 < progress < 1.0


# ========================================================================
# 字體調整在報告中即時生效
# ========================================================================

def test_font_size_change_during_recognition_does_not_break(main_window):
    """字體調整不應中斷辨識流程。"""
    win = main_window
    t = load_from_string(PRESENTATION)
    win._apply_transcript(t, source_path="")

    win._on_text_committed("大家好我是今天的報告人")
    pos_before = win.engine.current_global_char

    # 調整字體
    win.view.set_font_size(48)
    win.view.set_font_size(36)

    # 後續辨識仍正常
    win._on_text_committed("今天要分享 transformer")
    assert win.engine.current_global_char >= pos_before


# ========================================================================
# 長時間穩定性
# ========================================================================

def test_long_session_no_state_corruption(main_window):
    """模擬長時間報告，使用各自獨立的句子內容。"""
    win = main_window
    unique_sentences = [
        "大家好我是報告人", "今天分享研究成果", "首先介紹背景動機",
        "接著說明相關工作", "我們提出新方法", "資料集來自公開語料",
        "訓練使用 PyTorch", "實驗效果非常好", "錯誤分析有三類",
        "與基準比較有改善", "消融實驗驗證貢獻", "視覺化結果清楚",
        "貢獻有三個方面", "限制是計算成本", "未來研究方向",
        "感謝聆聽歡迎提問",
    ]
    long_script = "。".join(unique_sentences) + "。"
    t = load_from_string(long_script)
    win._apply_transcript(t, source_path="")

    for sent in unique_sentences:
        win._on_text_committed(sent[:5])
        win._on_text_committed(sent[5:])

    # 應推進到中後段
    assert win.engine.current_sentence_index >= len(t.sentences) // 3


def test_engine_position_within_doc_bounds_after_many_updates(main_window):
    """大量 update 後 global_char 仍在文檔長度內。"""
    win = main_window
    t = load_from_string(PRESENTATION)
    win._apply_transcript(t, source_path="")

    for _ in range(500):
        win._on_text_committed("最後是結論")

    assert 0 <= win.engine.current_global_char <= win.transcript.total_chars


# ========================================================================
# Pause/Resume 多次循環
# ========================================================================

def test_multiple_pause_resume_cycles(main_window):
    """多次暫停-繼續循環不應損壞狀態。"""
    win = main_window
    t = load_from_string(PRESENTATION)
    win._apply_transcript(t, source_path="")

    for _ in range(5):
        win._on_text_committed("大家好我是")
        win.timer_ctrl.pause()
        win.timer_ctrl.start()

    # 不崩潰即過
    assert win.engine.current_sentence_index >= 0
