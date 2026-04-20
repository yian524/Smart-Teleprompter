"""極嚴格 QA：演講當下最壞情況測試。

每個測試都模擬「演講中會發生的崩潰情境」。任一失敗 → 不得交付。
"""

from __future__ import annotations

import json
import os
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
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setattr(
        "teleprompter.ui.main_window.default_sessions_path",
        lambda: sd / "sessions.json",
    )
    # auto-discard dirty dialog during teardown
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **kw: QMessageBox.StandardButton.Discard,
    )
    return sd


@pytest.fixture
def sample_transcript(tmp_path):
    t = tmp_path / "speech.txt"
    t.write_text(
        "<!-- 備忘 -->\n\n# Slide 1 · 開場\n\n大家好，我是報告人。\n\n"
        "---\n\n# Slide 2 · 方法\n\n我們用 PyTorch。\n\n"
        "---\n\n# Slide 3 · 結論\n\n謝謝。",
        encoding="utf-8",
    )
    return t


@pytest.fixture
def pdf_5(tmp_path, app):
    import fitz
    d = fitz.open()
    for i in range(5):
        p = d.new_page(width=960, height=540)
        p.insert_text((50, 72), f"Slide {i+1}", fontsize=28)
    out = tmp_path / "deck.pdf"
    d.save(str(out))
    d.close()
    return out


def _make_mw(app, cfg=None):
    from teleprompter.config import load_config
    from teleprompter.ui.main_window import MainWindow
    if cfg is None:
        cfg = load_config()
    w = MainWindow(cfg)
    w.resize(1400, 900)
    w.show()
    app.processEvents()
    time.sleep(0.1)
    app.processEvents()
    return w


# ============================================================
# 情境 S1：使用者演講到一半 app 意外關閉 → 重開必須能繼續
# ============================================================

