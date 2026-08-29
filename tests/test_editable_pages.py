"""每一頁的講稿區都要能點擊定位游標並輸入。

回歸背景：載入 N 頁投影片但講稿只有 M 頁（M < N）時，多出來的頁在
PrompterView 裡是「只有空白高度、沒有 QTextBlock」的虛擬頁，
游標放不進去 → 使用者只能編輯第 1 頁。
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytest.importorskip("fitz")

from teleprompter.core.transcript_loader import load_from_string  # noqa: E402


# ============================================================
# 分頁口徑（parser 端）
# ============================================================

def test_heading_only_page_is_kept():
    """只有 `# Slide N` 標題、沒有句子的頁不得被丟掉——否則頁數會與投影片對不上。"""
    t = load_from_string("# Slide 1\n\n第一頁內容。\n\n---\n\n# Slide 2\n\n---\n\n# Slide 3\n\n第三頁。")
    assert len(t.pages) == 3
    empty = t.pages[1]
    assert empty.sentence_start == empty.sentence_end     # 空頁
    assert empty.contains_sentence(empty.sentence_start) is False


def test_four_dash_separator_counts_the_same():
    """`----`（4 個以上）與 `---` 都是分頁符，兩邊口徑必須一致。"""
    three = load_from_string("A。\n\n---\n\nB。")
    four = load_from_string("A。\n\n----\n\nB。")
    assert len(three.pages) == len(four.pages) == 2


# ============================================================
# 實際可編輯性（view 端）
# ============================================================

@pytest.fixture(scope="module")
def app():
    import sys

    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setattr(
        "teleprompter.ui.main_window.default_sessions_path",
        lambda: sd / "sessions.json",
    )
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **kw: QMessageBox.StandardButton.Discard,
    )
    return sd


@pytest.fixture
def pdf_5(tmp_path):
    import fitz
    doc = fitz.open()
    for i in range(5):
        page = doc.new_page(width=960, height=540)
        page.insert_text((50, 72), f"Slide {i + 1}", fontsize=28)
    out = tmp_path / "deck.pdf"
    doc.save(str(out))
    doc.close()
    return out


def _make_mw(app):
    from teleprompter.config import load_config
    from teleprompter.ui.main_window import MainWindow
    w = MainWindow(load_config())
    w.resize(1400, 900)          # 橫屏 → split 走 PrompterView
    w.show()
    app.processEvents()
    time.sleep(0.05)
    app.processEvents()
    return w


def _enter_edit(w, app):
    w.act_edit_mode.setChecked(True)
    app.processEvents()
    time.sleep(0.05)
    app.processEvents()


def test_edit_mode_gives_every_slide_a_block(app, sessions_dir, pdf_5, tmp_path):
    """5 頁投影片 + 1 頁講稿 → 進編輯模式後，文件裡要有 5 頁區塊。"""
    script = tmp_path / "s.txt"
    script.write_text("只有一頁的講稿。", encoding="utf-8")

    w = _make_mw(app)
    w.load_file(str(script))
    app.processEvents()
    w.load_slides(pdf_5)
    app.processEvents()
    assert w.slide_deck.page_count == 5

    _enter_edit(w, app)
    assert w.view.page_count_from_blocks() == 5
    assert len(w.transcript.pages) == 5
    w.close()
    app.processEvents()


def test_cursor_can_land_on_every_page(app, sessions_dir, pdf_5, tmp_path):
    """對每一頁的垂直中點做 hit-test，游標要落在該頁自己的區段內。"""
    from PySide6.QtCore import QPoint

    script = tmp_path / "s.txt"
    script.write_text("只有一頁的講稿。", encoding="utf-8")
    w = _make_mw(app)
    w.load_file(str(script))
    app.processEvents()
    w.load_slides(pdf_5)
    app.processEvents()
    _enter_edit(w, app)

    view = w.view
    bounds = view._page_boundaries
    assert len(bounds) == 5, "每頁都要有邊界（含補出來的頁）"

    sb = view.verticalScrollBar()
    seen_blocks = set()
    for idx, (top_y, bottom_y) in enumerate(bounds):
        mid_doc_y = (top_y + bottom_y) // 2
        sb.setValue(max(0, mid_doc_y - view.viewport().height() // 2))
        app.processEvents()
        vy = mid_doc_y - sb.value()
        cursor = view.cursorForPosition(QPoint(40, max(1, vy)))
        seen_blocks.add(cursor.blockNumber())
    # 每頁點到的 block 都不同 → 代表沒有全部被彈回第 1 頁
    assert len(seen_blocks) >= 4, f"多數頁應各自落在不同 block，實得 {seen_blocks}"
    w.close()
    app.processEvents()


def test_typing_on_a_later_page_lands_there(app, sessions_dir, pdf_5, tmp_path):
    """在第 4 頁的區塊輸入文字 → 文字要留在第 4 頁的段落，不是掉回第 1 頁。"""
    from PySide6.QtGui import QTextCursor

    script = tmp_path / "s.txt"
    script.write_text("第一頁講稿。", encoding="utf-8")
    w = _make_mw(app)
    w.load_file(str(script))
    app.processEvents()
    w.load_slides(pdf_5)
    app.processEvents()
    _enter_edit(w, app)

    text_before = w.view.toPlainText()
    marker_page = text_before.index("# Slide 4")
    cursor = w.view.textCursor()
    cursor.setPosition(marker_page + len("# Slide 4"))
    cursor.movePosition(QTextCursor.EndOfBlock)
    w.view.setTextCursor(cursor)
    w.view.insertPlainText("\n第四頁我打的字。")
    app.processEvents()

    after = w.view.toPlainText()
    seg = after[after.index("# Slide 4"):after.index("# Slide 5")]
    assert "第四頁我打的字。" in seg, "輸入的文字必須留在第 4 頁區段"
    w.close()
    app.processEvents()


def test_deleting_everything_in_edit_mode_restores_pages(app, sessions_dir, pdf_5, tmp_path):
    """編輯中把講稿刪光 → 保底機制要把每頁區塊補回來（不然又只剩第 1 頁可編）。"""
    script = tmp_path / "s.txt"
    script.write_text("原本的講稿。", encoding="utf-8")
    w = _make_mw(app)
    w.load_file(str(script))
    app.processEvents()
    w.load_slides(pdf_5)
    app.processEvents()
    _enter_edit(w, app)
    assert w.view.page_count_from_blocks() == 5

    w.view.selectAll()
    w.view.insertPlainText("121212")     # 模擬使用者全選覆寫
    app.processEvents()
    time.sleep(0.05)
    app.processEvents()

    assert w.view.page_count_from_blocks() == 5, "刪光後仍應保有 5 頁可編輯區塊"
    assert "121212" in w.view.toPlainText()
    w.close()
    app.processEvents()
