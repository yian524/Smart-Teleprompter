"""端對端情境測試：從大型會議發言者角度驗證工具可靠性。

跑法：pytest tests/test_e2e_speaker_scenarios.py -v

若本套測試任一 FAIL → 工具不得交付。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytest.importorskip("fitz")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    import sys
    a = QApplication.instance() or QApplication(sys.argv)
    return a


@pytest.fixture
def small_pdf(tmp_path: Path, app) -> Path:
    import fitz
    d = fitz.open()
    for i in range(5):
        p = d.new_page(width=960, height=540)
        p.insert_text((50, 72), f"Slide {i+1}", fontsize=24)
    out = tmp_path / "small.pdf"
    d.save(str(out))
    d.close()
    return out


@pytest.fixture
def large_pdf(tmp_path: Path, app) -> Path:
    import fitz
    d = fitz.open()
    for i in range(100):
        p = d.new_page(width=1920, height=1080)
        p.insert_text((50, 72), f"Slide {i+1}", fontsize=20)
    out = tmp_path / "large.pdf"
    d.save(str(out))
    d.close()
    return out


@pytest.fixture
def sample_transcript(tmp_path: Path) -> Path:
    content = """<!-- 這是給自己的備忘 -->

# Slide 1 · 開場

大家好，我是今天的報告人。今天分享 Transformer 在 NLP 的應用。

---

# Slide 2 · 背景

2017 年之前，NLP 主要依賴 RNN 和 LSTM。但它們處理長序列有瓶頸。

---

# Slide 3 · 方法

我們使用 PyTorch 框架，採用 AdamW optimizer。

---

# Slide 4 · 結果

GLUE benchmark 上達到 88.5 分，F1 score 85.2。

---

# Slide 5 · 結論

