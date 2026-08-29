"""拖放載入：把講稿或投影片直接拖進視窗就載入。"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytest.importorskip("fitz")


@pytest.fixture(scope="module")
def app():
    import sys

    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    from teleprompter.config import AppConfig
    from teleprompter.ui import main_window as mw
    from PySide6.QtWidgets import QMessageBox

    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setattr(mw, "default_sessions_path", lambda: sd / "sessions.json")
    monkeypatch.setattr(mw, "save_config", lambda cfg: None)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **kw: QMessageBox.StandardButton.Discard,
    )
    w = mw.MainWindow(AppConfig())
    w.resize(1400, 800)
    w.show()
    app.processEvents()
    time.sleep(0.05)
    app.processEvents()
    yield w
    w.close()
    app.processEvents()


@pytest.fixture
def script_file(tmp_path):
    f = tmp_path / "talk.txt"
    f.write_text("拖進來的講稿內容。", encoding="utf-8")
    return f


@pytest.fixture
def pdf_file(tmp_path):
    import fitz
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page(width=960, height=540)
        page.insert_text((50, 72), f"Slide {i + 1}", fontsize=24)
    out = tmp_path / "deck.pdf"
    doc.save(str(out))
    doc.close()
    return out


def test_window_accepts_drops(win):
    assert win.acceptDrops()


def test_classify_by_suffix(win):
    scripts, slides = win._classify_dropped(
        ["a.txt", "b.md", "c.docx", "d.pdf", "e.pptx", "f.zip", "g.exe"]
    )
    assert scripts == ["a.txt", "b.md", "c.docx"]
    assert slides == ["d.pdf", "e.pptx"]


def test_drop_script_loads_it(win, app, script_file):
    assert win.load_dropped_paths([str(script_file)]) is True
    app.processEvents()
    assert win.transcript is not None
    assert "拖進來的講稿內容" in win.transcript.full_text


def test_drop_slides_loads_deck(win, app, pdf_file):
    win.load_dropped_paths([str(pdf_file)])
    app.processEvents()
    assert win.slide_deck is not None
    assert win.slide_deck.page_count == 3


def test_drop_both_at_once(win, app, script_file, pdf_file):
    """一次拖講稿＋投影片：兩個都要載入。"""
    win.load_dropped_paths([str(script_file), str(pdf_file)])
    app.processEvents()
    assert win.transcript is not None
    assert win.slide_deck is not None
    assert win.slide_deck.page_count == 3


def test_unsupported_file_is_reported_not_crash(win, app, tmp_path):
    junk = tmp_path / "photo.jpg"
    junk.write_bytes(b"not a real image")
    assert win.load_dropped_paths([str(junk)]) is False
    assert "不支援" in win.status_recognized.text()
    app.processEvents()
    assert win.transcript is None or not win.transcript.full_text.strip()


def test_mixed_drop_reports_ignored_count(win, app, script_file, tmp_path):
    junk = tmp_path / "note.zip"
    junk.write_bytes(b"zip")
    assert win.load_dropped_paths([str(script_file), str(junk)]) is True
    text = win.status_recognized.text()   # 先讀，避免被之後的事件覆蓋
    app.processEvents()
    assert "已載入" in text
    assert "略過" in text


def test_drag_enter_previews_what_will_load(win, script_file, pdf_file):
    """拖到視窗上方時就先告訴使用者放開會載入什麼。"""
    text = win.describe_drop([str(script_file)])
    assert "放開以載入" in text
    assert "talk.txt" in text

    both = win.describe_drop([str(script_file), str(pdf_file)])
    assert "講稿" in both and "投影片" in both
