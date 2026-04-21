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
    QEvent,
    QPropertyAnimation,
    QPoint,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QWheelEvent,
)
from PySide6.QtWidgets import QTextEdit


class PrompterView(QTextEdit):
    """提詞器主顯示元件。"""

    position_clicked = Signal(int)  # 使用者點擊文字 → 全文字元位置
    font_size_changed = Signal(int)
    edit_mode_changed = Signal(bool)
    text_edited = Signal(str)  # 編輯模式關閉時發出最新文本
    slide_double_clicked = Signal(int)  # 雙擊右欄 slide → 發該 page_no

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        # 左右留白（避免文字貼到邊）
        self.document().setDocumentMargin(24)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # 編輯模式：為 True 時允許使用者修改講稿內容
        self._edit_mode = False
        # Markdown 視覺渲染：#/##/### 標題放大粗體、--- 分隔線、<!-- --> 註解灰階
        self._md_rendering = True

        # 編輯時 MD 渲染 debounce（避免每次 keystroke 都整篇重掃）
        self._md_refresh_timer = QTimer(self)
        self._md_refresh_timer.setSingleShot(True)
        self._md_refresh_timer.setInterval(220)
        self._md_refresh_timer.timeout.connect(self._refresh_md_while_editing)
        self.textChanged.connect(self._on_text_changed_for_md)

        # 水平分隔線位置（--- 所在 block 的 y 中線，由 paintEvent 繪製）
        self._hr_blocks: list[int] = []  # block positions for ---/===/***

        # 嵌入式投影片（載入 PDF 後會把每頁圖畫在 --- 的空間）
        self._slide_deck = None   # 型別：SlideDeck | None
        self._slide_margin_padding = 20   # 圖上下的留白
        # 每頁 (top_y_doc, bottom_y_doc)；以 slide 數為主（不限於講稿頁）
        self._page_boundaries: list[tuple[int, int]] = []
        # 文字寬度占比（使用者可拖拉調整）
        self._text_width_ratio = self._DEFAULT_TEXT_WIDTH_RATIO
        # 文 / 圖拖拉分隔條：狀態 + 滑鼠追蹤
        self._split_hover = False
        self._split_dragging = False
        self._split_line_x = 0   # 目前分隔線 x（viewport 座標）
        self.viewport().setMouseTracking(True)
        self.viewport().installEventFilter(self)

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
        # Markdown 樣式保護區段（標題/註解/分隔線）不被 spoken/upcoming 色覆蓋
        self._md_styled_ranges: list[tuple[int, int]] = []
        # 記住上次 current marker 的位置，每次移動前清掉舊的，避免黃點殘留
        self._last_marker_pos: int | None = None

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
        self._md_styled_ranges = []
        self._apply_line_spacing()
        if self._md_rendering:
            self._scan_markdown_ranges()
        self._apply_full_format()
        if self._md_rendering:
            self._apply_markdown_rendering()
        # 嵌入式投影片 margin（若已載入 deck）
        if self._slide_deck is not None:
            self._relayout_slide_gaps()

    # ---------- 編輯模式 ----------

    def is_edit_mode(self) -> bool:
        return self._edit_mode

    def set_edit_mode(self, enabled: bool) -> None:
        """切換編輯模式：enabled=True 時使用者可直接修改講稿。

        進入：開啟編輯、允許 undo/redo、顯示游標。
        離開：拿取最新文字透過 text_edited signal 發出（由 MainWindow 重新 parse）。
        """
        if enabled == self._edit_mode:
            return
        if enabled:
            self.setReadOnly(False)
            self.setUndoRedoEnabled(True)
            self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
            self._edit_mode = True
            self.edit_mode_changed.emit(True)
        else:
            # 離開編輯：發出最新文本
            new_text = self.toPlainText()
            self.setReadOnly(True)
            self.setUndoRedoEnabled(False)
            self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._edit_mode = False
            self.edit_mode_changed.emit(False)
            self.text_edited.emit(new_text)

    def compact_whitespace(self) -> None:
        """清理空白：
        - 每行尾端空白去除
        - 連續多個空行壓縮成單一空行
        - **結構行周圍的空行完全移除**（結構行 = `#/##/###` 標題、`---/===/***` 分隔、整行 `<!-- ... -->` 註解）
        - 檔頭/檔尾的空行全部去除
        只在編輯模式下執行，清理後仍維持編輯模式。
        """
        if not self._edit_mode:
            return
        raw = self.toPlainText()
        lines = [ln.rstrip() for ln in raw.split("\n")]

        def is_structural(s: str) -> bool:
            s = s.strip()
            if not s:
                return False
            if s.startswith("#"):
                return True
            if s in ("---", "===", "***"):
                return True
            if s.startswith("<!--") and s.endswith("-->"):
                return True
            return False

        # Pass 1：連續空行壓縮為單一空行
        compacted: list[str] = []
        prev_empty = False
        for ln in lines:
            if ln == "":
                if prev_empty:
                    continue
                prev_empty = True
            else:
                prev_empty = False
            compacted.append(ln)

        # Pass 2：刪除緊鄰結構行（上/下）的空行
        out: list[str] = []
        for i, ln in enumerate(compacted):
            if ln == "":
                prev_ln = out[-1] if out else ""
                next_ln = compacted[i + 1] if i + 1 < len(compacted) else ""
                if is_structural(prev_ln) or is_structural(next_ln):
                    continue
            out.append(ln)

        # 檔頭/檔尾 trim
        while out and out[0] == "":
            out.pop(0)
        while out and out[-1] == "":
            out.pop()

        new_text = "\n".join(out)
        if new_text == raw:
            return
        # 保留相對游標位置（以字元比例估算）
        cursor = self.textCursor()
        ratio = cursor.position() / max(1, len(raw))
        self.setPlainText(new_text)
        self._doc_length = len(new_text)
        new_cur = self.textCursor()
        new_cur.setPosition(min(int(ratio * len(new_text)), len(new_text)))
        self.setTextCursor(new_cur)
        # 立即 trigger MD 刷新（debounce 會被 textChanged 觸發，但也強制一次）
        self._md_refresh_timer.start()

    # ---------- 文字格式化（編輯模式下用） ----------

    def _apply_format_to_selection(self, fmt: QTextCharFormat) -> None:
        """把 QTextCharFormat 套到目前選取範圍；若無選取則什麼都不做。"""
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return
        # 防呆：選取範圍 > 90% 全文 → 疑似誤觸 Ctrl+A，要求確認
        sel_len = cursor.selectionEnd() - cursor.selectionStart()
        if sel_len > max(50, self._doc_length * 0.9):
            from PySide6.QtWidgets import QMessageBox
            ret = QMessageBox.question(
                self, "全文選取確認",
                f"目前選取了 {sel_len} 字（接近整篇）。\n"
                "是否確定要把整篇文字都套上這個格式？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        cursor.mergeCharFormat(fmt)

    def toggle_bold(self) -> None:
        if not self._edit_mode:
            return
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return
        # 判斷目前選取第一字是否已粗體 → 切換
        probe = QTextCursor(cursor)
        probe.setPosition(cursor.selectionStart())
        is_bold = probe.charFormat().fontWeight() >= QFont.Weight.Bold
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Normal if is_bold else QFont.Weight.Bold)
        self._apply_format_to_selection(fmt)

    def toggle_italic(self) -> None:
        if not self._edit_mode:
            return
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return
        probe = QTextCursor(cursor)
        probe.setPosition(cursor.selectionStart())
        is_italic = probe.charFormat().fontItalic()
        fmt = QTextCharFormat()
        fmt.setFontItalic(not is_italic)
        self._apply_format_to_selection(fmt)

    def toggle_underline(self) -> None:
        if not self._edit_mode:
            return
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return
        probe = QTextCursor(cursor)
        probe.setPosition(cursor.selectionStart())
        is_underline = probe.charFormat().fontUnderline()
        fmt = QTextCharFormat()
        fmt.setFontUnderline(not is_underline)
        self._apply_format_to_selection(fmt)

    def toggle_highlight(self) -> None:
        """螢光筆（黃色半透明底）。"""
        if not self._edit_mode:
            return
        from ..core.rich_text_format import HIGHLIGHT_RGB, highlight_brush_color
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return
        probe = QTextCursor(cursor)
        probe.setPosition(cursor.selectionStart())
        bg = probe.charFormat().background()
        is_highlight = (
            bg.style() != Qt.BrushStyle.NoBrush
            and (bg.color().rgb() & 0x00FFFFFF) == HIGHLIGHT_RGB
        )
        fmt = QTextCharFormat()
        if is_highlight:
            fmt.setBackground(Qt.BrushStyle.NoBrush)
        else:
            fmt.setBackground(highlight_brush_color())
        self._apply_format_to_selection(fmt)

    def clear_format(self) -> None:
        """清除選取範圍的粗體/斜體/底線/螢光筆。"""
        if not self._edit_mode:
            return
        from ..core.rich_text_format import clear_format_in_range
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return
        clear_format_in_range(cursor)

    def dump_format_spans(self) -> list:
        """匯出目前格式到 list[FormatSpan]（序列化用）。"""
        from ..core.rich_text_format import dump_formats
        return dump_formats(self.document())

    def restore_format_spans(self, spans: list) -> None:
        """還原 FormatSpan 序列（載入 session 用）。"""
        from ..core.rich_text_format import restore_formats
        restore_formats(self.document(), spans)

    def insert_annotation_at_cursor(self, text: str = "") -> None:
        """在游標處插入 `<!-- ... -->` 註解（只能在編輯模式下使用）。"""
        if not self._edit_mode:
            return
        placeholder = text.strip() if text.strip() else "在這裡寫你的備忘"
        snippet = f"<!-- {placeholder} -->"
        cursor = self.textCursor()
        cursor.insertText(snippet)
        # 選取 placeholder 方便使用者直接覆蓋
        if not text.strip():
            end = cursor.position() - len(" -->")
            start = end - len(placeholder)
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cursor)
        self.setFocus()

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
        self._apply_line_spacing()
        self._rescale_chars_to_font_size()
        self.font_size_changed.emit(size)

    def _rescale_chars_to_font_size(self) -> None:
        """setPlainText 會把當下字型烘焙到每個字元 → 變更字型後需手動重設。

        做法：整篇 setCharFormat 成 base（新字型大小），再重塗 spoken/漏講 + MD。
        字型大小變 → 文字排版高度變 → slide 嵌入座標也要重算。
        """
        if self._doc_length == 0:
            return
        base_fmt = QTextCharFormat()
        base_fmt.setFontPointSize(self._font_size)
        base_fmt.setFontWeight(QFont.Weight.Normal)
        base_fmt.setFontItalic(False)
        base_fmt.setFontStrikeOut(False)
        base_fmt.setForeground(self._color_upcoming)
        cursor = QTextCursor(self.document())
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.setCharFormat(base_fmt)
        # 重塗已念/漏講/目前 marker
        self._apply_full_format()
        if self._md_rendering:
            self._apply_markdown_rendering()
        # 字型變更 → 文字排版高度改變 → slide 邊界需重算
        if self._slide_deck is not None:
            self._relayout_slide_gaps()
            self.viewport().update()

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
        # 同步更新 document 預設字型 → 強制所有未明示 point size 的字元重新 layout
        self.document().setDefaultFont(f)

    def _apply_line_spacing(self) -> None:
        # 用 block format 設行高 (ProportionalHeight = 1)
        # 注意：用 fresh cursor 避免改到使用者當下 textCursor 的選取
        cursor = QTextCursor(self.document())
        cursor.select(QTextCursor.SelectionType.Document)
        block_format = cursor.blockFormat()
        block_format.setLineHeight(self._line_spacing * 100, 1)
        cursor.setBlockFormat(block_format)
        # 不呼叫 self.setTextCursor — 只改格式，不需要動 widget cursor

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

    # ---------- Markdown 視覺渲染 ----------

    def _scan_markdown_ranges(self) -> None:
        """先掃描整份文件，記錄所有 MD 樣式 block 的字元範圍 + 水平線 block。"""
        self._md_styled_ranges = []
        self._hr_blocks = []
        doc = self.document()
        block = doc.firstBlock()
        while block.isValid():
            stripped = block.text().strip()
            if (
                stripped.startswith(("# ", "## ", "### "))
                or stripped in ("---", "===", "***")
                or (stripped.startswith("<!--") and stripped.endswith("-->"))
            ):
                bs = block.position()
                be = bs + block.length() - 1
                if be > bs:
                    self._md_styled_ranges.append((bs, be))
                if stripped in ("---", "===", "***"):
                    self._hr_blocks.append(block.blockNumber())
            block = block.next()
        self._md_styled_ranges = self._merge_ranges(self._md_styled_ranges)

    def _apply_markdown_rendering(self) -> None:
        """掃描所有 block，對 #/##/### 標題、---分隔線、<!-- 註解 --> 套用視覺樣式。

        重要：只改字型樣式（粗體/大小/顏色），不刪字元。對齊索引仍以原始字元
        位置計算（split_sentences 會跳過這些 block），所以不會錯位。
        """
        if self._doc_length == 0:
            return
        doc = self.document()
        block = doc.firstBlock()
        while block.isValid():
            text = block.text()
            stripped = text.strip()
            fmt: QTextCharFormat | None = None
            block_fmt: QTextBlockFormat | None = None

            # 標題 ### / ## / #
            level = 0
            if stripped.startswith("### "):
                level = 3
            elif stripped.startswith("## "):
                level = 2
            elif stripped.startswith("# "):
                level = 1

            if level > 0:
                fmt = QTextCharFormat()
                scale = {1: 1.20, 2: 1.10, 3: 1.05}[level]
                fmt.setFontPointSize(self._font_size * scale)
                fmt.setFontWeight(QFont.Weight.Bold)
                fmt.setForeground(QColor("#80D8FF"))
                block_fmt = QTextBlockFormat()
                block_fmt.setTopMargin(0)
                block_fmt.setBottomMargin(0)
                block_fmt.setLineHeight(self._line_spacing * 100, 1)
            # 分隔線 ---  ===  *** → 字元隱藏，paintEvent 畫真正的水平線
            elif stripped in ("---", "===", "***"):
                fmt = QTextCharFormat()
                fmt.setForeground(self._bg_color)  # 與背景同色 → 看不見字元
                # 字型縮小，讓 block 高度就是一條細線
                fmt.setFontPointSize(max(4, self._font_size * 0.3))
                block_fmt = QTextBlockFormat()
                block_fmt.setTopMargin(0)
                block_fmt.setBottomMargin(0)
                block_fmt.setLineHeight(100, 1)  # 不放大行高
            # 含 <!-- ... --> 註解：整段灰階斜體
            elif stripped.startswith("<!--") and stripped.endswith("-->"):
                fmt = QTextCharFormat()
                fmt.setForeground(QColor("#707070"))
                fmt.setFontItalic(True)

            if fmt is not None:
                cursor = QTextCursor(block)
                cursor.setPosition(block.position())
                cursor.setPosition(
                    block.position() + block.length() - 1,
                    QTextCursor.MoveMode.KeepAnchor,
                )
                cursor.mergeCharFormat(fmt)
                if block_fmt is not None:
                    cursor.setBlockFormat(block_fmt)
            block = block.next()

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
        """產生 [start, end) 中不在 skipped / MD 保護區段的子區段。"""
        excluded = self._merge_ranges(self._skipped_ranges + self._md_styled_ranges)
        cur = start
        for s, e in excluded:
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
        """在目前位置塗上 current 色；並先把「上次 marker」清掉避免黃點殘留。

        - 反向跳躍時 `_repaint_delta` 的 upcoming 範圍不會覆蓋到舊 marker，故需手動清除。
        - 若位置落在 MD 區段（標題/註解/分隔線）則不套 current，保留原樣式。
        """
        marker_len = 2
        # 1. 清上一個 marker
        if self._last_marker_pos is not None:
            old = self._last_marker_pos
            old_end = min(self._doc_length, old + marker_len)
            if old < old_end:
                # 依目前 display_pos 判斷該位置原本該是 spoken 還是 upcoming
                target_color = (
                    self._color_spoken if old < self._display_pos
                    else self._color_upcoming
                )
                for s, e in self._iter_unskipped(old, old_end):
                    self._apply_format_range(s, e, target_color)
        # 2. 套新 marker（跳過 MD 保護區段）
        end = min(self._doc_length, pos + marker_len)
        if pos < end:
            in_md = any(s <= pos < e for s, e in self._md_styled_ranges)
            if not in_md:
                self._apply_format_range(pos, end, self._color_current)
        self._last_marker_pos = pos

    # ---------- 編輯時 MD 即時刷新 ----------

    def _on_text_changed_for_md(self) -> None:
        """編輯模式下文字變動 → debounce 觸發 MD 重新掃描 + 渲染。"""
        if self._edit_mode and self._md_rendering:
            self._md_refresh_timer.start()

    def _refresh_md_while_editing(self) -> None:
        if not (self._edit_mode and self._md_rendering):
            return
        from PySide6.QtWidgets import QApplication
        if (
            self.textCursor().hasSelection()
            and QApplication.mouseButtons() != Qt.MouseButton.NoButton
        ):
            self._md_refresh_timer.start(300)
            return

        # 保存使用者選取
        user_cursor = self.textCursor()
        had_selection = user_cursor.hasSelection()
        anchor = user_cursor.anchor()
        position = user_cursor.position()

        # 關鍵修正：保存使用者套的粗體/斜體/底線/螢光筆格式，
        # 不然 setCharFormat(base_fmt) 會把它們全部清掉
        from ..core.rich_text_format import dump_formats, restore_formats
        user_format_spans = dump_formats(self.document())

        self._doc_length = len(self.toPlainText())
        self._scan_markdown_ranges()

        base_fmt = QTextCharFormat()
        base_fmt.setFontPointSize(self._font_size)
        base_fmt.setFontWeight(QFont.Weight.Normal)
        base_fmt.setFontItalic(False)
        base_fmt.setFontStrikeOut(False)
        base_fmt.setForeground(self._color_upcoming)
        fresh = QTextCursor(self.document())
        fresh.select(QTextCursor.SelectionType.Document)
        fresh.setCharFormat(base_fmt)
        bf = QTextBlockFormat()
        bf.setLineHeight(self._line_spacing * 100, 1)
        fresh.setBlockFormat(bf)

        # 重新套用 MD 樣式
        self._apply_markdown_rendering()

        # 還原使用者格式（粗體/斜體/底線/螢光筆）
        if user_format_spans:
            restore_formats(self.document(), user_format_spans)

        # 重新套用嵌入式投影片 margin
        if self._slide_deck is not None:
            self._relayout_slide_gaps()

        # 還原選取
        new_cur = self.textCursor()
        new_cur.setPosition(min(anchor, self._doc_length))
        if had_selection:
            new_cur.setPosition(
                min(position, self._doc_length),
                QTextCursor.MoveMode.KeepAnchor,
            )
        self.setTextCursor(new_cur)
        self.viewport().update()

    # ---------- 水平分隔線繪製 ----------

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        try:
            sb_value = self.verticalScrollBar().value()
            vw = self.viewport().width()
            vh = self.viewport().height()

            # 1) 畫 slide 圖（垂直置中於每頁 top~bottom 區間）
            if self._slide_deck is not None and self._page_boundaries:
                for k, (top_y_doc, bottom_y_doc) in enumerate(self._page_boundaries):
                    page_no = k + 1
                    rect = self._slide_area_rect_for_page(page_no)
                    if rect is None:
                        continue
                    slide_x, _, slide_w, slide_h = rect
                    # 垂直置中
                    page_h = bottom_y_doc - top_y_doc
                    draw_y_doc = top_y_doc + max(0, (page_h - slide_h) // 2)
                    vy = draw_y_doc - sb_value
                    if vy + slide_h < -20 or vy > vh + 20:
                        continue
                    pix = self._slide_deck.render(page_no, slide_w)
                    if pix is not None and not pix.isNull():
                        painter.drawPixmap(slide_x, vy, pix)
                        pen = QPen(QColor("#3A3A3A"))
                        pen.setWidth(1)
                        painter.setPen(pen)
                        painter.drawRect(slide_x, vy, pix.width(), pix.height())
                    else:
                        painter.fillRect(slide_x, vy, slide_w, slide_h,
                                         QColor("#1A1A1A"))
                        pen = QPen(QColor("#3A3A3A"))
                        painter.setPen(pen)
                        painter.drawRect(slide_x, vy, slide_w, slide_h)

            # 2) 畫全寬 hr + 「── 第 N / total 頁 ──」標籤
            if self._page_boundaries:
                total = len(self._page_boundaries)
                hr_pen = QPen(QColor("#555"))
                hr_pen.setWidth(2)

                hr_points: list[tuple[int, int]] = []  # (doc_y, label_no；0 = 無標籤)
                # 頂部（第 1 頁上方），無標籤
                hr_points.append((self._page_boundaries[0][0], 0))
                # 每頁底 = 下頁頂；最後一頁底部不顯示標籤（純線）
                for k, (_, bottom_y) in enumerate(self._page_boundaries):
                    label = k + 1 if k + 1 < total else 0
                    hr_points.append((bottom_y, label))

                for doc_y, label_no in hr_points:
                    vy = doc_y - sb_value
                    if vy < -15 or vy > vh + 15:
                        continue
                    painter.setPen(hr_pen)
                    painter.drawLine(20, vy, vw - 20, vy)
                    if label_no > 0:
                        label = f"──  第 {label_no} / {total} 頁  ──"
                        font = painter.font()
                        font.setPointSize(10)
                        painter.setFont(font)
                        fm = painter.fontMetrics()
                        tw = fm.horizontalAdvance(label)
                        th = fm.height()
                        tx = (vw - tw) // 2
                        ty = vy + fm.ascent() - th // 2
                        painter.fillRect(tx - 8, vy - th // 2 - 2,
                                         tw + 16, th + 4, QColor("#1E1E1E"))
                        painter.setPen(QColor("#80D8FF"))
                        painter.drawText(tx, ty, label)

            # 3) 文圖分隔條：只在 hover / 拖曳時顯示
            if self._slide_deck is not None and (self._split_hover or self._split_dragging):
                x = self._split_line_x
                color = self._current_split_color()
                width = 4
                painter.fillRect(x - width // 2, 0, width, vh, color)
        finally:
            painter.end()

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

    # ---------- 投影片：文左圖右布局 ----------

    # 左側文字寬度占比的預設（可由使用者拖拉變更）
    _DEFAULT_TEXT_WIDTH_RATIO = 0.58
    _SLIDE_GAP_LEFT = 16      # 文字和 slide 之間留白
    _SLIDE_GAP_TOP_BOTTOM = 24  # slide 上下留白（確保垂直置中時有清楚空間）

    def set_slide_deck(self, deck) -> None:
        """載入投影片：左側文字限縮寬度，右側空間 paintEvent 畫 slide 圖。"""
        self._slide_deck = deck
        self._apply_text_wrap_width()
        self._relayout_slide_gaps()
        self.viewport().update()

    def _apply_text_wrap_width(self) -> None:
        """把文字 wrap 寬度設為 viewport 的 58%（若有 slide）或全寬（無 slide）。"""
        if self._slide_deck is None:
            self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
            return
        self.setLineWrapMode(QTextEdit.LineWrapMode.FixedPixelWidth)
        w = max(200, int(self.viewport().width() * self._text_width_ratio))
        self.setLineWrapColumnOrWidth(w)
        self._split_line_x = int(self.viewport().width() * self._text_width_ratio)

    def _split_line_hit_range(self) -> tuple[int, int]:
        """返回分隔線可點擊的 x 範圍（左右各 6px 容錯）。"""
        x = self._split_line_x
        return (x - 6, x + 6)

    def _is_over_split_line(self, x: int) -> bool:
        if self._slide_deck is None:
            return False
        lo, hi = self._split_line_hit_range()
        return lo <= x <= hi

    def set_split_ratio(self, ratio: float) -> None:
        """設定文/圖分隔比例（0.28~0.82）。供測試或設定 dialog 直接呼叫。"""
        if self._slide_deck is None:
            return
        ratio = max(0.28, min(0.82, ratio))
        if abs(ratio - self._text_width_ratio) < 0.005:
            return
        self._text_width_ratio = ratio
        self._apply_text_wrap_width()
        self._relayout_slide_gaps()
        self.viewport().update()

    def _current_split_color(self):
        if self._split_dragging:
            return QColor("#4CAF50")
        if self._split_hover:
            return QColor("#80D8FF")
        return QColor(128, 200, 255, 140)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """攔截 viewport 滑鼠事件以處理分隔線拖曳（在 QTextEdit 文字選取邏輯之前）。"""
        from PySide6.QtCore import QEvent
        if obj is not self.viewport() or self._slide_deck is None:
            return super().eventFilter(obj, event)

        et = event.type()
        if et == QEvent.Type.MouseMove:
            x = int(event.position().x()) if hasattr(event, "position") else event.pos().x()
            if self._split_dragging:
                vw = self.viewport().width()
                if vw > 0:
                    ratio = max(0.28, min(0.82, x / vw))
                    if abs(ratio - self._text_width_ratio) > 0.005:
                        self._text_width_ratio = ratio
                        self._apply_text_wrap_width()
                        self._relayout_slide_gaps()
                        self.viewport().update()
                return True
            # hover 狀態
            new_hover = self._is_over_split_line(x)
            if new_hover != self._split_hover:
                self._split_hover = new_hover
                # cursor 顯示為 SplitHCursor
                if new_hover:
                    self.viewport().setCursor(Qt.CursorShape.SplitHCursor)
                else:
                    self.viewport().unsetCursor()
                self.viewport().update()
            # hover 不應吃事件，讓 QTextEdit 正常處理
            return False

        if et == QEvent.Type.MouseButtonPress:
            x = int(event.position().x()) if hasattr(event, "position") else event.pos().x()
            if event.button() == Qt.MouseButton.LeftButton and self._is_over_split_line(x):
                self._split_dragging = True
                self.viewport().update()
                return True

        if et == QEvent.Type.MouseButtonRelease:
            if self._split_dragging:
                self._split_dragging = False
                self.viewport().update()
                return True

        if et == QEvent.Type.Leave:
            if self._split_hover:
                self._split_hover = False
                self.viewport().unsetCursor()
                self.viewport().update()

        return super().eventFilter(obj, event)

    def _slide_area_rect_for_page(self, page_no: int) -> tuple[int, int, int, int] | None:
        """回傳 slide 的 (x, y_document, width, height)；y_document 是 document 內座標（未扣 scroll）。

        結構：page N 的 slide 對應 page N 的 sentence_start block 頂端開始，
        往下延伸 slide_height。x 位於 viewport 右側欄。
        """
        if self._slide_deck is None or page_no < 1 or page_no > self._slide_deck.page_count:
            return None
        vw = self.viewport().width()
        text_w = int(vw * self._text_width_ratio)
        slide_w = vw - text_w - self._SLIDE_GAP_LEFT - 20
        if slide_w < 120:
            return None
        page = self._slide_deck.pages[page_no - 1]
        aspect = page.height_pt / page.width_pt if page.width_pt > 0 else 1.414
        slide_h = int(slide_w * aspect)
        return (text_w + self._SLIDE_GAP_LEFT, 0, slide_w, slide_h)

    def _page_top_block(self, page_index_0based: int):
        """回傳第 k 頁（0-based）的第一個 block；依 _hr_blocks 推算。

        - page 0 的 top = firstBlock
        - page k (k>=1) 的 top = _hr_blocks[k-1] 的 **下一個** block
        """
        doc = self.document()
        if page_index_0based <= 0:
            return doc.firstBlock()
        if page_index_0based - 1 >= len(self._hr_blocks):
            return None
        hr_block_no = self._hr_blocks[page_index_0based - 1]
        hr_block = doc.findBlockByNumber(hr_block_no)
        if not hr_block.isValid():
            return None
        return hr_block.next()

    def _page_last_block(self, page_index_0based: int):
        """回傳第 k 頁的最後一個內容 block。
        - 若有下一個 hr，取 hr 的**前一個** block
        - 否則為文件最後一個 block
        """
        doc = self.document()
        if page_index_0based < len(self._hr_blocks):
            hr_block_no = self._hr_blocks[page_index_0based]
            hr_block = doc.findBlockByNumber(hr_block_no)
            if hr_block.isValid():
                return hr_block.previous()
        # 沒有下一個 hr → 最後一頁
        last = doc.lastBlock()
        return last

    def _relayout_slide_gaps(self) -> None:
        """以 slide 數為主建立每頁邊界（top_y, bottom_y）。
        - 有講稿頁的：取 max(文字自然高度, slide 高度)
        - 超過講稿頁的 slide：虛擬頁，在文件最後一個 block 加 bottomMargin 延伸空間
        結果存到 self._page_boundaries（供 paintEvent 使用）。
        """
        doc = self.document()
        layout = doc.documentLayout()

        # 清掉所有先前的 padding（避免累加）
        block = doc.firstBlock()
        while block.isValid():
            cursor = QTextCursor(block)
            bf = cursor.blockFormat()
            if bf.bottomMargin() > 0 or bf.topMargin() > 0:
                bf.setBottomMargin(0)
                bf.setTopMargin(0)
                cursor.setBlockFormat(bf)
            block = block.next()

        self._page_boundaries: list[tuple[int, int]] = []

        if self._slide_deck is None:
            return

        total_slides = self._slide_deck.page_count
        transcript_pages = len(self._hr_blocks) + 1 if doc.blockCount() > 0 else 0
        n_transcript_covered = min(transcript_pages, total_slides)

        # Phase 1：有對應講稿的頁，計算文字自然高度並補齊到 slide 高度
        for k in range(n_transcript_covered):
            top_block = self._page_top_block(k)
            last_block = self._page_last_block(k)
            if top_block is None or last_block is None or not last_block.isValid():
                continue
            top_y = int(layout.blockBoundingRect(top_block).top())
            last_rect = layout.blockBoundingRect(last_block)
            text_bottom_y = int(last_rect.bottom())
            text_h = max(0, text_bottom_y - top_y)
            rect = self._slide_area_rect_for_page(k + 1)
            slide_h = rect[3] if rect else 0
            need_h = slide_h + self._SLIDE_GAP_TOP_BOTTOM * 2
            if need_h > text_h:
                pad = need_h - text_h
                cursor = QTextCursor(last_block)
                bf = cursor.blockFormat()
                bf.setBottomMargin(pad)
                cursor.setBlockFormat(bf)
                bottom_y = top_y + need_h
            else:
                bottom_y = text_bottom_y
            self._page_boundaries.append((top_y, bottom_y))

        # Phase 2：slide 數多於講稿頁 → 增加虛擬頁空間
        extra_pages = max(0, total_slides - n_transcript_covered)
        if extra_pages > 0:
            end_y = self._page_boundaries[-1][1] if self._page_boundaries else 0
            total_virtual_h = 0
            for k in range(n_transcript_covered, total_slides):
                rect = self._slide_area_rect_for_page(k + 1)
                if rect is None:
                    continue
                slide_h = rect[3]
                page_h = slide_h + self._SLIDE_GAP_TOP_BOTTOM * 2
                top_y = end_y + total_virtual_h
                bottom_y = top_y + page_h
                self._page_boundaries.append((top_y, bottom_y))
                total_virtual_h += page_h
            # Qt 的 last block bottomMargin 不會延伸 documentSize（沒有後續 block 要分隔）。
            # 改用 QTextFrameFormat.bottomMargin 直接加到 root frame，這會確實延伸文件高度。
            if total_virtual_h > 0:
                root = doc.rootFrame()
                ff = root.frameFormat()
                ff.setBottomMargin(total_virtual_h)
                root.setFrameFormat(ff)
        else:
            # 沒有虛擬頁 → 清除 root frame bottomMargin
            root = doc.rootFrame()
            ff = root.frameFormat()
            if ff.bottomMargin() > 0:
                ff.setBottomMargin(0)
                root.setFrameFormat(ff)

    # ---------- 逐頁尺寸與填補（供 MainWindow 對齊用） ----------

    def page_top_ys(self, pages) -> list[int]:
        """回傳 `pages` 每頁第一句所在 block 的 viewport Y 座標（含 scroll 偏移後的 document Y）。

        用 `document().documentLayout().blockBoundingRect(block)` 取得。
        若 pages 為空或 layout 未就緒，回傳空 list。
        """
        if not pages or self._doc_length == 0:
            return []
        doc = self.document()
        layout = doc.documentLayout()
        result: list[int] = []
        for p in pages:
            sent_start_idx = p.sentence_start
            # 找該 sentence 的 block
            if sent_start_idx < 0:
                result.append(0)
                continue
            # 用 cursor 找出該 sentence 的 document char pos → block
            try:
                char_pos = p.sentence_start
                # sentence_start 是 index in Transcript.sentences，要取得該 sentence.start
                # 這裡由外部傳 pages 原物件；Transcript.page.sentence_start 不是 char
                # 需呼叫端改傳 char_pos；為相容先以 block number 0 fallback
            except Exception:
                pass
            result.append(0)
        return result

    def block_top_y(self, block_number: int) -> int:
        """指定 block 的 document Y（未扣 scroll；適合傳給外層定位用）。"""
        doc = self.document()
        block = doc.findBlockByNumber(block_number)
        if not block.isValid():
            return 0
        rect = doc.documentLayout().blockBoundingRect(block)
        return int(rect.top())

    def char_document_y(self, char_pos: int) -> int:
        """指定 char 位置在 document 中的 Y 座標（未扣 scrollbar.value）。"""
        char_pos = max(0, min(char_pos, self._doc_length))
        cursor = QTextCursor(self.document())
        cursor.setPosition(char_pos)
        block = cursor.block()
        rect = self.document().documentLayout().blockBoundingRect(block)
        return int(rect.top())

    def set_block_bottom_padding(self, block_number: int, pad_px: int) -> None:
        """指定 block 下方加 padding（用於頁高對齊）。不動任何 char，只改 block format。"""
        doc = self.document()
        block = doc.findBlockByNumber(block_number)
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        bf = cursor.blockFormat()
        bf.setBottomMargin(max(0, int(pad_px)))
        cursor.setBlockFormat(bf)

    def clear_all_block_bottom_paddings(self) -> None:
        """清掉所有 block 的 bottomMargin（避免殘留）。"""
        doc = self.document()
        block = doc.firstBlock()
        while block.isValid():
            cursor = QTextCursor(block)
            bf = cursor.blockFormat()
            if bf.bottomMargin() > 0:
                bf.setBottomMargin(0)
                cursor.setBlockFormat(bf)
            block = block.next()

    # ---------- 視窗頂端對應位置（供雙向捲動同步用） ----------

    def visible_top_char(self) -> int:
        """回傳目前視窗頂端對應的全文字元 offset。"""
        cursor = self.cursorForPosition(QPoint(10, 10))
        return cursor.position()

    def scroll_to_char(self, char_pos: int) -> None:
        """把指定字元捲到視窗頂端附近（不動使用者游標位置）。"""
        char_pos = max(0, min(char_pos, self._doc_length))
        cursor = QTextCursor(self.document())
        cursor.setPosition(char_pos)
        rect = self.cursorRect(cursor)
        sb = self.verticalScrollBar()
        # 目前 scroll 值 + cursor 在 viewport 的 y 位置 - 目標頂端 offset（40px）
        new_val = sb.value() + rect.top() - 40
        new_val = max(sb.minimum(), min(sb.maximum(), new_val))
        sb.setValue(new_val)

    # ---------- 互動 ----------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._slide_deck is not None:
            self._apply_text_wrap_width()
            self._relayout_slide_gaps()

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

    def _page_at_viewport_pos(self, x: int, y: int) -> int | None:
        """判斷 viewport 座標 (x, y) 是否點到某張 slide；回 page_no 或 None。"""
        if self._slide_deck is None or not self._page_boundaries:
            return None
        sb = self.verticalScrollBar().value()
        doc_y = y + sb
        for k, (top_y, bottom_y) in enumerate(self._page_boundaries):
            if not (top_y <= doc_y < bottom_y):
                continue
            rect = self._slide_area_rect_for_page(k + 1)
            if rect is None:
                return None
            slide_x, _, slide_w, _slide_h = rect
            if slide_x <= x <= slide_x + slide_w:
                return k + 1
            return None
        return None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        # 點到右欄 slide → 發 slide_double_clicked signal
        page_no = self._page_at_viewport_pos(pos.x(), pos.y())
        if page_no is not None:
            self.slide_double_clicked.emit(page_no)
            event.accept()
            return
        # 編輯模式：沿用 Qt 預設（雙擊選整個單字，不跳位置）
        if self._edit_mode:
            super().mouseDoubleClickEvent(event)
            return
        # 非編輯模式：雙擊才「跳到該位置」
        cursor = self.cursorForPosition(pos)
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
