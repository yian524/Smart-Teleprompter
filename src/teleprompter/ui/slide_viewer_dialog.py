"""投影片放大檢視對話框：滑鼠滾輪縮放 + 拖曳 + 左右鍵換頁。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent, QWheelEvent
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class _ZoomableView(QGraphicsView):
    """可用滾輪縮放、左鍵拖曳的 QGraphicsView。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setRenderHint(self.renderHints().Antialiasing | self.renderHints().SmoothPixmapTransform)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        self.scale(factor, factor)


class SlideViewerDialog(QDialog):
    """投影片大圖檢視。"""

    def __init__(self, deck, page_no: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"投影片檢視 — 第 {page_no} / {deck.page_count} 頁")
        self.setModal(True)
        self._deck = deck
        self._page_no = max(1, min(page_no, deck.page_count))

        # 讓對話框大約佔 parent 的 80% 大小
        if parent is not None:
            psize = parent.size()
            self.resize(int(psize.width() * 0.8), int(psize.height() * 0.85))
        else:
            self.resize(1200, 800)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 頂部工具列
        top_bar = QWidget()
        top_bar.setFixedHeight(40)
        top_bar.setStyleSheet(
            "background-color: #252525;"
            " color: #F0F0F0;"
        )
        tb = QHBoxLayout(top_bar)
        tb.setContentsMargins(12, 4, 12, 4)

        self._prev_btn = QPushButton("← 上一頁")
        self._prev_btn.clicked.connect(lambda: self._change_page(self._page_no - 1))
        tb.addWidget(self._prev_btn)

        self._page_label = QLabel()
        self._page_label.setStyleSheet("color: #80D8FF; font-size: 14px; font-weight: 600;")
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tb.addWidget(self._page_label, 1)

        self._next_btn = QPushButton("下一頁 →")
        self._next_btn.clicked.connect(lambda: self._change_page(self._page_no + 1))
        tb.addWidget(self._next_btn)

        self._fit_btn = QPushButton("🔍 重設縮放")
        self._fit_btn.clicked.connect(self._fit_to_view)
        tb.addWidget(self._fit_btn)

        self._close_btn = QPushButton("✕ 關閉")
        self._close_btn.clicked.connect(self.reject)
        tb.addWidget(self._close_btn)

        layout.addWidget(top_bar)

        # 主檢視區
        self._scene = QGraphicsScene(self)
        self._view = _ZoomableView(self)
        self._view.setScene(self._scene)
        self._view.setStyleSheet("background-color: #1A1A1A; border: none;")
        layout.addWidget(self._view, 1)

        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._load_page(self._page_no)

    def _load_page(self, page_no: int) -> None:
        if page_no < 1 or page_no > self._deck.page_count:
            return
        self._page_no = page_no
        # 用目前 view 寬度渲染高解析圖
        target_w = max(1200, int(self._view.viewport().width() * 1.2))
        pix = self._deck.render(page_no, target_w)
        self._scene.clear()
        self._pixmap_item = None
        if pix is None or pix.isNull():
            return
        self._pixmap_item = self._scene.addPixmap(pix)
        self._scene.setSceneRect(pix.rect())
        self._fit_to_view()
        self._page_label.setText(f"第 {page_no} / {self._deck.page_count} 頁")
        self._prev_btn.setEnabled(page_no > 1)
        self._next_btn.setEnabled(page_no < self._deck.page_count)
        self.setWindowTitle(f"投影片檢視 — 第 {page_no} / {self._deck.page_count} 頁")

    def _fit_to_view(self) -> None:
        if self._pixmap_item is not None:
            self._view.resetTransform()
            self._view.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)

    def _change_page(self, page_no: int) -> None:
        page_no = max(1, min(page_no, self._deck.page_count))
        if page_no != self._page_no:
            self._load_page(page_no)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_PageUp):
            self._change_page(self._page_no - 1)
            return
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_PageDown, Qt.Key.Key_Space):
            self._change_page(self._page_no + 1)
            return
        if key == Qt.Key.Key_Escape:
            self.reject()
            return
        if key == Qt.Key.Key_0:
            self._fit_to_view()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 視窗調整時 fit-to-view（僅首次，後續保留使用者縮放）
        pass