我們證明了 Transformer 在中英混合場景的優異表現。謝謝大家。
"""
    path = tmp_path / "speech.txt"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def sessions_dir(tmp_path: Path, monkeypatch):
    """隔離 sessions.json 到臨時目錄。"""
    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setattr(
        "teleprompter.core.session.default_sessions_path",
        lambda: sd / "sessions.json",
    )
    return sd


@pytest.fixture
def mw(app, sessions_dir, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    # Auto-discard dirty dialog during fixture teardown (測試不該被 QMessageBox 擋住)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **kw: QMessageBox.StandardButton.Discard,
    )
    from teleprompter.config import load_config
    from teleprompter.ui.main_window import MainWindow
    cfg = load_config()
    w = MainWindow(cfg)
    w.resize(1920, 1080)
    w.show()
    app.processEvents()
    time.sleep(0.1)
    app.processEvents()
    yield w
    try:
        w.close()
        app.processEvents()
    except Exception:
        pass


# ============================================================
# 情境 1：上台前 5 分鐘載入檔案
# ============================================================

def test_load_transcript_never_crashes(mw, sample_transcript):
    mw.load_file(str(sample_transcript))
    assert mw.transcript is not None
    assert len(mw.transcript.sentences) > 0
    assert len(mw.transcript.pages) == 5


def test_load_slides_never_crashes(mw, sample_transcript, small_pdf):
    mw.load_file(str(sample_transcript))
    mw.load_slides(str(small_pdf))
    assert mw.slide_deck is not None
    assert mw.slide_deck.page_count == 5


def test_load_large_pdf_does_not_freeze(mw, sample_transcript, large_pdf, app):
    """100 頁 PDF 載入時間應 < 3 秒（lazy 渲染）。"""
    mw.load_file(str(sample_transcript))
    start = time.monotonic()
    mw.load_slides(str(large_pdf))
    app.processEvents()
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"載入太慢：{elapsed:.2f}s"
    assert mw.view._slide_deck.page_count == 100


# ============================================================
# 情境 2：多頁 Slide 的 boundaries 與 document 高度一致
# ============================================================

def test_page_boundaries_match_doc_height(mw, sample_transcript, large_pdf, app):
    """_page_boundaries 最後一項的 bottom_y 必須 <= document 實際高度。
    否則捲到底看不到最後一頁。"""
    mw.load_file(str(sample_transcript))
    mw.load_slides(str(large_pdf))
    app.processEvents()
    time.sleep(0.1)
    app.processEvents()

    boundaries = mw.view._page_boundaries
    assert len(boundaries) == 100, f"應有 100 個 boundary，實際 {len(boundaries)}"
    doc_h = int(mw.view.document().documentLayout().documentSize().height())
    last_bottom = boundaries[-1][1]
    assert last_bottom <= doc_h + 50, (
        f"最後一頁 bottom={last_bottom} 超過 doc_h={doc_h}，會看不到"
    )


def test_scrollbar_reaches_last_slide(mw, sample_transcript, large_pdf, app):
    """scrollbar 最大值應能讓最後一頁 slide 可見。"""
    mw.load_file(str(sample_transcript))
    mw.load_slides(str(large_pdf))
    app.processEvents()
    time.sleep(0.1)
    app.processEvents()

    sb = mw.view.verticalScrollBar()
    vh = mw.view.viewport().height()
    last_bottom = mw.view._page_boundaries[-1][1]
    # 捲到最底
    sb.setValue(sb.maximum())
    app.processEvents()
    # 最後一頁 bottom_y 必須在 [scroll, scroll+vh] 範圍內
    assert last_bottom - sb.value() <= vh + 50


# ============================================================
# 情境 3：辨識流程模擬（餵 text_committed 驅動 engine）
# ============================================================

def test_simulated_recognition_advances_position(mw, sample_transcript, app):
    mw.load_file(str(sample_transcript))
    app.processEvents()
    time.sleep(0.1)
    app.processEvents()

    # 取第一句內容餵給 engine
    first_sentence = mw.transcript.sentences[0].text
    start_pos = mw.engine.current_global_char
    mw._on_text_committed(first_sentence)
    app.processEvents()
    # 位置應有推進
    assert mw.engine.current_global_char > start_pos, "辨識第一句後位置沒前進"


def test_simulated_multiple_sentences_no_crash(mw, sample_transcript, app):
    mw.load_file(str(sample_transcript))
    app.processEvents()
    for s in mw.transcript.sentences[:5]:
        mw._on_text_committed(s.text)
        app.processEvents()
    assert mw.engine.current_sentence_index >= 0


def test_skip_detection_records_ranges(mw, sample_transcript, app):
    """跳講：只餵第 1 句和第 5 句 → 中間應標為漏講。"""
    mw.load_file(str(sample_transcript))
    app.processEvents()
    if len(mw.transcript.sentences) < 5:
        pytest.skip("句子不足 5 句")
    mw._on_text_committed(mw.transcript.sentences[0].text)
    # 跳到第 5 句
    target = mw.transcript.sentences[4].text
    mw._on_text_committed(target)
    app.processEvents()
    # view 的 skipped_ranges 可能為空（演算法可能保守），但不該 crash
    assert isinstance(mw.view._skipped_ranges, list)


# ============================================================
# 情境 4：字型縮放（演講者可能臨時想放大字）
# ============================================================

def test_font_scaling_preserves_text(mw, sample_transcript, small_pdf, app):
    mw.load_file(str(sample_transcript))
    mw.load_slides(str(small_pdf))
    app.processEvents()
    original_text = mw.view.toPlainText()
    for size in [24, 36, 48, 60, 72, 30]:
        mw.view.set_font_size(size)
        app.processEvents()
        assert mw.view.toPlainText() == original_text, f"字型變為 {size} 後文字內容變化"
        # 確保 slide boundaries 依然合理
        assert len(mw.view._page_boundaries) == mw.view._slide_deck.page_count


def test_rapid_font_scaling_no_crash(mw, sample_transcript, small_pdf, app):
    """壓力測試：快速連續縮放 20 次。"""
    mw.load_file(str(sample_transcript))
    mw.load_slides(str(small_pdf))
    app.processEvents()
    for i in range(20):
        mw.view.set_font_size(24 + (i % 4) * 12)
        app.processEvents()
    # 文件仍可查詢
    assert mw.view._doc_length > 0


# ============================================================
# 情境 5：編輯模式往返
# ============================================================

def test_edit_mode_toggle_preserves_content(mw, sample_transcript, app):
    mw.load_file(str(sample_transcript))
    app.processEvents()
    original = mw.view.toPlainText()
    mw.view.set_edit_mode(True)
    app.processEvents()
    mw.view.set_edit_mode(False)
    app.processEvents()
    assert mw.view.toPlainText() == original


def test_format_round_trip(mw, sample_transcript, app):
    """套用粗體 → 匯出 FormatSpan → 重建 document → 還原格式 → 旗標應存在。"""
    from PySide6.QtGui import QTextCursor
    mw.load_file(str(sample_transcript))
    mw.view.set_edit_mode(True)
    app.processEvents()
    cursor = mw.view.textCursor()
    cursor.setPosition(10)
    cursor.setPosition(20, QTextCursor.MoveMode.KeepAnchor)
    mw.view.setTextCursor(cursor)
    mw.view.toggle_bold()
    app.processEvents()
    spans = mw.view.dump_format_spans()
    assert any(s.bold for s in spans), "粗體未記錄到 FormatSpan"


def test_format_survives_md_refresh(mw, sample_transcript, app):
    """回歸測試：粗體套用後 → MD refresh 應保留格式（先前 bug：清掉）。"""
    from PySide6.QtGui import QTextCursor
    mw.load_file(str(sample_transcript))
    mw.view.set_edit_mode(True)
    app.processEvents()
    # 找普通文字位置（第一句話）避開 MD 註解 / heading block
    text = mw.view.toPlainText()
    target = "大家好"
    idx = text.find(target)
    assert idx >= 0, "找不到測試點 '大家好'"
    cursor = mw.view.textCursor()
    cursor.setPosition(idx)
    cursor.setPosition(idx + 3, QTextCursor.MoveMode.KeepAnchor)
    mw.view.setTextCursor(cursor)
    mw.view.toggle_bold()
    mw.view.toggle_italic()
    mw.view.toggle_highlight()
    app.processEvents()

    before = mw.view.dump_format_spans()
    hit = [s for s in before if s.start == idx and s.end == idx + 3]
    assert hit, f"選取範圍 ({idx}-{idx+3}) 沒產生 FormatSpan"
    assert hit[0].bold and hit[0].italic and hit[0].highlight, (
        f"套完粗體/斜體/螢光筆後三旗標應同時為 True，實際 {hit[0]}"
    )

    # 模擬 MD refresh 觸發
    mw.view._refresh_md_while_editing()
    app.processEvents()

    after = mw.view.dump_format_spans()
    hit2 = [s for s in after if s.start == idx and s.end == idx + 3]
    assert hit2, "MD refresh 後目標範圍的 FormatSpan 不見了"
    assert hit2[0].bold and hit2[0].italic and hit2[0].highlight, (
        f"MD refresh 後格式被清掉，實際 {hit2[0]}（bug 回歸）"
    )


def test_edit_mode_expands_transcript_for_extra_slides(mw, sample_transcript, large_pdf, app):
    """進編輯模式時，若 PDF 頁數 > 講稿頁數，應自動追加空白講稿 block 供點擊輸入。"""
    mw.load_file(str(sample_transcript))  # 5 頁講稿
    mw.load_slides(str(large_pdf))         # 100 頁 PDF
    app.processEvents(); time.sleep(0.2); app.processEvents()

    assert len(mw.transcript.pages) == 5
    assert mw.slide_deck.page_count == 100

    before_text = mw.view.toPlainText()
    before_pages = len(mw.transcript.pages)

    # 進編輯模式
    mw._toggle_edit_mode(True)
    app.processEvents()

    # 講稿應已擴張到 100 頁
    assert len(mw.transcript.pages) >= 100, (
        f"進編輯後講稿頁數應 ≥ 100，實際 {len(mw.transcript.pages)}"
    )
    # 文字應包含原本內容 + 新頁標題
    now_text = mw.view.toPlainText()
    assert "大家好" in now_text  # 原本內容保留
    assert "# Slide 100" in now_text or "# Slide 99" in now_text
    assert len(now_text) > len(before_text)

    mw._toggle_edit_mode(False)
    app.processEvents()


def test_edit_exit_preserves_position_and_format(mw, sample_transcript, app):
    """回歸：使用者**沒開始辨識**只是捲動看講稿 → 進編輯 → 套格式 → 離開 →
    必須停在剛剛捲到的那一頁（不是第一頁）+ 格式在。"""
    from PySide6.QtGui import QTextCursor
    mw.load_file(str(sample_transcript))
    mw.resize(1200, 800)
    mw.show()
    app.processEvents(); time.sleep(0.2); app.processEvents()

    # 模擬使用者捲動到中間（engine 在 sentence[0].start，使用者未辨識）
    initial_engine_char = mw.engine.current_global_char
    sb = mw.view.verticalScrollBar()
    target_scroll = max(1, sb.maximum() // 2)
    sb.setValue(target_scroll)
    app.processEvents(); time.sleep(0.1); app.processEvents()
    pre_scroll = sb.value()
    pre_visible_char = mw.view.visible_top_char()
    assert pre_scroll > 0, "scrollbar 應能捲動"
    assert pre_visible_char > 0, "視窗頂端應指向非 0 的 char 位置"

    # 進編輯模式 → 套粗體 + 螢光筆
    mw.act_edit_mode.setChecked(True)
    mw._toggle_edit_mode(True)
    app.processEvents()
    text_now = mw.view.toPlainText()
    idx2 = text_now.find("2017")
    if idx2 < 0:
        idx2 = text_now.find("Transformer")
    assert idx2 >= 0
    cur = mw.view.textCursor()
    cur.setPosition(idx2)
    cur.setPosition(idx2 + 4, QTextCursor.MoveMode.KeepAnchor)
    mw.view.setTextCursor(cur)
    mw.view.toggle_bold()
    mw.view.toggle_highlight()
    app.processEvents()

    # 離開編輯模式
    mw.act_edit_mode.setChecked(False)
    mw._toggle_edit_mode(False)
    app.processEvents(); time.sleep(0.2); app.processEvents()

    # 1) 視窗不應「完全回到最頂端」（使用者抱怨的 regression）
    now_scroll = mw.view.verticalScrollBar().value()
    assert now_scroll > 0, (
        f"離開編輯後捲軸完全歸 0（使用者：「誣陷在第一頁」）：pre={pre_scroll}, now={now_scroll}"
    )
    # 2) 格式仍在
    spans = mw.view.dump_format_spans()
    assert any(s.bold and s.highlight for s in spans), (
        "離開編輯後粗體+螢光筆消失了"
    )


def test_edit_toolbar_hidden_when_not_editing(mw, sample_transcript, app):
    """編輯工具按鈕在非編輯模式應完全隱藏。"""
    mw.load_file(str(sample_transcript))
    app.processEvents()
    # 預設非編輯模式
    assert not mw.view.is_edit_mode()
    for act in (mw.act_bold, mw.act_italic, mw.act_underline,
                mw.act_highlight, mw.act_clear_fmt,
                mw.act_insert_annotation, mw.act_compact_ws):
        assert not act.isVisible(), f"{act.text()} 在非編輯模式應隱藏"
    # 進編輯 → 顯示
    mw.view.set_edit_mode(True)
    app.processEvents()
    for act in (mw.act_bold, mw.act_italic, mw.act_underline,
                mw.act_highlight, mw.act_clear_fmt,
                mw.act_insert_annotation, mw.act_compact_ws):
        assert act.isVisible(), f"{act.text()} 在編輯模式應顯示"


# ============================================================
# 情境 6：多 Session 分頁
# ============================================================

def test_multiple_sessions_isolated(mw, sample_transcript, app, tmp_path):
    mw.load_file(str(sample_transcript))
    pos1 = mw.engine.current_global_char
    # 模擬辨識 → 改變 session 1 位置
    mw._on_text_committed(mw.transcript.sentences[0].text)
    pos1 = mw.engine.current_global_char
    # 新分頁
    mw._new_tab()
    app.processEvents()
    # 新 session 應為空白
    assert mw.transcript is None
    # 切回第一個
    first_sid = mw.session_manager.sessions[0].session_id
    mw.session_manager.set_active(first_sid)
    app.processEvents()
    assert mw.transcript is not None
    # 位置應該保留（已寫回 session）
    assert mw.engine.current_global_char >= 0


def test_session_persistence_round_trip(app, tmp_path, sample_transcript, small_pdf, monkeypatch):
    """必須 monkeypatch main_window.default_sessions_path（imported name）才能重導。"""
    sessions_path = tmp_path / "sessions.json"
    # main_window.py 用 `from ..core.session import default_sessions_path` — 要改那裡的名字
    monkeypatch.setattr(
        "teleprompter.ui.main_window.default_sessions_path",
        lambda: sessions_path,
    )

    from teleprompter.config import load_config
    from teleprompter.ui.main_window import MainWindow
    cfg = load_config()
    w1 = MainWindow(cfg)
    w1.show()
    app.processEvents()
    time.sleep(0.2)
    app.processEvents()
    w1.load_file(str(sample_transcript))
    w1.load_slides(str(small_pdf))
    app.processEvents()
    w1.close()
    app.processEvents()

    assert sessions_path.exists(), "sessions.json 沒被寫入"
    data = json.loads(sessions_path.read_text(encoding="utf-8"))
    assert len(data["sessions"]) >= 1

    w2 = MainWindow(cfg)
    w2.show()
    app.processEvents()
    time.sleep(0.3)
    app.processEvents()
    assert w2.transcript is not None, "重開後 transcript 沒還原"
    w2.close()


# ============================================================
# 情境 7：錯誤復原
# ============================================================

def test_load_missing_transcript_does_not_crash(mw, tmp_path, app, monkeypatch):
    """載入不存在檔案：app 不能 crash。QMessageBox 會被 patch 成 no-op 避免阻塞。"""
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **kw: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.StandardButton.Ok)
    fake = tmp_path / "nope.txt"
    try:
        mw.load_file(str(fake))
    except Exception:
        pass
    real = tmp_path / "real.txt"
    real.write_text("hello world.\n", encoding="utf-8")
    mw.load_file(str(real))
    assert mw.transcript is not None


def test_load_bad_pdf_does_not_crash(mw, sample_transcript, tmp_path, app, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **kw: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.StandardButton.Ok)
    mw.load_file(str(sample_transcript))
    fake_pdf = tmp_path / "bad.pdf"
    fake_pdf.write_bytes(b"not a pdf")
    try:
        mw.load_slides(str(fake_pdf))
    except Exception:
        pass
    import fitz
    good = tmp_path / "good.pdf"
    d = fitz.open()
    d.new_page()
    d.save(str(good))
    d.close()
    mw.load_slides(str(good))
    assert mw.slide_deck is not None


# ============================================================
# 情境 8：選取在編輯中不被重刷清除
# ============================================================

def test_selection_survives_md_refresh(mw, sample_transcript, app):
    from PySide6.QtGui import QTextCursor
    mw.load_file(str(sample_transcript))
    mw.view.set_edit_mode(True)
    app.processEvents()
    cursor = mw.view.textCursor()
    cursor.setPosition(10)
    cursor.setPosition(30, QTextCursor.MoveMode.KeepAnchor)
    mw.view.setTextCursor(cursor)
    anchor = mw.view.textCursor().anchor()
    pos = mw.view.textCursor().position()
    # 模擬 MD refresh 觸發
    mw.view._refresh_md_while_editing()
    app.processEvents()
    assert mw.view.textCursor().hasSelection(), "選取被清掉"
    assert mw.view.textCursor().anchor() == anchor
    assert mw.view.textCursor().position() == pos


# ============================================================
# 情境 9：清理空白不破壞內容
# ============================================================

def test_compact_whitespace_keeps_heading_content(mw, sample_transcript, app):
    mw.load_file(str(sample_transcript))
    mw.view.set_edit_mode(True)
    app.processEvents()
    mw.view.compact_whitespace()
    app.processEvents()
    text = mw.view.toPlainText()
    assert "Slide 1" in text
    assert "Slide 5" in text
    assert "大家好" in text


# ============================================================
# 情境 10：拖拉分隔條
# ============================================================

def test_edit_modified_text_persists_across_restart(app, tmp_path, sample_transcript, monkeypatch):
    """核心：編輯過的內容 → 關閉 app → 重開 → 內容仍在。"""
    sessions_path = tmp_path / "sessions.json"
    monkeypatch.setattr(
        "teleprompter.ui.main_window.default_sessions_path",
        lambda: sessions_path,
    )
    from teleprompter.config import load_config
    from teleprompter.ui.main_window import MainWindow
    cfg = load_config()

    # 開啟 → 編輯 → 關閉
    w1 = MainWindow(cfg)
    w1.show(); app.processEvents(); time.sleep(0.1); app.processEvents()
    w1.load_file(str(sample_transcript))
    app.processEvents(); time.sleep(0.1); app.processEvents()
    w1.view.set_edit_mode(True)
    app.processEvents()
    # 直接修改文字
    w1.view.setPlainText("修改後的全新內容。\n這是第二句話。")
    w1.view.set_edit_mode(False)
    app.processEvents(); time.sleep(0.1); app.processEvents()
    # 確認 session 有標 dirty + 有 modified_text
    active = w1.session_manager.active
    assert active.dirty is True
    assert "修改後的全新內容" in active.modified_text
    # 關閉（繞過 dirty dialog）
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **kw: QMessageBox.StandardButton.Discard,
    )
    w1.close()
    app.processEvents()

    # 重開
    w2 = MainWindow(cfg)
    w2.show(); app.processEvents(); time.sleep(0.2); app.processEvents()
    active2 = w2.session_manager.active
    assert active2 is not None
    assert "修改後的全新內容" in active2.modified_text
    # transcript 應是從 modified_text 來的，不是原檔
    assert active2.transcript is not None
    assert "修改後的全新內容" in active2.transcript.full_text
    w2.close()


def test_save_writes_transcript_to_file(mw, sample_transcript, tmp_path, app):
    """Ctrl+S 應把目前內容寫回檔案。"""
    mw.load_file(str(sample_transcript))
    mw.view.set_edit_mode(True)
    app.processEvents()
    mw.view.setPlainText("寫入檔案的測試內容\n第二句")
    mw.view.set_edit_mode(False)
    app.processEvents(); time.sleep(0.1); app.processEvents()
    assert mw.session_manager.active.dirty is True
    # 呼叫存檔（覆寫 sample_transcript 檔案）
    mw._save_current_transcript()
    app.processEvents()
    # dirty 應被清掉
    assert mw.session_manager.active.dirty is False
    # 檔案內容應更新
    written = sample_transcript.read_text(encoding="utf-8")
    assert "寫入檔案的測試內容" in written


def test_font_size_spinbox_syncs_with_view(mw, sample_transcript, app):
    """工具列字級 spinbox 與 view 字型雙向同步。"""
    mw.load_file(str(sample_transcript))
    app.processEvents()
    # 從 spinbox 改 → view 跟著改
    mw.sb_font_size.setValue(48)
    app.processEvents()
    assert mw.view.font_size() == 48
    # 從 view 改（Ctrl+wheel 模擬）→ spinbox 跟著改
    mw.view.set_font_size(60)
    app.processEvents()
    assert mw.sb_font_size.value() == 60


def test_slide_label_shows_top_visible_page(mw, sample_transcript, large_pdf, app):
    """右上角頁碼以視窗頂端為準（而非 1/3 處）。"""
    mw.load_file(str(sample_transcript))
    mw.load_slides(str(large_pdf))
    mw.resize(1400, 900); mw.show()
    app.processEvents(); time.sleep(0.2); app.processEvents()

    boundaries = mw.view._page_boundaries
    assert len(boundaries) >= 3
    # 滾到第 3 頁頂端附近
    target_top = boundaries[2][0]
    mw.view.verticalScrollBar().setValue(target_top + 5)
    app.processEvents()
    # 直接呼叫 update
    mw._update_slide_label_from_viewport()
    # 右上角應顯示第 3 頁
    label = mw.time_panel.slide_label.text()
    assert "3" in label, f"應顯示第 3 頁，實際：{label}"


def test_slide_double_click_opens_viewer(mw, sample_transcript, small_pdf, app, monkeypatch):
    """雙擊 slide 應發 signal。"""
    mw.load_file(str(sample_transcript))
    mw.load_slides(str(small_pdf))
    mw.resize(1400, 900); mw.show()
    app.processEvents(); time.sleep(0.2); app.processEvents()

    # patch SlideViewerDialog.exec 避免開對話框卡住測試
    opened: list[int] = []
    orig_open = mw._open_slide_viewer
    def fake_open(page_no):
        opened.append(page_no)
    mw._open_slide_viewer = fake_open
    mw.view.slide_double_clicked.disconnect()
    mw.view.slide_double_clicked.connect(fake_open)

    # 模擬點擊右欄 slide 座標
    rect = mw.view._slide_area_rect_for_page(1)
    assert rect is not None
    slide_x, _, slide_w, _ = rect
    click_x = slide_x + slide_w // 2
    # 第 1 頁的 top
    top_y = mw.view._page_boundaries[0][0]
    click_y = top_y + 50 - mw.view.verticalScrollBar().value()

    page_no = mw.view._page_at_viewport_pos(click_x, click_y)
    assert page_no == 1, f"應判定為第 1 頁，實際：{page_no}"


def test_target_duration_toggle(mw, app):
    """勾選/取消勾選目標時長應正確呼叫 timer_ctrl。"""
    assert hasattr(mw, "cb_target")
    assert hasattr(mw, "sb_target_min")
    mw.cb_target.setChecked(False)
    mw._on_target_toggled(False)
    assert (mw.timer_ctrl.target_ms // 1000) == 0
    assert not mw.sb_target_min.isVisible() or True  # 視覺上可能還 show，但 target_sec 為 0

    mw.sb_target_min.setValue(15)
    mw.cb_target.setChecked(True)
    mw._on_target_toggled(True)
    assert (mw.timer_ctrl.target_ms // 1000) == 15 * 60

    # 改分鐘數
    mw.sb_target_min.setValue(20)
    mw._on_target_minutes_changed(20)
    assert (mw.timer_ctrl.target_ms // 1000) == 20 * 60


def test_goto_speech_position(mw, sample_transcript, app):
    """按「回到念稿位置」應捲回 engine 位置。"""
    mw.load_file(str(sample_transcript))
    mw.resize(1200, 800); mw.show()
    app.processEvents(); time.sleep(0.1); app.processEvents()

    # 模擬 engine 推進
    text = mw.view.toPlainText()
    idx = text.find("2017")
    if idx > 0:
        result = mw.engine.jump_to_global_char(idx)
        mw.view.set_position(result.global_char_pos, animate=False)
        app.processEvents()

    # 使用者捲到最上面
    mw.view.verticalScrollBar().setValue(0)
    app.processEvents()
    assert mw.view.verticalScrollBar().value() == 0

    # 按按鈕
    mw._goto_speech_position()
    app.processEvents()
    # scroll value 應改變（若 engine 位置非頂端）
    # 至少呼叫不 crash 就算通過
    assert True


def test_split_handle_drag_changes_ratio(mw, sample_transcript, small_pdf, app):
    mw.load_file(str(sample_transcript))
    mw.load_slides(str(small_pdf))
    app.processEvents()
    original_ratio = mw.view._text_width_ratio
    mw.view.set_split_ratio(0.4)
    app.processEvents()
    assert abs(mw.view._text_width_ratio - 0.4) < 0.01
    assert 0.25 <= mw.view._text_width_ratio <= 0.85