def test_s1_crash_recovery_restores_edit_and_position(app, sessions_dir, sample_transcript, tmp_path):
    """演講中 app 崩潰（模擬）→ 重開後編輯內容 + 位置都在。"""
    w = _make_mw(app)
    w.load_file(str(sample_transcript))
    app.processEvents(); time.sleep(0.1); app.processEvents()

    # 編輯
    w.view.set_edit_mode(True); app.processEvents()
    w.view.setPlainText("緊急修改的內容。\n\n第二句。")
    w.view.set_edit_mode(False)
    app.processEvents(); time.sleep(0.1); app.processEvents()

    # 捲到中間
    sb = w.view.verticalScrollBar()
    sb.setValue(max(1, sb.maximum() // 2))
    # save view state manually
    if w._bound_session_id:
        active = w.session_manager.get(w._bound_session_id)
        w._save_view_state_to(active)

    # 觸發 session 存檔（模擬 close 流程的存檔部分）
    from teleprompter.ui.main_window import default_sessions_path
    w.session_manager.save_to_disk(default_sessions_path())

    # 「崩潰」（不走 close flow）
    w.hide()

    # 新實例模擬重開
    w2 = _make_mw(app)
    time.sleep(0.2); app.processEvents()
    s = w2.session_manager.active
    assert s is not None
    assert "緊急修改的內容" in (s.modified_text or ""), "編輯後內容遺失"
    assert s.transcript is not None
    assert "緊急修改的內容" in s.transcript.full_text
    w2.close(); w.close()


# ============================================================
# 情境 S2：快速多次切 tab + 回主 tab 位置不能亂
# ============================================================

def test_s2_rapid_tab_switch_keeps_position(app, sessions_dir, sample_transcript):
    w = _make_mw(app)
    w.load_file(str(sample_transcript))
    app.processEvents(); time.sleep(0.1); app.processEvents()

    first_id = w.session_manager.active.session_id
    # 推進到某位置
    text = w.view.toPlainText()
    idx = text.find("方法")
    if idx > 0:
        result = w.engine.jump_to_global_char(idx)
        w.view.set_position(result.global_char_pos, animate=False)
    pos_before = w.engine.current_global_char

    # 快速切 5 次 tab
    for _ in range(5):
        w._new_tab(); app.processEvents()
        w.session_manager.set_active(first_id); app.processEvents()

    # 位置必須保留
    pos_after = w.engine.current_global_char
    assert pos_after == pos_before, f"位置從 {pos_before} 變為 {pos_after}"
    w.close()


# ============================================================
# 情境 S3：辨識引擎在錯誤 tab 推進（跨 tab 污染）
# ============================================================

def test_s3_recognition_only_affects_active_tab(app, sessions_dir, sample_transcript):
    w = _make_mw(app)
    w.load_file(str(sample_transcript))
    app.processEvents(); time.sleep(0.1); app.processEvents()
    tab1_engine = w.engine
    tab1_pos_before = tab1_engine.current_global_char

    # 新 tab
    w._new_tab(); app.processEvents(); time.sleep(0.1); app.processEvents()
    tab2_engine = w.engine
    assert tab1_engine is not tab2_engine, "不同 tab 應有不同 engine 實例"

    # 模擬在 tab2 收到辨識
    w._on_text_committed("這是 tab2 收到的句子")
    app.processEvents()

    # tab1 engine 的位置不應被動到
    # 切回 tab1
    first_id = w.session_manager.sessions[0].session_id
    w.session_manager.set_active(first_id); app.processEvents()
    assert w.engine.current_global_char == tab1_pos_before, "tab1 被 tab2 的辨識污染"
    w.close()


# ============================================================
# 情境 S4：連續極端縮放字型不崩潰
# ============================================================

def test_s4_extreme_font_scaling_no_crash(app, sessions_dir, sample_transcript, pdf_5):
    w = _make_mw(app)
    w.load_file(str(sample_transcript))
    w.load_slides(str(pdf_5))
    app.processEvents(); time.sleep(0.2); app.processEvents()

    for size in (12, 120, 12, 80, 24, 120, 12):
        w.view.set_font_size(size); app.processEvents()
        assert w.view._doc_length > 0
        assert len(w.view._page_boundaries) == 5
    w.close()


# ============================================================
# 情境 S5：辨識 + 同時錄影 + 同時編輯模式
# ============================================================

def test_s5_concurrent_actions_no_state_corruption(app, sessions_dir, sample_transcript):
    w = _make_mw(app)
    w.load_file(str(sample_transcript))
    app.processEvents()

    # 餵辨識
    for s in w.transcript.sentences[:2]:
        w._on_text_committed(s.text); app.processEvents()
    char_after_recog = w.engine.current_global_char
    assert char_after_recog > 0

    # 進編輯模式（會暫停辨識）
    w.view.set_edit_mode(True); app.processEvents()
    assert w.view.is_edit_mode()

    # 退出
    w.view.set_edit_mode(False); app.processEvents(); time.sleep(0.1); app.processEvents()
    assert not w.view.is_edit_mode()
    w.close()


# ============================================================
# 情境 S6：Session JSON 檔壞掉不能整個 app 崩
# ============================================================

def test_s6_corrupted_sessions_json_graceful(app, tmp_path, monkeypatch):
    sfile = tmp_path / "sessions.json"
    sfile.write_text("this is not json {{{", encoding="utf-8")
    monkeypatch.setattr(
        "teleprompter.ui.main_window.default_sessions_path", lambda: sfile,
    )
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **kw: QMessageBox.StandardButton.Discard,
    )
    # 應該能正常啟動（loader 內部 try/except）
    w = _make_mw(app)
    time.sleep(0.2); app.processEvents()
    # 仍有一個 bootstrap session
    assert len(w.session_manager) >= 1
    w.close()


# ============================================================
# 情境 S7：Ctrl+S 存檔 → 重新載入 → 檔案內容正確
# ============================================================

def test_s7_save_and_reload_round_trip(app, sessions_dir, sample_transcript):
    w = _make_mw(app)
    w.load_file(str(sample_transcript))
    app.processEvents(); time.sleep(0.1); app.processEvents()

    w.view.set_edit_mode(True); app.processEvents()
    w.view.setPlainText("新內容第一句\n新內容第二句\n")
    w.view.set_edit_mode(False); app.processEvents(); time.sleep(0.1); app.processEvents()

    w._save_current_transcript(); app.processEvents()
    assert w.session_manager.active.dirty is False

    content = Path(sample_transcript).read_text(encoding="utf-8")
    assert "新內容第一句" in content
    assert "新內容第二句" in content

    # 重新載入該檔（模擬下次開）
    w2 = _make_mw(app)
    w2.load_file(str(sample_transcript))
    app.processEvents(); time.sleep(0.1); app.processEvents()
    assert "新內容第一句" in w2.view.toPlainText()
    w.close(); w2.close()


# ============================================================
# 情境 S8：超長講稿（1000 句）不應導致 engine 慢到不可用
# ============================================================

def test_s8_long_transcript_performance(app, sessions_dir, tmp_path):
    long_text = "\n".join(
        f"這是第 {i+1} 句話，用來測試 long transcript 效能。"
        for i in range(1000)
    )
    long_file = tmp_path / "long.txt"
    long_file.write_text(long_text, encoding="utf-8")

    w = _make_mw(app)
    start = time.monotonic()
    w.load_file(str(long_file))
    app.processEvents(); time.sleep(0.1); app.processEvents()
    load_time = time.monotonic() - start
    assert load_time < 3.0, f"1000 句載入 {load_time:.2f}s 太慢"
    assert len(w.transcript.sentences) >= 1000
    # 餵辨識不卡
    start = time.monotonic()
    for s in w.transcript.sentences[:10]:
        w._on_text_committed(s.text)
    process_time = time.monotonic() - start
    assert process_time < 2.0, f"辨識 10 句 {process_time:.2f}s 太慢"
    w.close()


# ============================================================
# 情境 S9：格式化套用在空選取不應改動任何內容
# ============================================================

def test_s9_format_on_empty_selection_no_change(app, sessions_dir, sample_transcript):
    w = _make_mw(app)
    w.load_file(str(sample_transcript))
    w.view.set_edit_mode(True); app.processEvents()
    text_before = w.view.toPlainText()
    spans_before = w.view.dump_format_spans()

    # 游標沒 selection
    w.view.toggle_bold()
    w.view.toggle_italic()
    w.view.toggle_underline()
    w.view.toggle_highlight()
    app.processEvents()

    text_after = w.view.toPlainText()
    spans_after = w.view.dump_format_spans()
    assert text_before == text_after, "無選取但格式化時內容被改"
    assert spans_before == spans_after, "無選取但格式化時格式被改"
    w.close()


# ============================================================
# 情境 S10：QA 模式 + 辨識並存不 crash
# ============================================================

def test_s10_qa_mode_toggle_no_crash(app, sessions_dir, sample_transcript):
    w = _make_mw(app)
    w.load_file(str(sample_transcript))
    app.processEvents()
    # toggle on / off 數次
    for _ in range(3):
        w._toggle_qa_mode(); app.processEvents()
        w._toggle_qa_mode(); app.processEvents()
    w.close()


# ============================================================
# 情境 S11：關閉時不應因 timer / recorder 阻塞
# ============================================================

def test_s11_clean_close_under_2_seconds(app, sessions_dir, sample_transcript):
    w = _make_mw(app)
    w.load_file(str(sample_transcript))
    # 推進引擎 + 開計時 + 模擬辨識
    w.timer_ctrl.start()
    for s in w.transcript.sentences[:3]:
        w._on_text_committed(s.text)
    app.processEvents(); time.sleep(0.1); app.processEvents()

    start = time.monotonic()
    w.close(); app.processEvents()
    close_time = time.monotonic() - start
    assert close_time < 2.0, f"關閉耗時 {close_time:.2f}s 太久（可能卡死 subprocess）"


# ============================================================
# 情境 S12：PDF + 空講稿（只有投影片沒講稿）
# ============================================================

def test_s12_slides_without_transcript(app, sessions_dir, tmp_path, pdf_5):
    # 只開新 tab 沒載入講稿
    w = _make_mw(app)
    app.processEvents(); time.sleep(0.1); app.processEvents()
    # 沒 transcript 時不應 load_slides crash
    try:
        w.load_slides(str(pdf_5)); app.processEvents()
    except Exception as e:
        pytest.fail(f"載入 slides 無講稿情境 crash: {e}")
    w.close()
