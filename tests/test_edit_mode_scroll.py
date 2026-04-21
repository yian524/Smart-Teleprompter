"""Regression：編輯模式下輸入字元後滾動，不應被 MD refresh 的 setTextCursor 拉回游標。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QTextCursor
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
        QMessageBox, "question", lambda *a, **kw: QMessageBox.StandardButton.Discard
    )
    cfg = load_config()
    w = MainWindow(cfg)
    w.show()
    w.resize(1200, 800)
    loop = QEventLoop(); QTimer.singleShot(200, loop.quit); loop.exec()
    yield w
    w.close()


def test_edit_mode_preserves_scroll_across_md_refresh(main_window, tmp_path, app):
    """使用者情境：
    1. 進編輯模式、游標在頂端
    2. 輸入一個字元（觸發 MD refresh debounce 220ms）
    3. 立刻往下捲
    4. 220ms 後 MD refresh 執行 → scroll 不得被拉回游標（頂端）
    """
    # 造一份夠長的講稿，確保 scroll bar 有空間
    # （1200 行 × 中英文填充，跨分頁情境、跨 viewport 尺寸都有 scroll）
    sample = tmp_path / "long.txt"
    long_text = "\n".join(
        [f"第 {i} 行的內容，這是一些填充文字供測試 scroll 使用。" for i in range(1200)]
    )
    sample.write_text(long_text, encoding="utf-8")
    main_window.load_file(str(sample))
    loop = QEventLoop(); QTimer.singleShot(200, loop.quit); loop.exec()

    main_window.view.set_edit_mode(True)
    app.processEvents()

    # 把游標放到最頂端
    cur = main_window.view.textCursor()
    cur.setPosition(0)
    main_window.view.setTextCursor(cur)
    app.processEvents()

    # 輸入一個字元 → textChanged 觸發 MD debounce
    typing_cur = main_window.view.textCursor()
    typing_cur.insertText("x")
    app.processEvents()

    # 立刻把 scroll 拉到底（模擬使用者滾輪往下捲）
    sb = main_window.view.verticalScrollBar()
    assert sb.maximum() > 100, "講稿需要有 scroll 空間；測試資料太短"
    sb.setValue(sb.maximum())
    scroll_before_refresh = sb.value()

    # 等 MD refresh debounce 結束（220ms + 餘裕）
    loop = QEventLoop(); QTimer.singleShot(400, loop.quit); loop.exec()

    # 關鍵斷言：scroll 沒被 setTextCursor 拉回頂端
    assert sb.value() == scroll_before_refresh, (
        f"MD refresh 跳回游標了：scroll 從 {scroll_before_refresh} 變成 {sb.value()}。"
        "使用者會覺得「滑不下去」。"
    )


def test_edit_mode_scroll_preserved_even_with_selection(main_window, tmp_path, app):
    """有選取時 MD refresh 也不該動 scroll。"""
    sample = tmp_path / "long.txt"
    long_text = "\n".join([f"第 {i} 行。" for i in range(200)])
    sample.write_text(long_text, encoding="utf-8")
    main_window.load_file(str(sample))
    loop = QEventLoop(); QTimer.singleShot(200, loop.quit); loop.exec()

    main_window.view.set_edit_mode(True)
    app.processEvents()

    # 在頂端選幾個字
    cur = main_window.view.textCursor()
    cur.setPosition(0)
    cur.setPosition(5, QTextCursor.MoveMode.KeepAnchor)
    main_window.view.setTextCursor(cur)
    app.processEvents()

    # 插入（會觸發 MD debounce）
    cur = main_window.view.textCursor()
    cur.insertText("y")
    app.processEvents()

    sb = main_window.view.verticalScrollBar()
    sb.setValue(sb.maximum() // 2)
    mid = sb.value()

    loop = QEventLoop(); QTimer.singleShot(400, loop.quit); loop.exec()

    assert sb.value() == mid, f"有選取時 scroll 也不該跳：{mid} → {sb.value()}"
