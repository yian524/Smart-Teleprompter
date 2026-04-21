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

from PySide6.QtCore import Qt, QPoint, QPointF, QRect, QRectF, Signal
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QBrush,
    QColor,
    QFont,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPalette,
    QPen,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import QInputDialog, QWidget

from ..core.annotations import Annotation
from ..core.rich_text_format import FormatSpan, restore_formats
# 用 transcript_loader 的標準 regex，保證頁面切分與 transcript.pages 一致
from ..core.transcript_loader import Transcript, _PAGE_SEPARATOR_RE


def _paint_sticky_body(
    painter: QPainter,
    rect: QRect,
    color: str,
    text: str,
    font_family: str,
    resize_handle_size: int,
) -> None:
    """漂亮版便利貼：多層陰影 + 折角 + 頂部色帶 + grip 拖曳指示 + resize dot。"""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # 1) 多層陰影（模擬軟陰影），3 層疊加越遠越淡
    for offset, alpha in [(1, 35), (3, 25), (6, 15)]:
        shadow_rect = rect.adjusted(offset, offset, offset, offset)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, alpha))
        painter.drawRoundedRect(shadow_rect, 8, 8)

    # 2) 主體色（不透明 → 看起來像紙）
    base = QColor(color)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(base)
    painter.drawRoundedRect(rect, 8, 8)

    # 3) 頂部色帶（深一點，像黏貼條）
    band_rect = QRect(rect.left(), rect.top(), rect.width(), 20)
    band_path = QPainterPath()
    band_path.addRoundedRect(QRectF(band_rect), 8.0, 8.0)
    # 裁掉下半邊的圓角，讓色帶只有上面圓角
    clip_path = QPainterPath()
    clip_path.addRect(QRectF(band_rect))
    band_path = band_path.intersected(clip_path)
    darker_band = base.darker(115)   # 稍深
    painter.setBrush(darker_band)
    painter.drawPath(band_path)
    # 底下一條細線分隔
    sep_pen = QPen(base.darker(135))
    sep_pen.setWidth(1)
    painter.setPen(sep_pen)
    painter.drawLine(
        rect.left() + 6, rect.top() + 20,
        rect.right() - 6, rect.top() + 20,
    )

    # 4) 頂部色帶上的 grip dots（告訴使用者「可拖拉」）
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(255, 255, 255, 180))
    cx = (rect.left() + rect.right()) // 2
    cy = rect.top() + 10
    for dx in (-8, 0, 8):
        painter.drawEllipse(QPoint(cx + dx, cy), 2, 2)

    # 5) 右上角折角（小三角，視覺上像紙的折痕）
    fold_size = 12
    fold_path = QPainterPath()
    fold_path.moveTo(rect.right() - fold_size, rect.top())
    fold_path.lineTo(rect.right(), rect.top())
    fold_path.lineTo(rect.right(), rect.top() + fold_size)
    fold_path.closeSubpath()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(base.darker(130))
    painter.drawPath(fold_path)

    # 6) 整體細邊框
    border_pen = QPen(base.darker(150))
    border_pen.setWidth(1)
    painter.setPen(border_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect, 8, 8)

    # 7) 文字內容（色帶下方，留出 resize handle 空間）
    if text:
        painter.setPen(QColor("#1A1A1A"))
        f = QFont(font_family, 11)
        painter.setFont(f)
        text_rect = QRect(
            rect.left() + 10,
            rect.top() + 26,  # 色帶下方
            rect.width() - 20,
            rect.height() - 26 - resize_handle_size - 4,
        )
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignTop
            | Qt.TextFlag.TextWordWrap,
            text,
        )

    # 8) 右下角 resize handle：兩個小方點 + 一個斜角三角 → 明確的「可縮放」指示
    s = resize_handle_size
    x2 = rect.right()
    y2 = rect.bottom()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 110))
    # 3 顆小點沿斜角排列
    for (dx, dy) in [(4, 4), (8, 4), (4, 8)]:
        painter.drawEllipse(QPoint(x2 - dx, y2 - dy), 1, 1)
    # 底部 & 右側邊緣各一條短粗線，明確劃出 handle 範圍
    corner_pen = QPen(QColor(0, 0, 0, 150))
    corner_pen.setWidth(2)
    painter.setPen(corner_pen)
    painter.drawLine(x2 - 10, y2 - 3, x2 - 3, y2 - 3)
    painter.drawLine(x2 - 3, y2 - 10, x2 - 3, y2 - 3)

    painter.restore()


