"""提詞顯示元件：卡拉 OK 式逐字高亮 + 平滑捲動。

關鍵設計：
- 整份講稿一次性載入到 QTextDocument。
- 內部維護 display_pos（已念到的全文字元位置），透過 QPropertyAnimation 從目前位置平滑推進到 target_pos，避免逐字跳動。
- 重新著色僅更新「delta 區段」，不每次刷整份文檔，效能與視覺都流暢。
- 捲動同樣以 QPropertyAnimation 動畫化 verticalScrollBar，保持目前位置位於畫面上 1/3 處。
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QPoint,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QTextCharFormat,
    QTextCursor,
    QWheelEvent,
)
from PySide6.QtWidgets import QTextEdit


class PrompterView(QTextEdit):
    """提詞器主顯示元件。"""

    position_clicked = Signal(int)  # 使用者點擊文字 → 全文字元位置
    font_size_changed = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # 顏色（由 set_colors 控制）
        self._color_spoken = QColor("#6B6B6B")
        self._color_upcoming = QColor("#F0F0F0")
        self._color_current = QColor("#FFD54A")
        self._color_skipped = QColor("#FF1744")  # 亮紅前景
        # 漏講背景色（半透明紅）— 為了讓視覺上極為醒目
        self._color_skipped_bg = QColor(255, 23, 68, 60)
        self._bg_color = QColor("#1E1E1E")

        # 位置
        self._target_pos = 0
        self._display_pos = 0
        self._doc_length = 0

        # 已標註為「漏講」的全文 char 區段，排序合併後存放
        self._skipped_ranges: list[tuple[int, int]] = []

        # 高亮動畫（位置）
        self._pos_anim = QPropertyAnimation(self, b"displayPos")
        self._pos_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._pos_anim.setDuration(150)

        # 捲動動畫
        self._scroll_anim = QPropertyAnimation(self.verticalScrollBar(), b"value")
        self._scroll_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scroll_anim.setDuration(200)

        # 字體
        self._font_size = 36
        self._font_family = "Microsoft JhengHei"
        self._line_spacing = 1.6
        self._apply_font()

        self._apply_palette()

    # ---------- 公開 API ----------

    def set_text(self, full_text: str) -> None:
        self.setPlainText(full_text)
        self._doc_length = len(full_text)
        self._target_pos = 0
        self._display_pos = 0
        self._skipped_ranges = []
        self._apply_full_format()
        self._apply_line_spacing()

    def mark_skipped(self, start: int, end: int) -> None:
        """把 [start, end) 標為「漏講」（亮紅 + 紅背景 + 刪除線），並記錄起來避免後續推進蓋掉。"""
        if self._doc_length <= 0:
            return
        start = max(0, min(start, self._doc_length))
        end = max(0, min(end, self._doc_length))
        if end <= start:
            return
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        fmt = QTextCharFormat()
        fmt.setForeground(self._color_skipped)
        fmt.setBackground(self._color_skipped_bg)
        fmt.setFontStrikeOut(True)
        cursor.mergeCharFormat(fmt)
        self._skipped_ranges.append((start, end))
        self._skipped_ranges = self._merge_ranges(self._skipped_ranges)
        # 強制重繪以確保視覺立即更新
        self.viewport().update()

    def mark_skipped_ranges(self, ranges: list[tuple[int, int]]) -> int:
        """批次標多個漏講區段；回傳實際標記的總字數。"""
        total = 0
        for s, e in ranges:
            before = sum(end - start for start, end in self._skipped_ranges)
            self.mark_skipped(s, e)
            after = sum(end - start for start, end in self._skipped_ranges)
            total += max(0, after - before)
        return total

    def clear_skipped(self) -> None:
        """清除所有漏講標記，並把這些區段恢復為 spoken/upcoming 色。"""
        if not self._skipped_ranges:
            return
        from PySide6.QtGui import QBrush
        for s, e in self._skipped_ranges:
            color = self._color_spoken if e <= self._display_pos else self._color_upcoming
            cursor = QTextCursor(self.document())
            cursor.setPosition(s)
            cursor.setPosition(e, QTextCursor.MoveMode.KeepAnchor)
            fmt = QTextCharFormat()
            fmt.setFontStrikeOut(False)
            fmt.setForeground(color)
            fmt.setBackground(QBrush(self._bg_color))  # 清除背景色
            cursor.mergeCharFormat(fmt)
        self._skipped_ranges = []
        self.viewport().update()

    def set_position(self, global_char: int, *, animate: bool = True) -> None:
        global_char = max(0, min(global_char, self._doc_length))
        self._target_pos = global_char
        if not animate:
            self._pos_anim.stop()
            old = self._display_pos
            self._display_pos = global_char
            self._repaint_delta(old, global_char)
            self._scroll_to_position(global_char, animate=False)
            return

        self._pos_anim.stop()
        self._pos_anim.setStartValue(self._display_pos)
        self._pos_anim.setEndValue(global_char)
        self._pos_anim.start()
        self._scroll_to_position(global_char, animate=True)

    def set_colors(
        self,
        *,
        spoken: str | None = None,
        upcoming: str | None = None,
        current: str | None = None,
        skipped: str | None = None,
        background: str | None = None,
    ) -> None:
        if spoken:
            self._color_spoken = QColor(spoken)
        if upcoming:
            self._color_upcoming = QColor(upcoming)
        if current:
            self._color_current = QColor(current)
        if skipped:
            self._color_skipped = QColor(skipped)
        if background:
            self._bg_color = QColor(background)
        self._apply_palette()
        self._apply_full_format()

    def set_font_family(self, family: str) -> None:
        self._font_family = family
        self._apply_font()

    def set_font_size(self, size: int) -> None:
        size = max(12, min(120, int(size)))
        if size == self._font_size:
            return
        self._font_size = size
        self._apply_font()
        self._apply_full_format()
        self._apply_line_spacing()
        self.font_size_changed.emit(size)

    def font_size(self) -> int:
        return self._font_size

    def set_line_spacing(self, factor: float) -> None:
        self._line_spacing = max(1.0, float(factor))
        self._apply_line_spacing()

    def set_animation_duration(self, ms: int) -> None:
        ms = max(0, int(ms))
        self._pos_anim.setDuration(ms)
        self._scroll_anim.setDuration(min(400, ms + 50))

    # ---------- 內部 ----------

    def _apply_palette(self) -> None:
        pal = self.palette()
        pal.setColor(self.viewport().backgroundRole(), self._bg_color)
        self.viewport().setPalette(pal)
        self.setStyleSheet(
            f"QTextEdit {{ background-color: {self._bg_color.name()}; "
            f"color: {self._color_upcoming.name()}; border: none; }}"
        )

    def _apply_font(self) -> None:
        f = QFont(self._font_family, self._font_size)
        f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        self.setFont(f)

    def _apply_line_spacing(self) -> None:
        # 用 block format 設行高 (ProportionalHeight = 1)
        cursor = self.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        block_format = cursor.blockFormat()
        block_format.setLineHeight(self._line_spacing * 100, 1)
        cursor.setBlockFormat(block_format)
        cursor.clearSelection()
        self.setTextCursor(cursor)

    def _make_format(self, color: QColor) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(color)
        return fmt

    def _apply_format_range(self, start: int, end: int, color: QColor) -> None:
        if start >= end or start < 0 or self._doc_length <= 0:
            return
        end = min(end, self._doc_length)
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.mergeCharFormat(self._make_format(color))

    def _apply_full_format(self) -> None:
        if self._doc_length == 0:
            return
        # 全部設為 upcoming，再把已念過的部分塗成 spoken
        self._apply_format_range(0, self._doc_length, self._color_upcoming)
        if self._display_pos > 0:
            self._paint_spoken_excluding_skipped(0, self._display_pos)
        # 重畫 skipped 區段（含刪除線）
        self._reapply_skipped_format()
        self._apply_current_marker(self._display_pos)

    def _repaint_delta(self, old_pos: int, new_pos: int) -> None:
        if new_pos > old_pos:
            self._paint_spoken_excluding_skipped(old_pos, new_pos)
        elif new_pos < old_pos:
            self._paint_upcoming_excluding_skipped(new_pos, old_pos)
        self._apply_current_marker(new_pos)

    def _paint_spoken_excluding_skipped(self, start: int, end: int) -> None:
        """把 [start, end) 塗成 spoken 色，但不覆蓋已被標為 skipped 的區段。"""
        for sub_start, sub_end in self._iter_unskipped(start, end):
            self._apply_format_range(sub_start, sub_end, self._color_spoken)

    def _paint_upcoming_excluding_skipped(self, start: int, end: int) -> None:
        for sub_start, sub_end in self._iter_unskipped(start, end):
            self._apply_format_range(sub_start, sub_end, self._color_upcoming)

    def _iter_unskipped(self, start: int, end: int):
        """產生 [start, end) 中不在 skipped 區段的子區段。"""
        cur = start
        for s, e in self._skipped_ranges:
            if e <= cur:
                continue
            if s >= end:
                break
            if s > cur:
                yield (cur, min(s, end))
            cur = max(cur, e)
            if cur >= end:
                return
        if cur < end:
            yield (cur, end)

    def _reapply_skipped_format(self) -> None:
        for s, e in self._skipped_ranges:
            cursor = QTextCursor(self.document())
            cursor.setPosition(s)
            cursor.setPosition(e, QTextCursor.MoveMode.KeepAnchor)
            fmt = QTextCharFormat()
            fmt.setForeground(self._color_skipped)
            fmt.setBackground(self._color_skipped_bg)
            fmt.setFontStrikeOut(True)
            cursor.mergeCharFormat(fmt)

    @staticmethod
    def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if not ranges:
            return []
        ranges = sorted(ranges)
        merged: list[tuple[int, int]] = [ranges[0]]
        for s, e in ranges[1:]:
            ls, le = merged[-1]
            if s <= le:
                merged[-1] = (ls, max(le, e))
            else:
                merged.append((s, e))
        return merged

    def _apply_current_marker(self, pos: int) -> None:
        # 在目前位置周圍 1~2 個字塗上「current」色，呈現亮點推進感
        # 為避免閃爍，先恢復前一個 marker 的色（已由 spoken 覆蓋過了）
        marker_len = 2
        end = min(self._doc_length, pos + marker_len)
        if pos < end:
            self._apply_format_range(pos, end, self._color_current)

    # ---------- displayPos 動畫 property ----------

    def _get_display_pos(self) -> int:
        return self._display_pos

    def _set_display_pos(self, value: int) -> None:
        old = self._display_pos
        new = max(0, min(int(value), self._doc_length))
        if new == old:
            return
        self._display_pos = new
        self._repaint_delta(old, new)

    displayPos = Property(int, _get_display_pos, _set_display_pos)

    # ---------- 捲動 ----------

    def _scroll_to_position(self, global_char: int, *, animate: bool) -> None:
        cursor = QTextCursor(self.document())
        cursor.setPosition(min(global_char, self._doc_length))
        rect = self.cursorRect(cursor)
        viewport_height = self.viewport().height()
        # 目標：把該位置維持在視窗上方 1/3
        target_y = viewport_height // 3
        cursor_y_in_viewport = rect.top()
        delta = cursor_y_in_viewport - target_y
        if abs(delta) < 4:
            return
        sb = self.verticalScrollBar()
        new_value = sb.value() + delta
        new_value = max(sb.minimum(), min(sb.maximum(), new_value))
        if not animate:
            sb.setValue(new_value)
            return
        self._scroll_anim.stop()
        self._scroll_anim.setStartValue(sb.value())
        self._scroll_anim.setEndValue(new_value)
        self._scroll_anim.start()

    # ---------- 互動 ----------

    def wheelEvent(self, event: QWheelEvent) -> None:
        # 只在 Ctrl 明確按下時才縮放字體；其他狀況走預設捲動
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            step = 2 if delta > 0 else -2
            self.set_font_size(self._font_size + step)
            event.accept()
            return
        super().wheelEvent(event)

    # 覆寫 QTextEdit 內建 zoom 方法，避免被自動捲動或外部事件誤觸發
    def zoomIn(self, range=1):  # noqa: A003 (override Qt API)
        pass

    def zoomOut(self, range=1):  # noqa: A003
        pass

    def zoomInF(self, range=1):  # noqa: A003
        pass

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        cursor = self.cursorForPosition(event.position().toPoint() if hasattr(event, "position") else event.pos())
        self.position_clicked.emit(cursor.position())
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # 字體快捷鍵（Ctrl + +/-/=）
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self.set_font_size(self._font_size + 2)
                event.accept()
                return
            if event.key() == Qt.Key.Key_Minus:
                self.set_font_size(self._font_size - 2)
                event.accept()
                return
        super().keyPressEvent(event)
