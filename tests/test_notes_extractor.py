"""講稿自動抽取（PPTX 備忘稿 / HTML notes / PDF 文字）與預覽對話框。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from teleprompter.core.notes_extractor import (  # noqa: E402
    ExtractedNotes,
    extract_from_html,
    extract_notes,
)

SAMPLES = Path(__file__).resolve().parent.parent / "src" / "teleprompter" / "resources" / "samples"


# ============================================================
# 抽取器
# ============================================================

def test_bundled_sample_pptx_has_notes():
    """內建範例必須帶得動整條流程（也是回歸保護：素材不能被誤刪）。"""
    pptx = SAMPLES / "範例投影片_含講稿.pptx"
    assert pptx.exists(), "內建範例投影片不存在"
    notes = extract_notes(pptx)
    assert notes.source == "pptx"
    assert notes.page_count == 6
    assert notes.filled_count == 6           # 每頁都有備忘稿
    assert "報告人" in notes.pages[0]
    assert notes.titles[0] == "研究背景與動機"


def test_html_speaker_notes_attribute(tmp_path):
    html = tmp_path / "deck.html"
    html.write_text(
        '<section data-label="01 Cover" data-speaker-notes="第一頁講稿&#10;&#10;'
        '[Sources]&#10;不該被唸出來">A</section>'
        '<section data-speaker-notes="第二頁講稿">B</section>',
        encoding="utf-8",
    )
    notes = extract_from_html(html)
    assert notes.page_count == 2
    assert notes.pages[0] == "第一頁講稿"      # [Sources] 私有段落要被剝掉
    assert notes.titles[0] == "01 Cover"
    assert notes.pages[1] == "第二頁講稿"


def test_html_aside_notes_fallback(tmp_path):
    html = tmp_path / "reveal.html"
    html.write_text(
        '<section><h2>標題</h2><aside class="notes">reveal 風格的講稿</aside></section>',
        encoding="utf-8",
    )
    notes = extract_from_html(html)
    assert notes.pages[0] == "reveal 風格的講稿"


def test_pdf_text_fallback(tmp_path):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page(width=960, height=540)
    # 用英文：fitz 內建字型不含中文字，寫中文會變成方框（測資限制，非程式問題）
    page.insert_text((50, 72), "Title Line", fontsize=24)
    page.insert_text((50, 140), "First body sentence.", fontsize=16)
    pdf = tmp_path / "a.pdf"
    doc.save(str(pdf))
    doc.close()

    notes = extract_notes(pdf)
    assert notes.source == "pdf"
    assert notes.titles[0] == "Title Line"
    assert "First body sentence." in notes.pages[0]


def test_unsupported_format_returns_empty(tmp_path):
    f = tmp_path / "x.docx"
    f.write_text("whatever", encoding="utf-8")
    notes = extract_notes(f)
    assert notes.page_count == 0
    assert notes.filled_count == 0


def test_to_transcript_text_shapes_standard_format():
    notes = ExtractedNotes(source="pptx", pages=["第一頁內容", ""], titles=["開場", ""])
    text = notes.to_transcript_text()
    assert "# Slide 1 · 開場" in text
    assert "# Slide 2" in text
    assert "---" in text          # 分頁符號
    assert text.count("---") == 1  # 兩頁 → 一個分隔


# ============================================================
# 預覽／編輯對話框
# ============================================================

@pytest.fixture(scope="module")
def app():
    import sys

    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)


def test_dialog_lists_pages_and_edits_flow_through(app):
    from teleprompter.ui.notes_import_dialog import NotesImportDialog

    notes = ExtractedNotes(source="pptx",
                           pages=["原始第一頁", "原始第二頁"],
                           titles=["A", "B"])
    dlg = NotesImportDialog(notes)
    assert dlg.page_list.count() == 2
    # 切到第二頁 → 編輯 → 內容要進到輸出
    dlg.page_list.setCurrentRow(1)
    app.processEvents()
    assert dlg.editor.toPlainText() == "原始第二頁"
    dlg.editor.setPlainText("我改過的第二頁")
    app.processEvents()

    out = dlg.transcript_text()
    assert "原始第一頁" in out
    assert "我改過的第二頁" in out
    assert "原始第二頁" not in out
    assert dlg.skipped is False
    dlg.deleteLater()


def test_dialog_skip_flag(app):
    from teleprompter.ui.notes_import_dialog import NotesImportDialog

    dlg = NotesImportDialog(ExtractedNotes(source="pdf", pages=["x"], titles=[""]))
    dlg._on_skip()
    assert dlg.skipped is True
    dlg.deleteLater()


def test_empty_page_marker_updates_on_edit(app):
    """空白頁標 ⚠️，打字後要變 ✅（給使用者即時回饋）。"""
    from teleprompter.ui.notes_import_dialog import NotesImportDialog

    dlg = NotesImportDialog(ExtractedNotes(source="pdf", pages=[""], titles=["空頁"]))
    assert dlg.page_list.item(0).text().startswith("⚠️")
    dlg.editor.setPlainText("補上的講稿")
    app.processEvents()
    assert dlg.page_list.item(0).text().startswith("✅")
    dlg.deleteLater()
