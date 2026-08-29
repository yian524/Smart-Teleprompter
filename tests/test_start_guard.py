"""「▶ 開始」前置檢查：畫面有內容就要能開始，救不回才擋。

回歸背景：編輯模式中 transcript 落後於畫面、補頁流程 re-parse 出 0 句時
會靜默覆寫 self.transcript，導致「畫面滿滿是字卻彈『尚未載入講稿』」。
"""

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
def info_calls(monkeypatch):
    """攔截 QMessageBox.information，記錄 (標題, 內文)。"""
    calls: list[tuple[str, str]] = []
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda parent, title, text, *a, **kw: calls.append((title, text)),
    )
    return calls


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
    w.resize(1400, 900)
    w.show()
    app.processEvents()
    time.sleep(0.05)
    app.processEvents()
    return w


def test_start_check_passes_with_normal_transcript(app, sessions_dir, info_calls, tmp_path):
    script = tmp_path / "s.txt"
    script.write_text("這是一段正常的講稿內容。", encoding="utf-8")
    w = _make_mw(app)
    w.load_file(str(script))
    app.processEvents()

    assert w._ensure_startable_transcript() is True
    assert info_calls == []
    w.close()
    app.processEvents()


def test_start_during_edit_mode_commits_and_passes(app, sessions_dir, info_calls, tmp_path):
    """編輯模式中按開始 → 自動離開編輯（re-parse）→ 通過檢查，不彈窗。"""
    script = tmp_path / "s.txt"
    script.write_text("原始講稿。", encoding="utf-8")
    w = _make_mw(app)
    w.load_file(str(script))
    app.processEvents()

    w.act_edit_mode.setChecked(True)
    app.processEvents()
    w.view.insertPlainText("編輯中新增的一句。")
    app.processEvents()

    assert w._ensure_startable_transcript() is True
    assert w.act_edit_mode.isChecked() is False, "按開始應自動離開編輯模式"
    assert info_calls == []
    w.close()
    app.processEvents()


def test_stale_empty_transcript_rescued_from_view_text(app, sessions_dir, info_calls, tmp_path):
    """transcript 被搞成空（模擬補頁 re-parse 事故）但畫面有字 → 自動重建、不彈窗。"""
    from teleprompter.core.transcript_loader import load_from_string

    script = tmp_path / "s.txt"
    script.write_text("畫面上真的有內容。", encoding="utf-8")
    w = _make_mw(app)
    w.load_file(str(script))
    app.processEvents()

    # 硬塞一個 0 句 transcript（重現舊 bug 的狀態）
    w.transcript = load_from_string("# 只有標題")
    assert not w.transcript.sentences

    assert w._ensure_startable_transcript() is True
    assert w.transcript.sentences, "應已從畫面文字重建"
    assert info_calls == []
    w.close()
    app.processEvents()


def test_heading_only_view_shows_specific_message(app, sessions_dir, info_calls, tmp_path):
    """畫面只有標題/分頁符 → 彈「沒有可念內文」而非誤導的「尚未載入」。"""
    from teleprompter.core.transcript_loader import load_from_string

    w = _make_mw(app)
    w.view.set_text("# Slide 1\n\n---\n\n# Slide 2\n")
    w.transcript = load_from_string("# Slide 1\n\n---\n\n# Slide 2\n")

    assert w._ensure_startable_transcript() is False
    assert len(info_calls) == 1
    title, text = info_calls[0]
    assert "沒有可念" in title
    assert "尚未載入" not in title
    w.close()
    app.processEvents()


def test_truly_empty_still_blocks(app, sessions_dir, info_calls):
    w = _make_mw(app)
    w.view.set_text("")
    w.transcript = None
    assert w._ensure_startable_transcript() is False
    assert info_calls and info_calls[0][0] == "尚未載入講稿"
    w.close()
    app.processEvents()


def test_expand_never_installs_empty_transcript(app, sessions_dir, pdf_5, tmp_path):
    """補頁 re-parse 出 0 句時不得覆寫 self.transcript（防呆回歸）。"""
    script = tmp_path / "s.txt"
    script.write_text("有句子的講稿。", encoding="utf-8")
    w = _make_mw(app)
    w.load_file(str(script))
    app.processEvents()
    w.load_slides(pdf_5)
    app.processEvents()

    # 把畫面文字改成只剩標題（模擬使用者清空內文），再觸發補頁
    w.view.set_text("# Slide 1")
    before = w.transcript
    w._expand_transcript_for_slides()
    app.processEvents()

    assert w.transcript is not None
    assert w.transcript.sentences, "補頁不得把 transcript 換成 0 句"
    assert w.transcript is before, "0 句結果應被丟棄、保留原 transcript"
    w.close()
    app.processEvents()
