"""Click-to-edit: 使用者雙擊講稿位置 → 對話框問是否編輯。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer
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
    # 預設吃掉所有非 click-to-edit 的 question dialog
    cfg = load_config()
    w = MainWindow(cfg)
    w.show()
    loop = QEventLoop(); QTimer.singleShot(200, loop.quit); loop.exec()
    yield w
    w.close()


def test_click_yes_enters_edit_mode_with_cursor_positioned(main_window, tmp_path, monkeypatch):
    """使用者對話框按 Yes → 進入編輯模式，游標停在點擊位置。"""
    sample = tmp_path / "s.txt"
    sample.write_text(
        "第一段的內容。\n第二段也有內容。\n第三段在這裡。\n",
        encoding="utf-8",
    )
    main_window.load_file(str(sample))
    loop = QEventLoop(); QTimer.singleShot(150, loop.quit); loop.exec()

    # 模擬使用者按 Yes
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Yes),
    )
    assert not main_window.view.is_edit_mode()
    main_window._on_view_clicked(10)   # 假設 char 10 是「二段也」附近
    loop = QEventLoop(); QTimer.singleShot(50, loop.quit); loop.exec()
    # 編輯模式被打開
    assert main_window.view.is_edit_mode()
    # 游標位置被定位
    assert main_window.view.textCursor().position() == 10


def test_click_no_jumps_without_editing(main_window, tmp_path, monkeypatch):
    """按 No → 不編輯，只跳到該位置（原本的行為）。"""
    sample = tmp_path / "s.txt"
    sample.write_text(
        "句子一。\n句子二。\n句子三。\n",
        encoding="utf-8",
    )
    main_window.load_file(str(sample))
    loop = QEventLoop(); QTimer.singleShot(150, loop.quit); loop.exec()

    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **kw: QMessageBox.StandardButton.No),
    )
    assert not main_window.view.is_edit_mode()
    before_sent = main_window.engine.current_sentence_index
    # 跳到某個 char
    main_window._on_view_clicked(main_window.transcript.sentences[1].start)
    loop = QEventLoop(); QTimer.singleShot(50, loop.quit); loop.exec()
    # 不該進編輯模式
    assert not main_window.view.is_edit_mode()
    # 念稿位置已更新
    assert main_window.engine.current_sentence_index != before_sent
