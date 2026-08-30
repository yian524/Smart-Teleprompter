"""空分頁引導頁：新分頁不再是一片全黑。"""

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
    w.resize(1200, 700)
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
    f.write_text("這是一段講稿內容。", encoding="utf-8")
    return f


@pytest.fixture
def pdf_file(tmp_path):
    import fitz
    doc = fitz.open()
    for i in range(2):
        page = doc.new_page(width=960, height=540)
        page.insert_text((50, 72), f"Slide {i + 1}", fontsize=24)
    out = tmp_path / "deck.pdf"
    doc.save(str(out))
    doc.close()
    return out


def test_empty_tab_shows_guidance(win, app):
    """沒有講稿也沒有投影片 → 引導頁要出現。"""
    win.refresh_empty_state()
    app.processEvents()
    assert win.empty_state.isVisible()
    assert "拖進來" in win.empty_state.title.text()


def test_guidance_hides_after_loading_script(win, app, script_file):
    win.load_file(str(script_file))
    app.processEvents()
    assert not win.empty_state.isVisible()


def test_guidance_hides_after_loading_slides_only(win, app, pdf_file):
    """只載投影片、沒有講稿時也算有內容，引導頁要收起。"""
    win.load_slides(pdf_file)
    app.processEvents()
    assert not win.empty_state.isVisible()


def test_guidance_buttons_are_wired(win, app, monkeypatch):
    """三顆按鈕要接到真正的載入流程。"""
    called = []
    monkeypatch.setattr(win, "_open_file_from_disk", lambda: called.append("file"))
    monkeypatch.setattr(win, "_paste_text", lambda: called.append("paste"))
    monkeypatch.setattr(win, "load_sample_bundle", lambda: called.append("sample"))
    # 重新接線（fixture 建立時已連到原方法）
    win.empty_state.open_file_requested.connect(lambda: win._open_file_from_disk())
    win.empty_state.paste_requested.disconnect()
    win.empty_state.paste_requested.connect(lambda: win._paste_text())
    win.empty_state.sample_requested.disconnect()
    win.empty_state.sample_requested.connect(lambda: win.load_sample_bundle())

    win.empty_state.btn_paste.click()
    win.empty_state.btn_sample.click()
    app.processEvents()
    assert "paste" in called and "sample" in called


def test_drag_highlight_changes_message(win, app):
    win.refresh_empty_state()
    app.processEvents()
    win.empty_state.set_drag_highlight(True)
    assert "放開" in win.empty_state.title.text()
    win.empty_state.set_drag_highlight(False)
    assert "拖進來" in win.empty_state.title.text()


def test_recent_files_are_remembered(win, app, script_file, pdf_file):
    """載入過的檔案要進最近清單，供引導頁與檔案選單使用。"""
    win.load_file(str(script_file))
    win.load_slides(pdf_file)
    app.processEvents()

    recent = win._recent_paths()
    assert str(script_file) in recent
    assert str(pdf_file) in recent


def test_recent_skips_missing_files(win, app, tmp_path):
    """檔案被移走後不該還列在最近使用。"""
    gone = tmp_path / "gone.txt"
    gone.write_text("x", encoding="utf-8")
    win.load_file(str(gone))
    app.processEvents()
    gone.unlink()
    assert str(gone) not in win._recent_paths()


def test_recent_menu_rebuilds(win, app, script_file):
    win.load_file(str(script_file))
    app.processEvents()
    win._rebuild_recent_menu()
    labels = [a.text() for a in win.menu_recent.actions()]
    assert "talk.txt" in labels


def test_recent_menu_shows_placeholder_when_empty(win, app):
    win.cfg = type(win.cfg)(**{**win.cfg.__dict__, "recent_scripts": "", "recent_slides": ""})
    win._rebuild_recent_menu()
    acts = win.menu_recent.actions()
    assert len(acts) == 1 and not acts[0].isEnabled()


def test_paste_names_the_tab(win, app, monkeypatch):
    """貼上文字後分頁標題不該停在「未命名」。"""
    from PySide6.QtWidgets import QInputDialog

    monkeypatch.setattr(
        QInputDialog, "getMultiLineText",
        staticmethod(lambda *a, **kw: ("今天要報告的是假訊息偵測研究。", True)),
    )
    win._paste_text()
    app.processEvents()
    active = win.session_manager.active
    assert active is not None
    assert active.title != "未命名"
    assert active.title.startswith("今天要報告")
