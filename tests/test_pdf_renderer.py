"""PDF 渲染單元測試。

用 PyMuPDF 動態產生一份極小 PDF（3 頁），避免依賴外部 fixture。
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Qt application fixture 必要（QPixmap 需要 QApplication）
pytest.importorskip("PySide6")
pytest.importorskip("fitz")


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication
    import sys
    app = QApplication.instance() or QApplication(sys.argv)
    return app


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    """動態產生 3 頁 PDF。"""
    import fitz  # PyMuPDF

    doc = fitz.open()
    for i in range(3):
        page = doc.new_page(width=595, height=842)  # A4
        page.insert_text((50, 72), f"Slide {i + 1}", fontsize=24)
    out = tmp_path / "sample.pdf"
    doc.save(str(out))
    doc.close()
    return out


def test_slide_deck_opens_and_reports_page_count(sample_pdf, qt_app):
    from teleprompter.core.pdf_renderer import SlideDeck

    deck = SlideDeck(sample_pdf)
    assert deck.page_count == 3
    assert len(deck.pages) == 3
    assert deck.pages[0].number == 1
    assert deck.pages[2].number == 3
    deck.close()


def test_render_returns_pixmap(sample_pdf, qt_app):
    from teleprompter.core.pdf_renderer import SlideDeck

    deck = SlideDeck(sample_pdf)
    pix = deck.render(1, width_px=400)
    assert pix is not None
    assert not pix.isNull()
    assert pix.width() == 400
    # 高度應該等比（842/595 ≈ 1.415）
    assert 550 <= pix.height() <= 580
    deck.close()


def test_render_out_of_range_returns_none(sample_pdf, qt_app):
    from teleprompter.core.pdf_renderer import SlideDeck

    deck = SlideDeck(sample_pdf)
    assert deck.render(0, 400) is None
    assert deck.render(99, 400) is None
    deck.close()


def test_thumbnail_cached(sample_pdf, qt_app):
    from teleprompter.core.pdf_renderer import SlideDeck

    deck = SlideDeck(sample_pdf)
    t1 = deck.thumbnail(2)
    t2 = deck.thumbnail(2)
    assert t1 is t2  # same cached object
    assert t1.width() == 160
    deck.close()


def test_render_cache_evicts_after_limit(sample_pdf, qt_app):
    from teleprompter.core.pdf_renderer import SlideDeck

    deck = SlideDeck(sample_pdf)
    # 觸發 15 次不同 (page, width) 渲染，超過 LRU=12 上限
    for i in range(15):
        deck.render(1 + (i % 3), width_px=200 + i * 10)
    assert len(deck._render_cache) <= 12
    deck.close()


def test_load_slide_deck_missing_file_raises(tmp_path, qt_app):
    from teleprompter.core.pdf_renderer import load_slide_deck

    with pytest.raises(FileNotFoundError):
        load_slide_deck(tmp_path / "nope.pdf")


def test_load_slide_deck_valid_file(sample_pdf, qt_app):
    from teleprompter.core.pdf_renderer import load_slide_deck

    deck = load_slide_deck(sample_pdf)
    assert deck.page_count == 3
    deck.close()
