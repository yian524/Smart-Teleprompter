"""Regression：使用者選取小段文字 → 離開編輯模式 → 格式不得擴散到整篇。

對應使用者回報的 bug：進編輯模式選一小段加粗體/斜體/底線/螢光後，
按下離開編輯模式 → 整篇講稿都被套上該格式。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QMessageBox

from teleprompter.config import load_config
from teleprompter.core.rich_text_format import (
    FormatSpan,
    dump_formats,
    restore_formats,
)
from teleprompter.ui import main_window as mw_mod
from teleprompter.ui.main_window import MainWindow


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_restore_formats_drops_out_of_bounds_span(app):
    """restore_formats 收到 end 遠大於 text_len 的壞 span → 整條丟棄，不夾。"""
    from PySide6.QtGui import QTextDocument

    doc = QTextDocument()
    doc.setPlainText("hello world")  # len=11
    bad_span = FormatSpan(start=0, end=999999, italic=True, underline=True, highlight=True)
    restore_formats(doc, [bad_span])
    # 壞 span 應被丟棄 → 沒有任何 italic/underline/highlight 被套用
    spans_after = dump_formats(doc)
    assert spans_after == [], f"壞 span 不應被套用，但 dump 出 {spans_after}"


def test_restore_formats_applies_in_bounds_span(app):
    """in-bounds span 正常套用。"""
    from PySide6.QtGui import QTextDocument

    doc = QTextDocument()
    doc.setPlainText("hello world")  # len=11
    good_span = FormatSpan(start=0, end=5, italic=True)
    restore_formats(doc, [good_span])
    spans_after = dump_formats(doc)
    assert any(s.italic and s.start == 0 and s.end == 5 for s in spans_after)


def test_format_does_not_spread_after_exit_edit_mode(app, tmp_path, monkeypatch):
    """重現使用者報告的 bug：選一小段 → B/I/U/H → 離開編輯模式 → 格式應只在選取範圍。"""
    monkeypatch.setattr(
        mw_mod, "default_sessions_path", lambda: tmp_path / "sessions.json"
    )
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **kw: QMessageBox.StandardButton.Discard
    )

    cfg = load_config()
    w = MainWindow(cfg)
    w.show()
    app.processEvents()

    sample_path = tmp_path / "sample.txt"
    sample_path.write_text(
        "<!-- 備忘: 開場白 -->\n"
        "# 第一頁\n"
        "大家好我是報告人。\n"
        "今天要跟各位分享的主題是 Transformer 架構。\n"
        "接下來我會用三個部分說明。\n",
        encoding="utf-8",
    )
    w.load_file(str(sample_path))
    app.processEvents()

    w.view.set_edit_mode(True)
    app.processEvents()

    plain = w.view.toPlainText()
    needle = "今天要跟各位分享的主題是"
    idx = plain.find(needle)
    assert idx >= 0, f"測試資料異常：找不到 {needle!r}"

    cur = w.view.textCursor()
    cur.setPosition(idx)
    cur.setPosition(idx + len(needle), QTextCursor.MoveMode.KeepAnchor)
    w.view.setTextCursor(cur)

    w.act_bold.trigger()
    w.act_italic.trigger()
    w.act_underline.trigger()
    w.act_highlight.trigger()
    app.processEvents()
    # 讓 MD debounce 完全跑完
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(400, loop.quit)
    loop.exec()

    # 離開編輯模式
    w.view.set_edit_mode(False)
    app.processEvents()
    loop2 = QEventLoop()
    QTimer.singleShot(400, loop2.quit)
    loop2.exec()

    spans = w.view.dump_format_spans()
    # underline / highlight 只有使用者會加（MD 不會）→ 用它們找「使用者 spans」
    user_spans = [s for s in spans if s.underline or s.highlight]
    plain_after = w.view.toPlainText()
    for s in user_spans:
        covered = plain_after[s.start : s.end]
        assert covered in needle, (
            f"格式擴散：span [{s.start}-{s.end}] = {covered!r}，"
            f"但使用者只選了 {needle!r}"
        )
        assert s.end - s.start <= len(needle), (
            f"格式範圍過大：span len={s.end - s.start} > needle len={len(needle)}"
        )

    w.close()
