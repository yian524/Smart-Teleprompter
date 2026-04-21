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


def test_edit_toolbar_always_visible(main_window):
    """edit_toolbar 在任何模式都應可見。"""
    assert main_window.edit_toolbar.isVisible()
    main_window.view.set_edit_mode(True)
    assert main_window.edit_toolbar.isVisible()
    main_window.view.set_edit_mode(False)
    assert main_window.edit_toolbar.isVisible()


def test_all_edit_actions_always_enabled(main_window):
    """所有 edit_toolbar action 永遠 enabled。"""
    for act in (
        main_window.act_bold, main_window.act_italic, main_window.act_underline,
        main_window.act_highlight, main_window.act_clear_fmt,
        main_window.act_clear_all_fmt,
        main_window.act_insert_annotation, main_window.act_compact_ws,
    ):
        assert act.isEnabled(), f"{act.text()} 應 enabled"
