"""Format 工具不再綁在 edit mode；結構性動作彈確認視窗。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import QApplication, QMessageBox

from teleprompter.config import load_config
from teleprompter.ui import main_window as mw_mod
from teleprompter.ui.main_window import MainWindow


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def main_window(app, tmp_path, monkeypatch):
    monkeypatch.setattr(
        mw_mod, "default_sessions_path", lambda: tmp_path / "sessions.json"
    )
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Discard),
    )
    cfg = load_config()
    w = MainWindow(cfg)
    w.show()
    loop = QEventLoop(); QTimer.singleShot(200, loop.quit); loop.exec()
    yield w
    w.close()


def _load_simple(mw, tmp_path, text="HelloWorldFoobar\n這是一段測試內容。\n"):
    f = tmp_path / "s.txt"
    f.write_text(text, encoding="utf-8")
    mw.load_file(str(f))
    loop = QEventLoop(); QTimer.singleShot(150, loop.quit); loop.exec()


# ---- Format 類：不需編輯模式 ----


def test_toggle_bold_works_without_edit_mode(main_window, tmp_path, app):
    _load_simple(main_window, tmp_path)
    assert not main_window.view.is_edit_mode()
    # 選取 5 個字
    cur = main_window.view.textCursor()
    cur.setPosition(0)
    cur.setPosition(5, QTextCursor.MoveMode.KeepAnchor)
    main_window.view.setTextCursor(cur)
    main_window.view.toggle_bold()
    # Dump format spans：應有 bold span
    spans = main_window.view.dump_format_spans()
    bolds = [s for s in spans if s.bold]
    assert len(bolds) >= 1, "非編輯模式應可套粗體"


def test_toggle_highlight_works_without_edit_mode(main_window, tmp_path, app):
    _load_simple(main_window, tmp_path)
    assert not main_window.view.is_edit_mode()
    cur = main_window.view.textCursor()
    cur.setPosition(0)
    cur.setPosition(3, QTextCursor.MoveMode.KeepAnchor)
    main_window.view.setTextCursor(cur)
    main_window.view.toggle_highlight()
    spans = main_window.view.dump_format_spans()
    assert any(s.highlight for s in spans), "非編輯模式應可套螢光筆"


def test_clear_format_works_without_edit_mode(main_window, tmp_path, app):
    """非編輯模式下 clear_format 清除選取範圍格式。"""
    _load_simple(main_window, tmp_path)
    assert not main_window.view.is_edit_mode()
    # 直接在非編輯模式下套粗體
    cur = main_window.view.textCursor()
    cur.setPosition(0)
    cur.setPosition(5, QTextCursor.MoveMode.KeepAnchor)
    main_window.view.setTextCursor(cur)
    main_window.view.toggle_bold()
    assert any(s.bold for s in main_window.view.dump_format_spans()), "先套成粗體"
    # 再選同範圍 clear_format
    cur = main_window.view.textCursor()
    cur.setPosition(0)
    cur.setPosition(5, QTextCursor.MoveMode.KeepAnchor)
    main_window.view.setTextCursor(cur)
    main_window.view.clear_format()
    assert not any(s.bold for s in main_window.view.dump_format_spans()), "清除後無粗體"


# ---- 結構類：彈確認視窗 ----


def test_insert_annotation_rejected(main_window, tmp_path, app, monkeypatch):
    _load_simple(main_window, tmp_path)
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.No),
    )
    before = main_window.view.toPlainText()
    main_window._insert_annotation()
    loop = QEventLoop(); QTimer.singleShot(100, loop.quit); loop.exec()
    after = main_window.view.toPlainText()
    assert before == after, "拒絕時不該改動文字"


def test_insert_annotation_accepted(main_window, tmp_path, app, monkeypatch):
    _load_simple(main_window, tmp_path)
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Yes),
    )
    # 把游標放在某處
    cur = main_window.view.textCursor()
    cur.setPosition(0)
    main_window.view.setTextCursor(cur)
    main_window._insert_annotation()
    loop = QEventLoop(); QTimer.singleShot(200, loop.quit); loop.exec()
    assert "<!--" in main_window.view.toPlainText(), "同意後應插入 <!-- --> 註解"


def test_compact_whitespace_rejected(main_window, tmp_path, app, monkeypatch):
    # 造有多餘空白的文字
    _load_simple(main_window, tmp_path, "a\n\n\n\nb\n\n\nc\n")
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.No),
    )
    before = main_window.view.toPlainText()
    main_window._compact_whitespace()
    loop = QEventLoop(); QTimer.singleShot(100, loop.quit); loop.exec()
    assert main_window.view.toPlainText() == before


# ---- edit_toolbar 永遠可見 ----


def test_edit_actions_on_annotation_toolbar(main_window):
    """編輯 actions 已併入 annotation_toolbar；edit_toolbar 被停用。"""
    anno = main_window.annotation_toolbar.actions()
    assert main_window.act_edit_mode in anno
    assert main_window.act_bold in anno
    assert main_window.act_insert_annotation in anno
    # 原 edit_toolbar 改為隱藏
    assert not main_window.edit_toolbar.isVisible()


def test_all_edit_actions_always_enabled(main_window):
    """所有 edit_toolbar action 永遠 enabled。"""
    for act in (
        main_window.act_bold, main_window.act_italic, main_window.act_underline,
        main_window.act_highlight, main_window.act_clear_fmt,
        main_window.act_clear_all_fmt,
        main_window.act_insert_annotation, main_window.act_compact_ws,
    ):
        assert act.isEnabled(), f"{act.text()} 應 enabled"


# ---- 🖍 螢光筆搬到 annotation_toolbar ----


def test_highlight_moved_to_annotation_toolbar(main_window):
    """🖍 螢光筆 應該在 annotation_toolbar，不再在 edit_toolbar。"""
    assert main_window.act_highlight in main_window.annotation_toolbar.actions()
    assert main_window.act_highlight not in main_window.edit_toolbar.actions()


def test_highlight_uses_tool_color(main_window, tmp_path, app):
    """螢光筆顏色跟著 _tool_color（顏色按鈕會同時影響鉛筆 + 螢光筆）。"""
    _load_simple(main_window, tmp_path)
    # 換色到紅
    main_window._set_annotation_color("#F44336")
    cur = main_window.view.textCursor()
    cur.setPosition(0)
    cur.setPosition(3, QTextCursor.MoveMode.KeepAnchor)
    main_window.view.setTextCursor(cur)
    main_window.view.toggle_highlight()
    spans = main_window.view.dump_format_spans()
    hl = [s for s in spans if s.highlight]
    assert hl, "套上螢光筆"
    # 顏色應接近紅（不是預設的黃）
    stored = hl[0].highlight_color.lower()
    assert "f4" in stored or "#f4" in stored, f"顏色應是紅色，不是 {stored}"


# ---- 貼完便利貼自動回指標 ----


def test_sticky_note_auto_switches_back_to_pointer(main_window, tmp_path, app, monkeypatch):
    """貼完便利貼 → tool 自動切回 pointer。"""
    from PySide6.QtWidgets import QInputDialog

    _load_simple(main_window, tmp_path)
    monkeypatch.setattr(
        QInputDialog, "getMultiLineText",
        staticmethod(lambda *a, **kw: ("筆記", True)),
    )
    # 切到便利貼工具
    main_window._set_annotation_tool("note")
    assert main_window.view.current_tool() == "note"
    # 貼一個便利貼（透過 view 的 add 方法）
    from PySide6.QtCore import QPoint
    main_window.view._add_sticky_note_at_viewport(QPoint(50, 50))
    loop = QEventLoop(); QTimer.singleShot(50, loop.quit); loop.exec()
    # 工具應自動切回 pointer
    assert main_window.view.current_tool() == "pointer"


# ---- 兩個清除按鈕都要確認 ----


def test_clear_all_formatting_needs_confirmation(main_window, tmp_path, app, monkeypatch):
    """❌ 清文字格式 按下 → 彈確認視窗；No → 格式不變。"""
    _load_simple(main_window, tmp_path)
    # 套粗體
    cur = main_window.view.textCursor()
    cur.setPosition(0)
    cur.setPosition(3, QTextCursor.MoveMode.KeepAnchor)
    main_window.view.setTextCursor(cur)
    main_window.view.toggle_bold()
    assert any(s.bold for s in main_window.view.dump_format_spans())
    # 拒絕確認 → 不動
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.No),
    )
    main_window._clear_all_formatting()
    assert any(s.bold for s in main_window.view.dump_format_spans()), "拒絕確認不該清"
    # 同意 → 清
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Yes),
    )
    main_window._clear_all_formatting()
    assert not any(s.bold for s in main_window.view.dump_format_spans()), "同意後應清"
