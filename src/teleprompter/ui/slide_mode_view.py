"""SlideModeView：投影片模式獨立顯示元件。

每頁固定版面（PPT 風格）：
- 左欄：該頁講稿文字（QTextDocument 渲染到固定 rect）
- 右欄：該頁投影片圖
- 左右方向鍵：切換上/下一頁

設計原則：
- 不繼承 QTextEdit / QScrollArea → 避免 document flow / scroll / margin 副作用
- paintEvent 從當前頁資料直接渲染到 viewport rect → 第 1 頁與第 100 頁版面完全相同
- 沒有 scroll → 沒有「跨頁版面飄移」的空間
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QRect, Signal
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QPen,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import QWidget

# 用 transcript_loader 的標準 regex，保證頁面切分與 transcript.pages 一致
from ..core.transcript_loader import Transcript, _PAGE_SEPARATOR_RE
from ..core.rich_text_format import FormatSpan, restore_formats


class SlideModeView(QWidget):
    """投影片模式單頁顯示元件。"""

    page_navigate_requested = Signal(int)   # Left = -1、Right = +1

    # 版面常數（跨頁一致）
    PAD = 40             # viewport 外框 padding
    COL_GAP = 32         # 左文／右圖欄位間距
    DEFAULT_TEXT_RATIO = 0.5  # 左欄佔 viewport 寬度預設比例（可拖拉調整）
    MIN_TEXT_RATIO = 0.25
    MAX_TEXT_RATIO = 0.75
    PAGE_LABEL_H = 24    # 底部頁碼高度
    SPLITTER_HIT_W = 10  # splitter 滑鼠 hit 範圍半寬

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoFillBackground(True)
        self.setMouseTracking(True)   # 啟用 hover 偵測（splitter 變色）

        # 資料
        self._transcript: Optional[Transcript] = None
        self._slide_deck = None
        self._current_page_idx: int = 0
        self._format_spans: list[FormatSpan] = []
        # 每頁 char range 快取：[(start_char, end_char_exclusive), ...]
        self._page_char_ranges: list[tuple[int, int]] = []

        # 外觀
        self._bg_color = QColor("#1E1E1E")
        self._text_color = QColor("#F0F0F0")
        self._font_family = "Microsoft JhengHei"
        self._font_size = 36
        self._line_spacing = 1.6

        # 文字／投影片 splitter 狀態
        # - 橫屏：_text_ratio = 左欄文字寬度比例
        # - 直屏：_text_ratio = 下方文字高度比例
        self._text_ratio = self.DEFAULT_TEXT_RATIO
        self._splitter_x = 0   # 橫屏時的 splitter x
        self._splitter_y = 0   # 直屏時的 splitter y
        self._split_hover = False
        self._split_dragging = False
        # 版面對調：橫屏時文字在「左→右」、直屏時文字在「下→上」
        self._layout_swapped = False

        self._apply_palette()

    # ---------- 公開 API ----------

    def set_transcript(self, transcript: Optional[Transcript]) -> None:
        self._transcript = transcript
        self._compute_page_char_ranges()
        # 若 current_page 超出，夾到範圍
        if transcript is not None and transcript.pages:
            self._current_page_idx = max(
                0, min(len(transcript.pages) - 1, self._current_page_idx)
            )
        self.update()

    def set_slide_deck(self, deck) -> None:
        self._slide_deck = deck
        self.update()

    def set_current_page(self, idx: int) -> None:
        if self._transcript is None or not self._transcript.pages:
            return
        idx = max(0, min(len(self._transcript.pages) - 1, int(idx)))
        if idx != self._current_page_idx:
            self._current_page_idx = idx
            self.update()

    def current_page(self) -> int:
        return self._current_page_idx

    def set_format_spans(self, spans: list[FormatSpan]) -> None:
        """從 PrompterView dump 出的 FormatSpan（針對整份文字），本元件 paint 時
        只會取落在當前頁範圍的 spans，轉成 page-local offset 套用。"""
        self._format_spans = list(spans)
        self.update()

    def set_font_family(self, family: str) -> None:
        self._font_family = family
        self.update()

    def set_font_size(self, size: int) -> None:
        self._font_size = max(12, min(120, int(size)))
        self.update()

    def set_line_spacing(self, factor: float) -> None:
        self._line_spacing = max(1.0, float(factor))
        self.update()

    def set_layout_swapped(self, swapped: bool) -> None:
        """對調文字/投影片位置。橫屏左右互換、直屏上下互換。"""
        if self._layout_swapped != swapped:
            self._layout_swapped = bool(swapped)
            self.update()

    def set_colors(
        self,
        *,
        background: Optional[str] = None,
        upcoming: Optional[str] = None,
        **_ignored,
    ) -> None:
        if background:
            self._bg_color = QColor(background)
        if upcoming:
            self._text_color = QColor(upcoming)
        self._apply_palette()
        self.update()

    # ---------- 內部：資料預處理 ----------

    def _apply_palette(self) -> None:
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, self._bg_color)
        self.setPalette(pal)

    def _compute_page_char_ranges(self) -> None:
        """從 transcript.full_text 按 page separator 切出每頁 char range。
        用 transcript_loader 同一個 regex 以保證頁數、邊界與 transcript.pages 一致。
        """
        self._page_char_ranges = []
        if self._transcript is None or not self._transcript.pages:
            return
        text = self._transcript.full_text
        matches = list(_PAGE_SEPARATOR_RE.finditer(text))
        prev_end = 0
        for i in range(len(self._transcript.pages)):
            if i < len(matches):
                start = prev_end
                end = matches[i].start()
                prev_end = matches[i].end()
            else:
                start = prev_end
                end = len(text)
            self._page_char_ranges.append((start, end))

    # ---------- 事件 ----------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.modifiers() == Qt.KeyboardModifier.NoModifier:
            if event.key() == Qt.Key.Key_Left:
                self.page_navigate_requested.emit(-1)
                event.accept()
                return
            if event.key() == Qt.Key.Key_Right:
                self.page_navigate_requested.emit(+1)
                event.accept()
                return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # 搶 focus，讓方向鍵能被收到
        self.setFocus()
        pos = event.position() if hasattr(event, "position") else event.pos()
        x, y = int(pos.x()), int(pos.y())
        if event.button() == Qt.MouseButton.LeftButton and self._is_over_splitter(x, y):
            self._split_dragging = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position() if hasattr(event, "position") else event.pos()
        x, y = int(pos.x()), int(pos.y())
        if self._split_dragging:
            if self._is_portrait():
                # 直屏：y 決定下方文字高度
                vh = self.height()
                content_top = self.PAD
                content_bottom = vh - self.PAD - self.PAGE_LABEL_H
                content_h = max(1, content_bottom - content_top)
                # split_y = content_top + content_h * (1 - text_ratio)
                # → text_ratio = 1 - (split_y - content_top) / content_h
                new_ratio = 1 - (y - content_top) / content_h
            else:
                vw = self.width()
                content_w = max(1, vw - 2 * self.PAD)
                new_ratio = (x - self.PAD) / content_w
            new_ratio = max(self.MIN_TEXT_RATIO, min(self.MAX_TEXT_RATIO, new_ratio))
            if abs(new_ratio - self._text_ratio) > 0.005:
                self._text_ratio = new_ratio
                self.update()
            event.accept()
            return
        # Hover 偵測
        new_hover = self._is_over_splitter(x, y)
        if new_hover != self._split_hover:
            self._split_hover = new_hover
            if new_hover:
                cursor = (
                    Qt.CursorShape.SplitVCursor
                    if self._is_portrait()
                    else Qt.CursorShape.SplitHCursor
                )
                self.setCursor(cursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._split_dragging:
            self._split_dragging = False
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        if self._split_hover:
            self._split_hover = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
        super().leaveEvent(event)

    def _is_portrait(self) -> bool:
        """直屏判定：viewport 寬 < 高，切成上下版面。"""
        return self.width() < self.height()

    def _compute_column_rects(self) -> tuple[QRect, QRect]:
        """計算文字 + 投影片 rect。
        橫屏 → 左文右圖（swap → 左圖右文）
        直屏 → 上圖下文（swap → 上文下圖）
        """
        vw, vh = self.width(), self.height()
        if self._is_portrait():
            # 直屏：上圖下文（或 swap 後：上文下圖）
            content_top = self.PAD
            content_bottom = vh - self.PAD - self.PAGE_LABEL_H
            content_h = max(0, content_bottom - content_top)
            content_w = max(0, vw - 2 * self.PAD)
            split_y = content_top + int(content_h * (1 - self._text_ratio))
            self._splitter_y = split_y
            self._splitter_x = 0
            upper_h = max(0, split_y - content_top - self.COL_GAP // 2)
            lower_h = max(0, content_bottom - (split_y + self.COL_GAP // 2))
            upper_rect = QRect(self.PAD, content_top, content_w, upper_h)
            lower_rect = QRect(
                self.PAD, split_y + self.COL_GAP // 2, content_w, lower_h
            )
            if self._layout_swapped:
                # 上文下圖
                return upper_rect, lower_rect   # text, slide
            # 上圖下文（預設）
            return lower_rect, upper_rect
        # 橫屏：左文右圖（或 swap 後：左圖右文）
        content_top = self.PAD
        content_h = max(0, vh - 2 * self.PAD - self.PAGE_LABEL_H)
        content_w = max(0, vw - 2 * self.PAD)
        split_x = self.PAD + int(content_w * self._text_ratio)
        self._splitter_x = split_x
        self._splitter_y = 0
        left_w = max(0, split_x - self.PAD - self.COL_GAP // 2)
        right_w = max(0, (self.PAD + content_w) - (split_x + self.COL_GAP // 2))
        left_rect = QRect(self.PAD, content_top, left_w, content_h)
        right_rect = QRect(
            split_x + self.COL_GAP // 2, content_top, right_w, content_h
        )
        if self._layout_swapped:
            # 左圖右文
            return right_rect, left_rect   # text, slide
        # 左文右圖（預設）
        return left_rect, right_rect

    def _is_over_splitter(self, x: int, y: int = -1) -> bool:
        """滑鼠是否在 splitter hit 範圍內（依 orientation 判斷）。"""
        if self._is_portrait():
            # 直屏：垂直座標 y 要接近 splitter_y
            if y < 0:
                return False
            return abs(y - self._splitter_y) <= self.SPLITTER_HIT_W
        return abs(x - self._splitter_x) <= self.SPLITTER_HIT_W

    def _paint_splitter(self, painter: QPainter, top: int, bottom: int) -> None:
        """畫 splitter 分隔條：橫屏垂直線、直屏水平線。"""
        if self._split_dragging:
            color = QColor("#4CAF50"); width = 4
        elif self._split_hover:
            color = QColor("#80D8FF"); width = 3
        else:
            color = QColor(128, 200, 255, 90); width = 2
        if self._is_portrait():
            # 水平分隔條
            vw = self.width()
            painter.fillRect(
                self.PAD, self._splitter_y - width // 2,
                vw - 2 * self.PAD, width, color,
            )
        else:
            painter.fillRect(
                self._splitter_x - width // 2, top, width, bottom - top, color
            )

    # ---------- 繪圖 ----------

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), self._bg_color)

            if (
                self._transcript is None
                or not self._transcript.pages
                or self._current_page_idx < 0
                or self._current_page_idx >= len(self._transcript.pages)
            ):
                self._paint_placeholder(painter)
                return

            vw, vh = self.width(), self.height()
            content_top = self.PAD
            content_bottom = vh - self.PAD - self.PAGE_LABEL_H
            content_h = max(0, content_bottom - content_top)

            text_rect, slide_rect = self._compute_column_rects()

            idx = self._current_page_idx
            self._paint_text(painter, text_rect, idx)
            self._paint_slide(painter, slide_rect, idx)
            self._paint_splitter(painter, content_top, content_bottom)
            self._paint_page_indicator(painter, vw, vh, idx)
        finally:
            painter.end()

    def _paint_placeholder(self, painter: QPainter) -> None:
        painter.setPen(QColor("#707070"))
        font = QFont(self._font_family, 14)
        painter.setFont(font)
        painter.drawText(
            self.rect(), Qt.AlignmentFlag.AlignCenter, "尚未載入講稿 / 投影片"
        )

    def _paint_text(self, painter: QPainter, rect: QRect, page_idx: int) -> None:
        """用 QTextDocument 把當前頁文字渲染到 rect，置中。"""
        if rect.width() <= 0 or rect.height() <= 0:
            return
        if page_idx >= len(self._page_char_ranges):
            return

        start_char, end_char = self._page_char_ranges[page_idx]
        if end_char <= start_char:
            return
        page_text = self._transcript.full_text[start_char:end_char]

        doc = QTextDocument()
        doc.setDefaultFont(QFont(self._font_family, self._font_size))
        doc.setDocumentMargin(0)
        doc.setTextWidth(rect.width())
        doc.setPlainText(page_text)

        # 套 MD 樣式（headings / comments / ---）
        self._apply_md_to_doc(doc)

        # 套使用者格式（只取重疊部分）
        page_spans = self._format_spans_for_page(start_char, end_char)
        if page_spans:
            restore_formats(doc, page_spans)

        # 垂直置中（若內容高度 < rect.height()）
        doc_h = doc.size().height()
        y_offset = 0
        if doc_h < rect.height():
            y_offset = int((rect.height() - doc_h) / 2)

        painter.save()
        painter.setClipRect(rect)
        painter.translate(rect.left(), rect.top() + y_offset)
        ctx = QAbstractTextDocumentLayout.PaintContext()
        ctx.palette.setColor(QPalette.ColorRole.Text, self._text_color)
        doc.documentLayout().draw(painter, ctx)
        painter.restore()

    def _format_spans_for_page(
        self, start_char: int, end_char: int
    ) -> list[FormatSpan]:
        """把跨整篇文字的 FormatSpans 截到當前頁 [start, end)，並轉成 page-local offset。"""
        out: list[FormatSpan] = []
        for s in self._format_spans:
            if s.end <= start_char or s.start >= end_char:
                continue
            ps = max(0, s.start - start_char)
            pe = min(end_char - start_char, s.end - start_char)
            if pe > ps:
                out.append(
                    FormatSpan(
                        start=ps,
                        end=pe,
                        bold=s.bold,
                        italic=s.italic,
                        underline=s.underline,
                        highlight=s.highlight,
                    )
                )
        return out

    def _apply_md_to_doc(self, doc: QTextDocument) -> None:
        """對 doc 套 Markdown 樣式：# 標題放大粗體藍字 / <!-- --> 註解斜體灰 / --- 隱藏。"""
        block = doc.firstBlock()
        while block.isValid():
            stripped = block.text().strip()
            char_fmt: Optional[QTextCharFormat] = None
            block_fmt = QTextBlockFormat()
            block_fmt.setLineHeight(self._line_spacing * 100, 1)
            block_fmt.setTopMargin(0)
            block_fmt.setBottomMargin(0)

            level = 0
            if stripped.startswith("### "):
                level = 3
            elif stripped.startswith("## "):
                level = 2
            elif stripped.startswith("# "):
                level = 1
            if level > 0:
                char_fmt = QTextCharFormat()
                scale = {1: 1.20, 2: 1.10, 3: 1.05}[level]
                char_fmt.setFontPointSize(self._font_size * scale)
                char_fmt.setFontWeight(QFont.Weight.Bold)
                char_fmt.setForeground(QColor("#80D8FF"))
            elif stripped in ("---", "===", "***"):
                char_fmt = QTextCharFormat()
                char_fmt.setForeground(self._bg_color)
                char_fmt.setFontPointSize(max(4, self._font_size * 0.3))
            elif stripped.startswith("<!--") and stripped.endswith("-->"):
                char_fmt = QTextCharFormat()
                char_fmt.setForeground(QColor("#707070"))
                char_fmt.setFontItalic(True)

            cursor = QTextCursor(block)
            cursor.setPosition(block.position())
            cursor.setPosition(
                block.position() + block.length() - 1,
                QTextCursor.MoveMode.KeepAnchor,
            )
            if char_fmt is not None:
                cursor.mergeCharFormat(char_fmt)
            cursor.setBlockFormat(block_fmt)
            block = block.next()

    def _paint_slide(self, painter: QPainter, rect: QRect, page_idx: int) -> None:
        if self._slide_deck is None or rect.width() <= 0 or rect.height() <= 0:
            return
        page = self._transcript.pages[page_idx]
        page_no = page.number
        if page_no < 1 or page_no > self._slide_deck.page_count:
            # 對應 slide 不存在 → 顯示 placeholder
            painter.setPen(QColor("#3A3A3A"))
            painter.drawRect(rect)
            painter.setPen(QColor("#707070"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "（無對應投影片）")
            return
        slide_page = self._slide_deck.pages[page_no - 1]
        aspect = (
            slide_page.height_pt / slide_page.width_pt
            if slide_page.width_pt > 0
            else 1.414
        )
        # Fit to rect：先假設寬度填滿，若高度超過則以高度為準
        target_w = rect.width()
        target_h = int(target_w * aspect)
        if target_h > rect.height():
            target_h = rect.height()
            target_w = int(target_h / aspect) if aspect > 0 else rect.width()
        # 水平 + 垂直置中
        x = rect.left() + (rect.width() - target_w) // 2
        y = rect.top() + (rect.height() - target_h) // 2

        pix = self._slide_deck.render(page_no, target_w)
        if pix is not None and not pix.isNull():
            painter.drawPixmap(x, y, pix)
            painter.setPen(QColor("#3A3A3A"))
            painter.drawRect(x, y, pix.width(), pix.height())
        else:
            painter.fillRect(x, y, target_w, target_h, QColor("#1A1A1A"))
            painter.setPen(QColor("#3A3A3A"))
            painter.drawRect(x, y, target_w, target_h)

    def _paint_page_indicator(
        self, painter: QPainter, vw: int, vh: int, idx: int
    ) -> None:
        if self._transcript is None:
            return
        total = len(self._transcript.pages)
        painter.setPen(QColor("#888888"))
        font = QFont(self._font_family, 11)
        painter.setFont(font)
        text = f"{idx + 1} / {total}"
        painter.drawText(
            QRect(0, vh - self.PAGE_LABEL_H - 4, vw, self.PAGE_LABEL_H),
            Qt.AlignmentFlag.AlignCenter,
            text,
        )
