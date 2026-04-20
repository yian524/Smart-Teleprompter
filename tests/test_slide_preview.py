"""SlidePreviewPanel 單元測試（垂直捲動全頁版本）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("fitz")


@pytest.fixture
def qt_app():
    from PySide6.QtWidgets import QApplication
    import sys
    app = QApplication.instance() or QApplication(sys.argv)
    return app


@pytest.fixture
def sample_deck(tmp_path: Path, qt_app):
    import fitz
    from teleprompter.core.pdf_renderer import SlideDeck

    doc = fitz.open()
    for i in range(4):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 72), f"Slide {i + 1}", fontsize=24)
    pdf = tmp_path / "deck.pdf"
    doc.save(str(pdf))
    doc.close()
    deck = SlideDeck(pdf)
    yield deck
    deck.close()


def test_panel_initial_empty(qt_app):
    from teleprompter.ui.slide_preview import SlidePreviewPanel

    p = SlidePreviewPanel()
    assert p.page_count() == 0
    assert p.current_page() == 0


def test_set_deck_creates_page_widgets(qt_app, sample_deck):
    from teleprompter.ui.slide_preview import SlidePreviewPanel

    p = SlidePreviewPanel()
    p.set_deck(sample_deck, title="deck")
    assert p.page_count() == 4
    assert len(p._page_headers) == 4
    assert len(p._page_images) == 4
    assert p.current_page() == 1


def test_scroll_to_page_updates_current(qt_app, sample_deck):
    from teleprompter.ui.slide_preview import SlidePreviewPanel

    p = SlidePreviewPanel()
    p.resize(600, 800)
    p.set_deck(sample_deck)
    p.show()  # ensure layout geometry computed
    qt_app.processEvents()
    p.scroll_to_page(3)
    assert p.current_page() == 3


def test_scroll_to_page_out_of_range_ignored(qt_app, sample_deck):
    from teleprompter.ui.slide_preview import SlidePreviewPanel

    p = SlidePreviewPanel()
    p.set_deck(sample_deck)
    p.scroll_to_page(99)
    assert p.current_page() == 1


def test_clear_deck(qt_app, sample_deck):
    from teleprompter.ui.slide_preview import SlidePreviewPanel

    p = SlidePreviewPanel()
    p.set_deck(sample_deck)
    p.set_deck(None)
    assert p.page_count() == 0
    assert p._page_headers == []
