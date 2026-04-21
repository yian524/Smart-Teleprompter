"""SlidePreviewPanel：右側投影片預覽（垂直捲動全頁面）。

布局：
 ┌─────────────────────────────┐
 │  fb260406202648.pdf   1/35  │  top bar
 ├─────────────────────────────┤
 │  ──── 第 1 / 35 頁 ────     │
 │                             │
 │       [ 頁 1 大圖 ]          │
 │                             │
 │  ──── 第 2 / 35 頁 ────     │
 │                             │
 │       [ 頁 2 大圖 ]          │
 │       ...                    │
 └─────────────────────────────┘

同步設計：
- 使用者**捲動**本面板 → 偵測目前頂端顯示哪一頁 → emit page_changed(page_no)
- 呼叫 `scroll_to_page(n)` → 捲到第 n 頁頂端（由 MainWindow 接收左側講稿訊號）
- `_programmatic_scroll` 旗標避免 ping-pong loop
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.pdf_renderer import SlideDeck


class SlidePreviewPanel(QWidget):
    """投影片預覽面板（所有頁垂直列出，可捲動）。"""

    page_changed = Signal(int)  # 使用者捲動導致目前可見頁變更（1-based）
    page_requested = Signal(int)  # 點縮圖要求跳到某頁（由 MainWindow 接）
    page_navigate_requested = Signal(int)  # 左右方向鍵 → ±1（聚焦在縮圖列時發）
    collapse_requested = Signal(bool)       # True=收合、False=展開

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._deck: SlideDeck | None = None
        self._page_headers: list[QLabel] = []   # 每頁 header 的位置用來偵測捲動對應哪頁
        self._page_images: list[QLabel] = []    # 每頁大圖 label（依需要補 pixmap）
        self._current_page: int = 0
        self._programmatic_scroll: bool = False
        self._reset_guard_timer = QTimer(self)
        self._reset_guard_timer.setSingleShot(True)
        self._reset_guard_timer.timeout.connect(self._clear_guard)

        self.setStyleSheet(
            "SlidePreviewPanel { background-color: #2A2A2A; }"
            " QLabel#Placeholder { color: #888; font-size: 14px; }"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 頂部資訊 bar
        top_bar = QWidget()
        top_bar.setFixedHeight(32)
        top_bar.setStyleSheet(
            "background-color: #252525; border-bottom: 1px solid #3A3A3A;"
        )
        tb_layout = QHBoxLayout(top_bar)
        tb_layout.setContentsMargins(8, 4, 8, 4)
        self.title_label = QLabel("尚未載入投影片")
        self.title_label.setStyleSheet("color: #80D8FF; font-size: 13px;")
        tb_layout.addWidget(self.title_label)
        tb_layout.addStretch(1)
        self.page_label = QLabel("—")
        self.page_label.setStyleSheet("color: #CCCCCC; font-size: 13px;")
        tb_layout.addWidget(self.page_label)
        # 收合按鈕（◀）：收合後 MainWindow 把 panel 寬度設為 0
        self.btn_collapse = QToolButton()
        self.btn_collapse.setText("◀")
        self.btn_collapse.setToolTip("收合縮圖列")
        self.btn_collapse.setStyleSheet(
            "QToolButton { color: #80D8FF; font-size: 14px; border: none; padding: 2px 6px; }"
            "QToolButton:hover { background: #3A3A3A; border-radius: 3px; }"
        )
        self.btn_collapse.clicked.connect(lambda: self.collapse_requested.emit(True))
        tb_layout.addWidget(self.btn_collapse)
        outer.addWidget(top_bar)
        # 允許鍵盤 focus 讓左右方向鍵被 keyPressEvent 收到
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # 捲動區（視覺捲軸隱藏，只保留內部滾動功能讓滑鼠滾輪依舊可用）
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet(
            "QScrollArea { background-color: #2A2A2A; border: none; }"
            " QScrollBar:vertical { width: 0px; background: transparent; }"
        )
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)
        outer.addWidget(self.scroll, 1)

        # 內層容器
        self.container = QWidget()
        self.col = QVBoxLayout(self.container)
        self.col.setContentsMargins(8, 8, 8, 8)
        self.col.setSpacing(10)
        self.container.setStyleSheet("background-color: #2A2A2A;")
        self.scroll.setWidget(self.container)

        # 預設 placeholder（明確的拖拉提示 + 邊框）
        self._placeholder = QLabel(
            "📥  把 PDF / PPTX 拖到這裡\n\n"
            "或點左上工具列「🖼 載入投影片」\n\n"
            "支援 .pdf .pptx .ppt"
        )
        self._placeholder.setObjectName("Placeholder")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet(
            "QLabel#Placeholder { color: #888; font-size: 14px;"
            "  border: 2px dashed #444; border-radius: 8px;"
            "  padding: 40px; margin: 20px; }"
        )
        self.col.addWidget(self._placeholder, 1)

    # ---------- 公開 API ----------

    def set_deck(self, deck: SlideDeck | None, title: str = "") -> None:
        """載入新投影片；None = 清空。

        效能：只建立 QLabel 佔位（依 PDF 原始寬高比預留尺寸），**不立即渲染**。
        真正的 pixmap 由 `_render_visible_pages()` 在 scroll 事件中按需渲染。
        """
        self._deck = deck
        self._page_headers = []
        self._page_images = []
        self._page_spacers: list[QWidget] = []   # 每頁下方 padding spacer（對齊用）
        self._rendered_widths: dict[int, int] = {}
        self._current_page = 0
        self._clear_container()

        if deck is None:
            self.title_label.setText("尚未載入投影片")
            self.page_label.setText("—")
            self.col.addWidget(self._placeholder, 1)
            self._placeholder.show()
            return

        self._placeholder.hide()
        self.title_label.setText(title or "投影片")
        self.page_label.setText(f"1 / {deck.page_count}")

        target_width = max(80, self.scroll.viewport().width() - 40)
        for i, page in enumerate(deck.pages, start=1):
            # 每頁上方小標籤「第 N 頁」
            header = QLabel(f"第 {i} 頁")
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.setStyleSheet(
                "color: #80D8FF; font-size: 11px; font-weight: 600;"
                " padding: 2px 0; margin: 4px 0 2px 0;"
            )
            self.col.addWidget(header, 0, Qt.AlignmentFlag.AlignHCenter)
            self._page_headers.append(header)

            img = QLabel("載入中…")
            img.setAlignment(Qt.AlignmentFlag.AlignCenter)
            img.setSizePolicy(QSizePolicy.Policy.Fixed,
                              QSizePolicy.Policy.Fixed)
            img.setStyleSheet(
                "background-color: transparent; border: 1px solid #3A3A3A;"
                " border-radius: 4px; color: #555;"
            )
            img.setCursor(Qt.CursorShape.PointingHandCursor)
            # 點擊縮圖 → 跳到該頁（page_requested signal）
            img.mousePressEvent = (  # type: ignore[method-assign]
                lambda ev, pn=i: self._on_thumbnail_clicked(pn)
            )
            # 依 PDF 原始寬高比預留空間，避免 scroll 過程中尺寸跳動
            if page.width_pt > 0:
                aspect = page.height_pt / page.width_pt
            else:
                aspect = 1.414  # A4 fallback
            img.setFixedSize(target_width, int(target_width * aspect))
            self.col.addWidget(img, 0, Qt.AlignmentFlag.AlignHCenter)
            self._page_images.append(img)

            # 每頁下方 spacer（對齊填補用，目前保留接口）
            spacer = QWidget()
            spacer.setFixedHeight(0)
            self.col.addWidget(spacer)
            self._page_spacers.append(spacer)

        # 捲到頂
        self._programmatic_scroll = True
        self.scroll.verticalScrollBar().setValue(0)
        self._reset_guard_timer.start(120)
        self._current_page = 1
        # 首批渲染（視窗內 + 前兩頁緩衝）
        QTimer.singleShot(30, self._render_visible_pages)

    def _on_thumbnail_clicked(self, page_no: int) -> None:
        """縮圖被點 → 發 page_requested 訊號 + 搶 focus 讓方向鍵能生效。"""
        if self._deck is None or not (1 <= page_no <= self._deck.page_count):
            return
        self._current_page = page_no
        self.page_label.setText(f"{page_no} / {self._deck.page_count}")
        self._highlight_active_thumbnail()
        self.setFocus()   # 讓後續方向鍵能被這個 panel 收到
        self.page_requested.emit(page_no)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """面板聚焦時左右方向鍵 → 發 page_navigate_requested(±1)。"""
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

    def mousePressEvent(self, event) -> None:
        # 點在面板空白處也能搶 focus
        self.setFocus()
        super().mousePressEvent(event)

    def _highlight_active_thumbnail(self) -> None:
        """把 self._current_page 對應的縮圖加亮框，其他的恢復普通框。"""
        for i, img in enumerate(self._page_images, start=1):
            if i == self._current_page:
                img.setStyleSheet(
                    "background-color: transparent; border: 2px solid #4CAF50;"
                    " border-radius: 4px; color: #555;"
                )
            else:
                img.setStyleSheet(
                    "background-color: transparent; border: 1px solid #3A3A3A;"
                    " border-radius: 4px; color: #555;"
                )

    def scroll_to_page(self, page_no: int) -> None:
        """把指定頁的 header 捲到視窗頂端（由 MainWindow 從左側講稿同步觸發）。"""
        if self._deck is None:
            return
        if page_no < 1 or page_no > len(self._page_headers):
            return
        if page_no == self._current_page:
            return
        header = self._page_headers[page_no - 1]
        # 轉成 container 座標
        y = header.y()
        self._programmatic_scroll = True
        self.scroll.verticalScrollBar().setValue(max(0, y - 10))
        self._current_page = page_no
        self.page_label.setText(f"{page_no} / {self._deck.page_count}")
        self._highlight_active_thumbnail()
        self._reset_guard_timer.start(120)
        # 確保目標頁已渲染
        self._render_visible_pages()

    def page_top_ys(self) -> list[int]:
        """每個 page header 在 container 中的 Y 座標（1-based → list[i-1]）。"""
        return [hdr.y() for hdr in self._page_headers]

    def page_natural_heights(self) -> list[int]:
        """每頁自然高度 = page header + image height（不含 spacer）。用於對齊計算。"""
        result: list[int] = []
        for i, img in enumerate(self._page_images):
            # 單頁佔用 = header 高 + spacing + image 高
            header_h = self._page_headers[i].sizeHint().height()
            img_h = img.minimumHeight()
            result.append(header_h + img_h + 18)  # 18 = layout spacing
        return result

    def set_page_bottom_paddings(self, pads: list[int]) -> None:
        """對每頁下方 spacer 設定 padding 高度（達成頁高對齊）。"""
        for i, pad in enumerate(pads):
            if i >= len(self._page_spacers):
                break
            self._page_spacers[i].setFixedHeight(max(0, int(pad)))

    def scroll_area(self):
        """外部需要取得內部 scrollbar 做同步。"""
        return self.scroll

    # 舊 API 保留（無副作用）
    def show_page(self, page_no: int) -> None:
        self.scroll_to_page(page_no)

    def current_page(self) -> int:
        return self._current_page

    def page_count(self) -> int:
        return self._deck.page_count if self._deck else 0

    # ---------- 內部 ----------

    def _clear_container(self) -> None:
        """移除 container 中所有 children（保留 layout 本身）。"""
        while self.col.count():
            item = self.col.takeAt(0)
            w = item.widget()
            if w is self._placeholder:
                # placeholder 僅 hide，不 delete（set_deck(None) 時還會用到）
                continue
            if w is not None:
                w.deleteLater()

    def _on_scroll(self, value: int) -> None:
        """使用者手動捲動 → 判斷目前頂端對應哪一頁 → emit page_changed。
        同時按需渲染視窗附近的頁面。"""
        # 不論程式/人為捲動，都要做 lazy 渲染
        self._render_visible_pages()
        if self._programmatic_scroll or not self._page_headers:
            return
        anchor_y = value + 80
        best_page = 1
        for i, hdr in enumerate(self._page_headers):
            if hdr.y() <= anchor_y:
                best_page = i + 1
            else:
                break
        if best_page != self._current_page:
            self._current_page = best_page
            self.page_label.setText(f"{best_page} / {self._deck.page_count}")
            self.page_changed.emit(best_page)

    def _render_visible_pages(self) -> None:
        """只渲染目前視窗內（加 400px 緩衝）的頁面，加速載入與捲動。"""
        if self._deck is None or not self._page_images:
            return
        target_w = max(80, self.scroll.viewport().width() - 40)
        dpr = self.devicePixelRatioF() or 1.0
        sb = self.scroll.verticalScrollBar()
        top = sb.value()
        bottom = top + self.scroll.viewport().height()
        margin = 400
        for i, img in enumerate(self._page_images):
            y = img.y()
            h = img.height()
            if y + h < top - margin:
                continue
            if y > bottom + margin:
                break
            # 已按當前寬度 + DPR 渲染過 → 跳過
            if self._rendered_widths.get(i + 1) == (target_w, round(dpr, 2)):
                continue
            pix = self._deck.render(i + 1, target_w, dpr)
            if pix is not None:
                img.setText("")
                img.setPixmap(pix)
                # 邏輯像素尺寸（pix.size() 是物理像素；要除以 DPR 才是 label 該佔的版面大小）
                d = pix.devicePixelRatio() or 1.0
                from PySide6.QtCore import QSize
                img.setFixedSize(QSize(
                    int(round(pix.width() / d)),
                    int(round(pix.height() / d)),
                ))
                self._rendered_widths[i + 1] = (target_w, round(dpr, 2))

    def _clear_guard(self) -> None:
        self._programmatic_scroll = False

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._deck is None or not self._page_images:
            return
        # debounce：寬度變動 300ms 後才重新渲染視窗內頁面（避免拖拉過程卡）
        if not hasattr(self, "_resize_timer"):
            self._resize_timer = QTimer(self)
            self._resize_timer.setSingleShot(True)
            self._resize_timer.timeout.connect(self._on_resize_done)
        self._resize_timer.start(300)

    def _on_resize_done(self) -> None:
        """寬度改變完畢 → 清掉舊渲染標記，lazy 重新渲染視窗內頁面。"""
        self._rendered_widths.clear()
        # 先用新尺寸修佔位尺寸（避免 scroll 位置錯亂）
        if self._deck is not None:
            target_w = max(80, self.scroll.viewport().width() - 20)
            for i, img in enumerate(self._page_images):
                page = self._deck.pages[i]
                if page.width_pt > 0:
                    aspect = page.height_pt / page.width_pt
                else:
                    aspect = 1.414
                img.setFixedSize(target_w, int(target_w * aspect))
        self._render_visible_pages()
