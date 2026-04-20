"""PageDividerOverlay：覆蓋在左右兩側 content_splitter 上方的透明 widget。

功能：
- 在左右兩側某個 Y 畫一條 2px 橫線（貫穿整個 content_splitter）
- 線正中間放標籤 `── 第 N / M 頁 ──`
- 滑鼠事件完全穿透（`WA_TransparentForMouseEvents`）

由 MainWindow 控制：
- `set_boundaries(viewport_ys)` 告訴 overlay 每頁邊界位置（已扣 scroll offset 的 viewport Y）
- 當外層 scrollbar 捲動時，呼叫 `update()` 重繪
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget


class PageDividerOverlay(QWidget):
    """半透明 overlay，負責畫頁邊界橫線。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._boundaries: list[tuple[int, int, int]] = []
        # tuple = (viewport_y, page_no, total_pages)

    def set_boundaries(self, boundaries: list[tuple[int, int, int]]) -> None:
        """設定要畫的邊界列表；每項 (viewport_y, page_no, total_pages)。"""
        self._boundaries = list(boundaries)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._boundaries:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        pen = QPen(QColor("#555"))
        pen.setWidth(2)
        painter.setPen(pen)

        font = QFont()
        font.setPointSize(10)
        painter.setFont(font)
        fm = QFontMetrics(font)

        w = self.width()
        for y, page_no, total in self._boundaries:
            if y < -10 or y > self.height() + 10:
                continue
            # 橫線
            painter.setPen(pen)
            painter.drawLine(24, y, w - 24, y)
            # 中央文字
            text = f"──  第 {page_no} / {total} 頁  ──"
            tw = fm.horizontalAdvance(text)
            th = fm.height()
            tx = (w - tw) // 2
            ty = y - th // 2 + fm.ascent()
            # 先畫背景小塊遮掉橫線
            painter.fillRect(tx - 10, y - th // 2 - 2,
                             tw + 20, th + 4, QColor("#1E1E1E"))
            painter.setPen(QColor("#80D8FF"))
            painter.drawText(tx, ty, text)
        painter.end()
