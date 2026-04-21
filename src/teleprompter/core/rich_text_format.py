"""文字格式 dump/restore：把 QTextDocument 的粗體/斜體/底線/螢光筆視覺標註
序列化為純資料，供 Session 持久化，並能還原到另一個 QTextDocument。

設計要點：
- 純文字內容是對齊引擎的 source of truth，格式只是視覺層。
- FormatSpan 的 (start, end) 是純文字字元 offset（不含 block break 外的任何魔法字元）。
- dump/restore 對 plainText 位置做 round-trip；若純文字被編輯過位置會失準（呼叫端要負責在正確時機 dump，再 restore）。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)


# 螢光筆底色（黃，半透明以便黑底白字仍可讀）
HIGHLIGHT_COLOR_HEX = "#FFEB3B"
HIGHLIGHT_ALPHA = 110   # ~43% 透明
# 偵測目前字元是否有螢光筆：看 background RGB 是否是我們的黃
HIGHLIGHT_RGB = QColor(HIGHLIGHT_COLOR_HEX).rgb() & 0x00FFFFFF


def highlight_brush_color() -> QColor:
    c = QColor(HIGHLIGHT_COLOR_HEX)
    c.setAlpha(HIGHLIGHT_ALPHA)
    return c


@dataclass
class FormatSpan:
    start: int
    end: int
    bold: bool = False
    italic: bool = False
    underline: bool = False
    highlight: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FormatSpan":
        return cls(**d)

    def is_empty(self) -> bool:
        return not (self.bold or self.italic or self.underline or self.highlight)


def _char_attrs(fmt: QTextCharFormat) -> tuple[bool, bool, bool, bool]:
    """(bold, italic, underline, highlight) 四旗標。"""
    bold = fmt.fontWeight() >= QFont.Weight.Bold
    italic = fmt.fontItalic()
    underline = fmt.fontUnderline()
    bg = fmt.background()
    highlight = False
    if bg.style() != Qt.BrushStyle.NoBrush:
        color = bg.color()
        if color.alpha() > 0 and (color.rgb() & 0x00FFFFFF) == HIGHLIGHT_RGB:
            highlight = True
    return bold, italic, underline, highlight


def dump_formats(doc: QTextDocument) -> list[FormatSpan]:
    """掃描整份 document，把連續相同格式的字元合併成 FormatSpan。
    純文字 offset 以 `doc.toPlainText()` 為準（block break = '\\n' 佔 1 char）。
    格式不跨 block：每個 block 結束時 flush 當前 span。
    """
    spans: list[FormatSpan] = []
    if doc.isEmpty():
        return spans

    block = doc.firstBlock()
    while block.isValid():
        block_start = block.position()
        cur_attrs: tuple[bool, bool, bool, bool] | None = None
        span_start_in_block = 0
        block_offset = 0
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid():
                frag_text = frag.text()
                frag_attrs = _char_attrs(frag.charFormat())
                for _ in frag_text:
                    if cur_attrs is None:
                        cur_attrs = frag_attrs
                        span_start_in_block = block_offset
                    elif frag_attrs != cur_attrs:
                        if any(cur_attrs):
                            spans.append(FormatSpan(
                                start=block_start + span_start_in_block,
                                end=block_start + block_offset,
                                bold=cur_attrs[0], italic=cur_attrs[1],
                                underline=cur_attrs[2], highlight=cur_attrs[3],
                            ))
                        cur_attrs = frag_attrs
                        span_start_in_block = block_offset
                    block_offset += 1
            it += 1
        # block 尾端 flush（不讓 span 跨 newline）
        if cur_attrs is not None and any(cur_attrs):
            spans.append(FormatSpan(
                start=block_start + span_start_in_block,
                end=block_start + block_offset,
                bold=cur_attrs[0], italic=cur_attrs[1],
                underline=cur_attrs[2], highlight=cur_attrs[3],
            ))
        block = block.next()

    return [s for s in spans if not s.is_empty() and s.end > s.start]


def restore_formats(doc: QTextDocument, spans: list[FormatSpan]) -> None:
    """把一系列 FormatSpan 套回 doc。

    **安全機制**：
    1. `end > text_len * 1.05` 的壞 span 直接丟棄（不夾到 text_len，避免擴散至整篇）
    2. 累計覆蓋 >80% → 視為壞資料，全部跳過
    """
    if not spans:
        return
    text_len = len(doc.toPlainText())
    if text_len <= 0:
        return

    # 1) 夾到 doc 範圍內；明顯越界（end > text_len * 1.05）的 span 直接丟棄
    clipped: list[tuple[int, int, FormatSpan]] = []
    for s in spans:
        if s.end <= s.start:
            continue
        if s.end > text_len * 1.05:
            # 壞資料：丟棄整條，而非夾到 text_len（避免套到整篇）
            continue
        start = max(0, min(text_len, s.start))
        end = max(0, min(text_len, s.end))
        if end > start:
            clipped.append((start, end, s))

    if not clipped:
        return

    # 2) 覆蓋率檢查（基於夾過的 span）
    merged: list[tuple[int, int]] = []
    for start, end, _ in sorted(clipped):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    total_covered = sum(e - s for s, e in merged)
    if total_covered > text_len * 0.8:
        import logging
        logging.getLogger(__name__).warning(
            "偵測到 FormatSpans 總覆蓋 %.0f%% 全文（疑似壞資料），已跳過全部格式還原",
            100 * total_covered / max(1, text_len),
        )
        return

    # 3) 套用（**用夾過的 start/end**，不是原 span.start/end）
    for start, end, span in clipped:
        cursor = QTextCursor(doc)
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        fmt = QTextCharFormat()
        if span.bold:
            fmt.setFontWeight(QFont.Weight.Bold)
        if span.italic:
            fmt.setFontItalic(True)
        if span.underline:
            fmt.setFontUnderline(True)
        if span.highlight:
            fmt.setBackground(highlight_brush_color())
        cursor.mergeCharFormat(fmt)


def clear_format_in_range(cursor: QTextCursor) -> None:
    """把目前 cursor 選取範圍內的 bold/italic/underline/highlight 全部清除。"""
    fmt = QTextCharFormat()
    fmt.setFontWeight(QFont.Weight.Normal)
    fmt.setFontItalic(False)
    fmt.setFontUnderline(False)
    # 背景清成 NoBrush
    fmt.setBackground(Qt.BrushStyle.NoBrush)
    cursor.mergeCharFormat(fmt)
