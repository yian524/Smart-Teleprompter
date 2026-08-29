"""懸浮計時視窗：切到其他程式（投影片、瀏覽器）時仍看得到剩餘時間。

給站在台上的人用，所以優先考慮「餘光瞄一眼就讀得到」：

- **字級跟著視窗大小走**：右下角拖曳把視窗拉大，數字就跟著變大，不必進設定調
- **高對比**：近乎不透明的深底 + 外框，貼在任何桌布或投影片上都看得清楚
- **狀態靠顏色與外框**：正常／接近時限／超時，外框同步變色，遠看也分得出來
- 無邊框、永遠置頂、不搶焦點；位置與尺寸記在設定裡，下次開啟回到原樣
- 空間不夠時自動只留數字（說明文字先讓位給時間）
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizeGrip,
    QVBoxLayout,
    QWidget,
)

from ..core.timer_controller import format_mmss

# 視窗尺寸範圍：最小仍要讀得到，最大可佔滿一個角落
MIN_SIZE = QSize(180, 96)
MAX_SIZE = QSize(1200, 640)
DEFAULT_SIZE = QSize(340, 168)


class FloatingTimer(QWidget):
    """置頂的計時顯示，可自由縮放。"""

    closed = Signal()
    geometry_changed = Signal(int, int, int, int)   # x, y, w, h → 供設定保存

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        # 圓角外要真的透明 → 背景與外框自己畫（見 paintEvent）。
        # 純靠 stylesheet 在 WA_TranslucentBackground 的 QWidget 上不會生效，
        # 結果會是「字浮在桌布上」，淺色桌布幾乎讀不到。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("floatingTimer")
        self.setMinimumSize(MIN_SIZE)
        self.setMaximumSize(MAX_SIZE)
        self._drag_from: QPoint | None = None
        self._show_detail = True
        self._color = "#9E9E9E"

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 12, 18, 8)
        root.setSpacing(0)

        self.main_label = QLabel("--:--")
        self.main_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.main_label.setMinimumHeight(1)   # 允許被壓縮，字級由 resize 決定
        root.addWidget(self.main_label, 1)

        self.sub_label = QLabel("尚未開始")
        self.sub_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.sub_label)

        # 右下角縮放握把（Qt 內建，拖曳即可改變視窗大小）
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch(1)
        self.size_grip = QSizeGrip(self)
        self.size_grip.setFixedSize(16, 16)
        grip_row.addWidget(self.size_grip, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(grip_row)

        self.resize(DEFAULT_SIZE)
        self._restyle()

    # ---------- 外觀 ----------

    def _font_px(self) -> int:
        """依視窗大小推算數字字級：寬度與高度都不能讓數字被切掉。

        `mm:ss` 約 5 個字寬，取字級 ≈ 寬度 / 3.6；同時不超過可用高度的 6 成。
        """
        by_width = int(self.width() / 3.6)
        by_height = int(self.height() * 0.58)
        return max(22, min(by_width, by_height, 240))

    def _restyle(self) -> None:
        px = self._font_px()
        # 空間太小就把說明字讓給數字
        self._show_detail = self.height() >= 120 and px >= 34
        self.sub_label.setVisible(self._show_detail)

        self.main_label.setStyleSheet(
            f"color: {self._color}; font-size: {px}px; font-weight: 700;"
            " font-family: 'Segoe UI', 'Noto Sans TC', sans-serif;"
            " letter-spacing: 1px; background: transparent;"
        )
        self.sub_label.setStyleSheet(
            f"color: #b9bec6; font-size: {max(11, int(px * 0.26))}px;"
            " background: transparent;"
        )

    def paintEvent(self, event) -> None:  # noqa: D102, ARG002
        """自繪深色圓角底 + 狀態色外框（高對比，貼在任何背景上都讀得到）。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setBrush(QColor(12, 14, 17, 244))
        pen = QPen(QColor(self._color))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRoundedRect(rect, 10, 10)

    def resizeEvent(self, event) -> None:  # noqa: D102
        super().resizeEvent(event)
        self._restyle()

    # ---------- 資料 ----------

    def update_state(self, state) -> None:
        """接 TimerController.state_changed。"""
        self._color = getattr(state.time_color, "value", "#9E9E9E")
        if state.target_ms == 0:
            self.main_label.setText(format_mmss(state.elapsed_ms))
            self.sub_label.setText("已用時間（未設目標）")
        elif state.overrun_ms > 0:
            self.main_label.setText("+" + format_mmss(state.overrun_ms))
            self.sub_label.setText("已超時")
        else:
            self.main_label.setText(format_mmss(state.remaining_ms))
            self.sub_label.setText(
                f"剩餘　·　{format_mmss(state.elapsed_ms)} / {format_mmss(state.target_ms)}"
            )
        self._restyle()

    # ---------- 互動 ----------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: D102
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: D102
        if self._drag_from is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_from)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: D102, ARG002
        if self._drag_from is not None:
            self._drag_from = None
            self._emit_geometry()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: D102
        """滾輪縮放：不想拖角落時，滑鼠移上去滾一下就能放大縮小。"""
        step = 24 if event.angleDelta().y() > 0 else -24
        new_w = max(MIN_SIZE.width(), min(MAX_SIZE.width(), self.width() + step))
        ratio = self.height() / max(1, self.width())
        self.resize(new_w, int(new_w * ratio))
        self._emit_geometry()
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: D102, ARG002
        """雙擊在「預設大小」與「大字模式」之間切換。"""
        target = QSize(620, 300) if self.width() < 500 else DEFAULT_SIZE
        self.resize(target)
        self._emit_geometry()

    def _emit_geometry(self) -> None:
        g = self.geometry()
        self.geometry_changed.emit(g.x(), g.y(), g.width(), g.height())

    def closeEvent(self, event) -> None:  # noqa: D102
        self._emit_geometry()
        self.closed.emit()
        super().closeEvent(event)
