"""文字格式 dump/restore 測試。"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


@pytest.fixture
def qt_app():
    from PySide6.QtWidgets import QApplication
    import sys
    app = QApplication.instance() or QApplication(sys.argv)
    return app


def _make_doc(text: str):
    from PySide6.QtGui import QTextDocument
    doc = QTextDocument()
    doc.setPlainText(text)
    return doc


def _apply(doc, start: int, end: int, *,
           bold=False, italic=False, underline=False, highlight=False):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
    from teleprompter.core.rich_text_format import HIGHLIGHT_COLOR_HEX

    cursor = QTextCursor(doc)
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    fmt = QTextCharFormat()
    if bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    if italic:
        fmt.setFontItalic(True)
    if underline:
        fmt.setFontUnderline(True)
    if highlight:
        fmt.setBackground(QColor(HIGHLIGHT_COLOR_HEX))
    cursor.mergeCharFormat(fmt)


def test_empty_doc_has_no_spans(qt_app):
    from teleprompter.core.rich_text_format import dump_formats
    doc = _make_doc("")
    assert dump_formats(doc) == []


def test_plain_doc_has_no_spans(qt_app):
    from teleprompter.core.rich_text_format import dump_formats
    doc = _make_doc("hello world")
    assert dump_formats(doc) == []


def test_dump_bold_span(qt_app):
    from teleprompter.core.rich_text_format import dump_formats
    doc = _make_doc("abcdefghij")  # len=10
    _apply(doc, 2, 5, bold=True)
    spans = dump_formats(doc)
    assert len(spans) == 1
    s = spans[0]
    assert s.start == 2 and s.end == 5
    assert s.bold and not s.italic and not s.underline and not s.highlight


def test_dump_multiple_non_adjacent_spans(qt_app):
    from teleprompter.core.rich_text_format import dump_formats
    doc = _make_doc("0123456789abcdef")  # len=16
    _apply(doc, 2, 4, bold=True)
    _apply(doc, 8, 12, italic=True, highlight=True)
    spans = dump_formats(doc)
    assert len(spans) == 2
    spans.sort(key=lambda s: s.start)
    assert spans[0].start == 2 and spans[0].end == 4 and spans[0].bold
    assert spans[1].start == 8 and spans[1].end == 12
    assert spans[1].italic and spans[1].highlight


def test_roundtrip_restore(qt_app):
    from teleprompter.core.rich_text_format import dump_formats, restore_formats
    doc1 = _make_doc("hello world foo bar")
    _apply(doc1, 0, 5, bold=True)
    _apply(doc1, 6, 11, italic=True)
    _apply(doc1, 12, 15, highlight=True)
    spans = dump_formats(doc1)

    doc2 = _make_doc("hello world foo bar")
    restore_formats(doc2, spans)
    spans2 = dump_formats(doc2)
    # 排序後應相等
    spans.sort(key=lambda s: s.start)
    spans2.sort(key=lambda s: s.start)
    assert spans == spans2


def test_restore_drops_out_of_range(qt_app):
    """end 遠大於 text_len（>1.05x）的 span 直接丟棄，不夾到 text_len。

    理由：若使用者剛套 biuh 到選取，textCursor 上的 charFormat 會繼承 biuh；
    此時若有壞 span 被「夾」到整篇，會污染所有字元。統一丟棄避免擴散。
    """
    from teleprompter.core.rich_text_format import FormatSpan, restore_formats, dump_formats
    doc = _make_doc("short")  # len=5
    restore_formats(doc, [FormatSpan(start=0, end=100, bold=True)])
    spans = dump_formats(doc)
    assert spans == [], "out-of-range span 應該被丟棄不套用"


def test_restore_in_range_still_applied(qt_app):
    """正常 in-range span 仍正常套用。"""
    from teleprompter.core.rich_text_format import FormatSpan, restore_formats, dump_formats
    doc = _make_doc("hello world")  # len=11
    restore_formats(doc, [FormatSpan(start=0, end=5, bold=True)])
    spans = dump_formats(doc)
    assert len(spans) == 1
    assert spans[0].start == 0 and spans[0].end == 5 and spans[0].bold


def test_spans_multiline_block(qt_app):
    from teleprompter.core.rich_text_format import dump_formats
    # 包含 newline（block 間）
    doc = _make_doc("line one\nline two")  # '\n' at index 8
    _apply(doc, 5, 8, bold=True)   # "one"
    _apply(doc, 9, 13, italic=True)  # "line" in line two
    spans = dump_formats(doc)
    assert len(spans) == 2
    spans.sort(key=lambda s: s.start)
    assert spans[0].start == 5 and spans[0].end == 8 and spans[0].bold
    assert spans[1].start == 9 and spans[1].end == 13 and spans[1].italic


def test_format_span_to_dict_roundtrip():
    from teleprompter.core.rich_text_format import FormatSpan
    s1 = FormatSpan(start=3, end=7, bold=True, highlight=True)
    d = s1.to_dict()
    s2 = FormatSpan.from_dict(d)
    assert s1 == s2