class SlideModeView(QWidget):
    """投影片模式單頁顯示元件。"""

    page_navigate_requested = Signal(int)   # Left = -1、Right = +1
    annotations_changed = Signal()           # 標註有變動（新增/刪除/編輯）→ 通知 session 存檔
    text_copied = Signal(str)                # 選字複製 → 通知狀態列
    tool_requested = Signal(str)              # 要求 MainWindow 切成其他 tool（例如貼完便利貼回指標）

    # 工具列模式
    TOOL_POINTER = "pointer"
    TOOL_SELECT = "select"      # 選取投影片上的文字
    TOOL_PENCIL = "pencil"       # 鉛筆畫
    TOOL_NOTE = "note"            # 便利貼
    TOOL_ERASER = "eraser"        # 橡皮擦

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

        # ==== 工具狀態 ====
        self._tool = self.TOOL_POINTER
        self._tool_color = QColor("#FFEB3B")     # 筆劃 / 便利貼預設色
        self._tool_stroke_width = 3

        # ==== PDF 文字選取（word-level，像 Word 那樣）====
        # anchor_idx / focus_idx = 在 pdf_renderer.get_text_blocks(page_no) 陣列的索引
        self._text_select_anchor_idx: Optional[int] = None
        self._text_select_focus_idx: Optional[int] = None
        self._selected_text: str = ""

        # ==== 標註 ====
        # 當前頁的標註清單（由外部透過 set_annotations 填）
        self._annotations: list[Annotation] = []
        # 正在繪製的鉛筆筆劃（list of viewport points；完成時 normalize 存入 annotation）
        self._drawing_stroke: list[QPointF] = []
        # 上次繪圖 slide rect 快取（用於把 viewport 座標轉成 slide 內 0..1 比例）
        self._last_slide_rect: Optional[QRect] = None
        # 正在拖拉的便利貼（指標工具下；None = 沒在拖）
        self._dragging_note: Optional[Annotation] = None
        self._drag_offset: QPoint = QPoint(0, 0)
        # 正在縮放的便利貼（右下角 handle）
        self._resizing_note: Optional[Annotation] = None
        # 「已複製」toast 顯示截止時間（ms since epoch）
        self._copy_toast_until_ms: int = 0

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
            # 切頁 → 清掉跨頁的 PDF 選取
            self._text_select_anchor_idx = None
            self._text_select_focus_idx = None
            self._selected_text = ""
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

    # ---------- 工具列 API ----------

    def set_tool(self, tool: str) -> None:
        if tool not in (
            self.TOOL_POINTER, self.TOOL_SELECT, self.TOOL_PENCIL,
            self.TOOL_NOTE, self.TOOL_ERASER,
        ):
            return
        self._tool = tool
        # 切換工具 → 清掉 PDF 選取
        self._text_select_anchor_idx = None
        self._text_select_focus_idx = None
        self._selected_text = ""
        cursors = {
            self.TOOL_POINTER: Qt.CursorShape.ArrowCursor,
            self.TOOL_SELECT: Qt.CursorShape.IBeamCursor,
            self.TOOL_PENCIL: Qt.CursorShape.CrossCursor,
            self.TOOL_NOTE: Qt.CursorShape.PointingHandCursor,
            self.TOOL_ERASER: Qt.CursorShape.ForbiddenCursor,
        }
        self.setCursor(cursors.get(tool, Qt.CursorShape.ArrowCursor))
        self.update()

    def current_tool(self) -> str:
        return self._tool

    def set_tool_color(self, color: str) -> None:
        self._tool_color = QColor(color)

    def set_tool_stroke_width(self, w: int) -> None:
        self._tool_stroke_width = max(1, min(20, int(w)))

    def copy_selected_text(self) -> bool:
        """把目前選取的 PDF 文字複製到剪貼簿 + toast 提示；回傳是否成功。"""
        # 若 anchor/focus 還在 → 先擷取文字
        if (
            self._text_select_anchor_idx is not None
            and self._text_select_focus_idx is not None
        ):
            self._finalize_text_selection()
        if self._selected_text.strip():
            QGuiApplication.clipboard().setText(self._selected_text)
            self.text_copied.emit(self._selected_text)
            self._copy_toast_until_ms = self._now_ms() + 1500
            self.update()
            return True
        return False

    # ---------- 標註 API ----------

    def set_annotations(self, annotations: list[Annotation]) -> None:
        """從 session 載入標註清單。"""
        self._annotations = list(annotations)
        self.update()

    def annotations(self) -> list[Annotation]:
        """給 session 持久化用。"""
        return list(self._annotations)

    def current_page_annotations(self) -> list[Annotation]:
        page = self._current_page_idx + 1  # 1-based
        if self._transcript and self._current_page_idx < len(self._transcript.pages):
            page = self._transcript.pages[self._current_page_idx].number
        return [a for a in self._annotations if a.slide_page == page]

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
        # Ctrl+C 複製 PDF 選取的文字
        if (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and event.key() == Qt.Key.Key_C
        ):
            if self.copy_selected_text():
                event.accept()
                return
        if event.modifiers() == Qt.KeyboardModifier.NoModifier:
            if event.key() == Qt.Key.Key_Left:
                self.page_navigate_requested.emit(-1)
                event.accept()
                return
            if event.key() == Qt.Key.Key_Right:
                self.page_navigate_requested.emit(+1)
                event.accept()
                return
            # 工具快捷鍵
            key_to_tool = {
                Qt.Key.Key_V: self.TOOL_POINTER,     # V = pointer
                Qt.Key.Key_S: self.TOOL_SELECT,       # S = select text
                Qt.Key.Key_P: self.TOOL_PENCIL,       # P = pencil
                Qt.Key.Key_N: self.TOOL_NOTE,          # N = note
                Qt.Key.Key_E: self.TOOL_ERASER,        # E = eraser
            }
            if event.key() in key_to_tool:
                self.set_tool(key_to_tool[event.key()])
                event.accept()
                return
        super().keyPressEvent(event)

    def _find_note_at(self, point: QPoint) -> Optional[Annotation]:
        """找到點擊位置下的便利貼（當前頁）。"""
        page_no = self._page_no_for_current()
        if page_no is None:
            return None
        vw, vh = max(1, self.width()), max(1, self.height())
        for ann in self._annotations:
            if ann.slide_page != page_no or ann.kind != "note":
                continue
            nx = ann.x * vw
            ny = ann.y * vh
            nw = max(80, ann.width * vw)
            nh = max(40, ann.height * vh)
            if nx <= point.x() <= nx + nw and ny <= point.y() <= ny + nh:
                return ann
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.setFocus()
        pos = event.position() if hasattr(event, "position") else event.pos()
        x, y = int(pos.x()), int(pos.y())
        point = QPoint(x, y)

        # 1) splitter 優先
        if event.button() == Qt.MouseButton.LeftButton and self._is_over_splitter(x, y):
            self._split_dragging = True
            event.accept()
            return

        # 2a) 指標工具：note handle → note body → slide PDF 選字 → QTextEdit 預設
        if event.button() == Qt.MouseButton.LeftButton and self._tool == self.TOOL_POINTER:
            rz = self._find_note_resize_handle_at(point)
            if rz is not None:
                self._resizing_note = rz
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
                event.accept()
                return
            note = self._find_note_at(point)
            if note is not None:
                self._dragging_note = note
                vw, vh = max(1, self.width()), max(1, self.height())
                self._drag_offset = QPoint(
                    int(note.x * vw) - point.x(),
                    int(note.y * vh) - point.y(),
                )
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                event.accept()
                return
            # 點在 slide 區域：嘗試開始 PDF word 選字（Word 風格）
            slide_rect = self._last_slide_rect
            if slide_rect is not None and slide_rect.contains(point):
                page_no = self._page_no_for_current()
                if page_no is not None:
                    idx = self._word_index_at_viewport(point, slide_rect, page_no)
                    if idx is not None:
                        self._text_select_anchor_idx = idx
                        self._text_select_focus_idx = idx
                        self._selected_text = ""
                        self.setCursor(Qt.CursorShape.IBeamCursor)
                        self.update()
                        event.accept()
                        return
                # 點在空白處 → 清除現有選取
                if self._text_select_anchor_idx is not None:
                    self._text_select_anchor_idx = None
                    self._text_select_focus_idx = None
                    self.update()

        # 2b) 工具模式
        slide_rect = self._last_slide_rect
        if event.button() == Qt.MouseButton.LeftButton:
            # 鉛筆 / 便利貼 / 橡皮擦：整個 viewport 都可用（避開 splitter）
            if self._tool in (self.TOOL_PENCIL, self.TOOL_NOTE, self.TOOL_ERASER):
                if self._tool == self.TOOL_PENCIL:
                    self._drawing_stroke = [QPointF(point)]
                    self.update()
                    event.accept()
                    return
                if self._tool == self.TOOL_NOTE:
                    self._add_sticky_note_at(point)
                    event.accept()
                    return
                if self._tool == self.TOOL_ERASER:
                    if self._erase_at(point):
                        self.annotations_changed.emit()
                        self.update()
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position() if hasattr(event, "position") else event.pos()
        x, y = int(pos.x()), int(pos.y())
        point = QPoint(x, y)

        # 1) splitter 拖拉
        if self._split_dragging:
            if self._is_portrait():
                vh = self.height()
                content_top = self.PAD
                content_bottom = vh - self.PAD - self.PAGE_LABEL_H
                content_h = max(1, content_bottom - content_top)
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

        # 2) 指標模式 + 文字選取中（左鍵按著拖曳）
        if (
            self._tool == self.TOOL_POINTER
            and self._text_select_anchor_idx is not None
            and (event.buttons() & Qt.MouseButton.LeftButton)
            and self._last_slide_rect is not None
        ):
            page_no = self._page_no_for_current()
            if page_no is not None:
                idx = self._word_index_at_viewport(
                    point, self._last_slide_rect, page_no
                )
                if idx is not None and idx != self._text_select_focus_idx:
                    self._text_select_focus_idx = idx
                    self.update()
            event.accept()
            return
        if (
            self._tool == self.TOOL_PENCIL
            and self._drawing_stroke
            and (event.buttons() & Qt.MouseButton.LeftButton)
        ):
            self._drawing_stroke.append(QPointF(point))
            self.update()
            event.accept()
            return
        # 指標模式 + 縮放便利貼
        if (
            self._tool == self.TOOL_POINTER
            and self._resizing_note is not None
            and (event.buttons() & Qt.MouseButton.LeftButton)
        ):
            vw, vh = max(1, self.width()), max(1, self.height())
            ann = self._resizing_note
            new_w = (point.x() - ann.x * vw) / vw
            new_h = (point.y() - ann.y * vh) / vh
            ann.width = max(0.08, min(0.95, new_w))
            ann.height = max(0.05, min(0.9, new_h))
            self.update()
            event.accept()
            return
        # 指標模式 + 拖拉便利貼
        if (
            self._tool == self.TOOL_POINTER
            and self._dragging_note is not None
            and (event.buttons() & Qt.MouseButton.LeftButton)
        ):
            vw, vh = max(1, self.width()), max(1, self.height())
            new_left = point.x() + self._drag_offset.x()
            new_top = point.y() + self._drag_offset.y()
            self._dragging_note.x = max(0.0, min(0.95, new_left / vw))
            self._dragging_note.y = max(0.0, min(0.95, new_top / vh))
            self.update()
            event.accept()
            return
        # 橡皮擦模式 + 拖曳擦除
        if (
            self._tool == self.TOOL_ERASER
            and (event.buttons() & Qt.MouseButton.LeftButton)
        ):
            if self._erase_at(point):
                self.annotations_changed.emit()
                self.update()
            event.accept()
            return

        # 3) splitter hover
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
            elif self._tool == self.TOOL_POINTER:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._split_dragging:
            self._split_dragging = False
            self.update()
            event.accept()
            return

        # 結束便利貼縮放 → 存檔
        if self._resizing_note is not None:
            self._resizing_note = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.annotations_changed.emit()
            self.update()
            event.accept()
            return
        # 結束便利貼拖拉 → 存檔
        if self._dragging_note is not None:
            self._dragging_note = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.annotations_changed.emit()
            self.update()
            event.accept()
            return

        # 結束 PDF 文字選取 → 只是停止拖曳；不自動複製（使用者 Ctrl+C 手動複製）
        if (
            self._tool == self.TOOL_POINTER
            and self._text_select_anchor_idx is not None
        ):
            # 保留選取高亮 → 使用者看得到可以複製什麼
            self._finalize_text_selection()   # 預先算好 _selected_text
            event.accept()
            return

        # 結束鉛筆筆劃 → 存成 annotation
        if self._tool == self.TOOL_PENCIL and self._drawing_stroke:
            self._finalize_pencil_stroke()
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """雙擊便利貼 → 編輯文字。"""
        if self._tool == self.TOOL_POINTER:
            pos = event.position() if hasattr(event, "position") else event.pos()
            point = QPoint(int(pos.x()), int(pos.y()))
            note = self._find_note_at(point)
            if note is not None:
                new_text, ok = QInputDialog.getMultiLineText(
                    self, "編輯便利貼", "修改內容：", note.text,
                )
                if ok:
                    note.text = new_text
                    self.annotations_changed.emit()
                    self.update()
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    # ---------- 工具：selection / annotations 內部動作 ----------

    def _finalize_text_selection(self) -> None:
        """從 anchor_idx..focus_idx 擷取 PDF 當前頁文字（reading order）。"""
        if (
            self._slide_deck is None
            or self._text_select_anchor_idx is None
            or self._text_select_focus_idx is None
            or self._transcript is None
        ):
            return
        page_no = self._page_no_for_current()
        if page_no is None:
            return
        try:
            blocks = self._slide_deck.get_text_blocks(page_no)
        except Exception:
            blocks = []
        if not blocks:
            return
        i0 = max(0, min(self._text_select_anchor_idx, self._text_select_focus_idx))
        i1 = min(
            len(blocks) - 1,
            max(self._text_select_anchor_idx, self._text_select_focus_idx),
        )
        words = [blocks[i].text for i in range(i0, i1 + 1)]
        # 多數 PDF words 不會自帶空白 → 用 " " 連接
        self._selected_text = " ".join(words)
        self.update()

    def _viewport_rect(self) -> QRect:
        """整個 viewport rect（作為標註座標系的基準）。"""
        return QRect(0, 0, self.width(), self.height())

    def _finalize_pencil_stroke(self) -> None:
        """把正在繪製的筆劃 normalize 成 viewport 0..1 比例存成 Annotation。"""
        if not self._drawing_stroke:
            self._drawing_stroke = []
            return
        page_no = self._page_no_for_current()
        if page_no is None:
            self._drawing_stroke = []
            return
        vw, vh = max(1, self.width()), max(1, self.height())
        segment: list[tuple[float, float]] = []
        for p in self._drawing_stroke:
            rx = p.x() / vw
            ry = p.y() / vh
            segment.append((max(0.0, min(1.0, rx)), max(0.0, min(1.0, ry))))
        if len(segment) < 2:
            self._drawing_stroke = []
            return
        # 合併到同頁/同色/同寬的既有 stroke annotation
        page_annots = self.current_page_annotations()
        merged = False
        for a in page_annots:
            if (
                a.kind == "stroke"
                and a.color == self._tool_color.name()
                and a.stroke_width == self._tool_stroke_width
            ):
                a.strokes.append(segment)
                merged = True
                break
        if not merged:
            ann = Annotation(
                kind="stroke",
                slide_page=page_no,
                color=self._tool_color.name(),
                stroke_width=self._tool_stroke_width,
                strokes=[segment],
            )
            self._annotations.append(ann)
        self._drawing_stroke = []
        self.annotations_changed.emit()
        self.update()

    def _add_sticky_note_at(self, point: QPoint) -> None:
        """在 viewport 點擊處新增便利貼，彈出對話框輸入文字。"""
        text, ok = QInputDialog.getMultiLineText(
            self, "新增便利貼", "在此輸入筆記內容（Ctrl+Enter 確定）：",
        )
        if not ok or not text.strip():
            return
        page_no = self._page_no_for_current()
        if page_no is None:
            return
        vw, vh = max(1, self.width()), max(1, self.height())
        rx = point.x() / vw
        ry = point.y() / vh
        ann = Annotation(
            kind="note",
            slide_page=page_no,
            x=max(0.0, min(0.85, rx)),
            y=max(0.0, min(0.85, ry)),
            width=0.2,
            height=0.1,
            text=text,
            color=self._tool_color.name(),
        )
        self._annotations.append(ann)
        self.annotations_changed.emit()
        self.update()
        # 貼完便利貼自動回指標工具（不要卡在 note 模式）
        self.tool_requested.emit(self.TOOL_POINTER)

    # 橡皮擦半徑（像素）
    ERASER_RADIUS = 18

    def _erase_at(self, point: QPoint) -> bool:
        """塗抹式橡皮擦：移除以 point 為中心、ERASER_RADIUS 半徑內的筆劃點與便利貼。

        回傳 True 如果有任何標註被修改。
        - 筆劃（stroke）：被擦過的點切斷 segment；剩下非空 segment 保留
        - 整個 annotation 沒有 segment 了 → 刪除 annotation
        - 便利貼：擦到就整個刪
        """
        page_no = self._page_no_for_current()
        if page_no is None:
            return False
        vw, vh = max(1, self.width()), max(1, self.height())
        r = self.ERASER_RADIUS
        changed = False
        remaining: list[Annotation] = []
        for ann in self._annotations:
            if ann.slide_page != page_no:
                remaining.append(ann)
                continue
            if ann.kind == "note":
                nx = ann.x * vw
                ny = ann.y * vh
                nw = max(80, ann.width * vw)
                nh = max(40, ann.height * vh)
                # 擦到便利貼 → 整個刪
                if nx - r <= point.x() <= nx + nw + r and ny - r <= point.y() <= ny + nh + r:
                    changed = True
                    continue
                remaining.append(ann)
            elif ann.kind == "stroke":
                new_segments: list[list[tuple[float, float]]] = []
                for segment in ann.strokes:
                    current_run: list[tuple[float, float]] = []
                    for (xr, yr) in segment:
                        px = xr * vw
                        py = yr * vh
                        if (px - point.x()) ** 2 + (py - point.y()) ** 2 <= r * r:
                            # 擦到此點：flush 目前 run，丟棄此點
                            if len(current_run) >= 2:
                                new_segments.append(current_run)
                            current_run = []
                            changed = True
                        else:
                            current_run.append((xr, yr))
                    if len(current_run) >= 2:
                        new_segments.append(current_run)
                    elif current_run:
                        # 1 個點的 run 太短 → 丟棄（畫不出線段）
                        changed = True
                if new_segments:
                    ann.strokes = new_segments
                    remaining.append(ann)
                else:
                    # 整個 annotation 的筆劃都被擦掉 → 刪 annotation
                    changed = True
            else:
                remaining.append(ann)
        if changed:
            self._annotations = remaining
        return changed

    def _page_no_for_current(self) -> Optional[int]:
        if (
            self._transcript is None
            or self._current_page_idx < 0
            or self._current_page_idx >= len(self._transcript.pages)
        ):
            return None
        return self._transcript.pages[self._current_page_idx].number

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
            # 標註畫在所有內容之上（不限於 slide 區）
            self._paint_annotations_on_viewport(painter)
            # 已複製 toast
            if self._copy_toast_until_ms > self._now_ms():
                self._paint_copy_toast(painter, vw, vh)
                # 安排重繪以讓 toast 自動消失
                from PySide6.QtCore import QTimer
                QTimer.singleShot(
                    max(0, self._copy_toast_until_ms - self._now_ms()),
                    self.update,
                )
        finally:
            painter.end()

    @staticmethod
    def _now_ms() -> int:
        import time
        return int(time.monotonic() * 1000)

    def _paint_copy_toast(self, painter: QPainter, vw: int, vh: int) -> None:
        """畫「📋 已複製」flash 在畫面上方中央。"""
        text = f"📋 已複製 {len(self._selected_text)} 字"
        painter.save()
        # 背景：半透明深色膠囊
        f = QFont(self._font_family, 13, QFont.Weight.Bold)
        painter.setFont(f)
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(text)
        th = fm.height()
        pad_x, pad_y = 18, 10
        rect_w = tw + pad_x * 2
        rect_h = th + pad_y * 2
        rx = (vw - rect_w) // 2
        ry = 60
        bg_rect = QRect(rx, ry, rect_w, rect_h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(30, 170, 80, 230))
        painter.drawRoundedRect(bg_rect, 16, 16)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(bg_rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

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
            self._last_slide_rect = None
            return
        page = self._transcript.pages[page_idx]
        page_no = page.number
        if page_no < 1 or page_no > self._slide_deck.page_count:
            painter.setPen(QColor("#3A3A3A"))
            painter.drawRect(rect)
            painter.setPen(QColor("#707070"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "（無對應投影片）")
            self._last_slide_rect = None
            return
        slide_page = self._slide_deck.pages[page_no - 1]
        aspect = (
            slide_page.height_pt / slide_page.width_pt
            if slide_page.width_pt > 0
            else 1.414
        )
        target_w = rect.width()
        target_h = int(target_w * aspect)
        if target_h > rect.height():
            target_h = rect.height()
            target_w = int(target_h / aspect) if aspect > 0 else rect.width()
        x = rect.left() + (rect.width() - target_w) // 2
        y = rect.top() + (rect.height() - target_h) // 2

        dpr = self.devicePixelRatioF() or 1.0
        pix = self._slide_deck.render(page_no, target_w, dpr)
        slide_rect = QRect(x, y, target_w, target_h)
        if pix is not None and not pix.isNull():
            painter.drawPixmap(x, y, pix)
            # slide_rect 用邏輯像素（target_w/target_h）；pix.width()/height() 在 HiDPI 下是物理像素
            slide_rect = QRect(x, y, target_w, target_h)
            painter.setPen(QColor("#3A3A3A"))
            painter.drawRect(slide_rect)
        else:
            painter.fillRect(slide_rect, QColor("#1A1A1A"))
            painter.setPen(QColor("#3A3A3A"))
            painter.drawRect(slide_rect)
        # 快取 slide rect（viewport 座標）供 mouse / 文字選取 使用
        self._last_slide_rect = slide_rect
        # 文字選取高亮（只針對 slide）
        self._paint_text_selection(painter, slide_rect, page_no)

    def _paint_annotations_on_viewport(self, painter: QPainter) -> None:
        """當前頁的所有標註以 viewport 0..1 比例座標畫在整個 viewport。"""
        for ann in self.current_page_annotations():
            if ann.kind == "stroke":
                self._paint_stroke(painter, ann)
            elif ann.kind == "note":
                self._paint_sticky_note(painter, ann)
        # 正在繪製中的鉛筆筆劃（尚未提交）
        if self._drawing_stroke and self._tool == self.TOOL_PENCIL:
            pen = QPen(self._tool_color)
            pen.setWidth(self._tool_stroke_width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolyline(self._drawing_stroke)

    def _paint_stroke(self, painter: QPainter, ann: Annotation) -> None:
        vw, vh = self.width(), self.height()
        pen = QPen(QColor(ann.color))
        pen.setWidth(ann.stroke_width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)   # ★ 不要填內部
        for segment in ann.strokes:
            if len(segment) < 2:
                continue
            # 用 drawPolyline（只描線）而非 drawPath（會把封閉區域填色）
            points = [QPointF(x * vw, y * vh) for (x, y) in segment]
            painter.drawPolyline(points)

    RESIZE_HANDLE_SIZE = 14

    def _paint_sticky_note(self, painter: QPainter, ann: Annotation) -> None:
        vw, vh = self.width(), self.height()
        nx = int(ann.x * vw)
        ny = int(ann.y * vh)
        nw = max(80, int(ann.width * vw))
        nh = max(40, int(ann.height * vh))
        note_rect = QRect(nx, ny, nw, nh)
        _paint_sticky_body(
            painter, note_rect, ann.color, ann.text,
            self._font_family, self.RESIZE_HANDLE_SIZE,
        )

    def _note_rect_in_viewport(self, ann: Annotation) -> QRect:
        vw, vh = max(1, self.width()), max(1, self.height())
        nx = int(ann.x * vw)
        ny = int(ann.y * vh)
        nw = max(80, int(ann.width * vw))
        nh = max(40, int(ann.height * vh))
        return QRect(nx, ny, nw, nh)

    def _find_note_resize_handle_at(self, point: QPoint) -> Optional[Annotation]:
        """點擊是否在便利貼右下角 handle 上。"""
        page_no = self._page_no_for_current()
        if page_no is None:
            return None
        s = self.RESIZE_HANDLE_SIZE
        for ann in self._annotations:
            if ann.slide_page != page_no or ann.kind != "note":
                continue
            r = self._note_rect_in_viewport(ann)
            if (
                r.right() - s <= point.x() <= r.right()
                and r.bottom() - s <= point.y() <= r.bottom()
            ):
                return ann
        return None

    def _paint_text_selection(
        self, painter: QPainter, slide_rect: QRect, page_no: int
    ) -> None:
        """Word 風格：對選取範圍內每個 word 畫藍色高亮矩形（指標模式下）。"""
        if (
            self._text_select_anchor_idx is None
            or self._text_select_focus_idx is None
            or self._slide_deck is None
        ):
            return
        try:
            blocks = self._slide_deck.get_text_blocks(page_no)
        except Exception:
            return
        if not blocks:
            return
        i0 = max(0, min(self._text_select_anchor_idx, self._text_select_focus_idx))
        i1 = min(len(blocks) - 1, max(self._text_select_anchor_idx, self._text_select_focus_idx))
        pdf_page = self._slide_deck.pages[page_no - 1]
        scale_x = slide_rect.width() / max(1, pdf_page.width_pt)
        scale_y = slide_rect.height() / max(1, pdf_page.height_pt)
        overlay = QColor(80, 160, 255, 110)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(overlay)
        for i in range(i0, i1 + 1):
            b = blocks[i]
            rx = int(slide_rect.left() + b.x0 * scale_x)
            ry = int(slide_rect.top() + b.y0 * scale_y)
            rw = int((b.x1 - b.x0) * scale_x)
            rh = int((b.y1 - b.y0) * scale_y)
            painter.drawRect(rx, ry, rw, rh)

    def _word_index_at_viewport(
        self, point: QPoint, slide_rect: QRect, page_no: int
    ) -> Optional[int]:
        """回傳 point 下面最近的 word block 的 index（reading order）。

        - 若點在某 word 的 bbox 內 → 回傳該 index
        - 若沒點到任何 word，回傳最接近的 word 的 index（以中心距離）
        """
        if self._slide_deck is None:
            return None
        try:
            blocks = self._slide_deck.get_text_blocks(page_no)
        except Exception:
            return None
        if not blocks:
            return None
        pdf_page = self._slide_deck.pages[page_no - 1]
        if slide_rect.width() <= 0 or slide_rect.height() <= 0:
            return None
        # viewport → PDF point
        pdf_x = (point.x() - slide_rect.left()) * pdf_page.width_pt / slide_rect.width()
        pdf_y = (point.y() - slide_rect.top()) * pdf_page.height_pt / slide_rect.height()
        # 先找是否點在某 word 內
        for i, b in enumerate(blocks):
            if b.x0 <= pdf_x <= b.x1 and b.y0 <= pdf_y <= b.y1:
                return i
        # 找最近的（中心距離）
        best_i, best_d = -1, float("inf")
        for i, b in enumerate(blocks):
            cx = (b.x0 + b.x1) / 2
            cy = (b.y0 + b.y1) / 2
            d = (cx - pdf_x) ** 2 + (cy - pdf_y) ** 2
            if d < best_d:
                best_d = d
                best_i = i
        return best_i if best_i >= 0 else None

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
