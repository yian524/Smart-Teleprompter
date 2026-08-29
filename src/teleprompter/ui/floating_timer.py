"""懸浮計時視窗：切到其他程式（投影片、瀏覽器）時仍看得到剩餘時間。

- 無邊框、永遠置頂，不搶焦點（不會打斷你正在操作的視窗）
- 可用滑鼠拖曳到任一角落；位置記在設定裡，下次開啟回到原處
- 顏色跟主計時器同一套（正常／接近／超時），一眼判斷還剩多少
- 雙擊可切換「只看剩餘」與「已用／目標都看」兩種顯示密度
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..core.timer_controller import format_mmss


class FloatingTimer(QWidget):
    """置頂的小型計時顯示。"""

    closed = Signal()
    moved = Signal(int, int)   # 拖曳結束後回報位置，供設定保存

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        # Tool + FramelessWindowHint：不進工作列、無邊框
        # WindowDoesNotAcceptFocus：點它不會把焦點從簡報軟體搶走
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("floatingTimer")
        self._drag_from: QPoint | None = None
        self._compact = True

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(2)

        self.main_label = QLabel("--:--")
        self.main_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sub_label = QLabel("尚未開始")
        self.sub_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.main_label)
        lay.addWidget(self.sub_label)

        self._apply_style("#d6d9de")
        self.setMinimumWidth(150)

    # ---------- 外觀 ----------

    def _apply_style(self, color: str) -> None:
        self.setStyleSheet(
            "QWidget#floatingTimer {"
            "  background-color: rgba(23, 25, 28, 235);"
            "  border: 1px solid #383c42; border-radius: 8px;"
            "}"
        )
        self.main_label.setStyleSheet(
            f"color: {color}; font-size: 30px; font-weight: 700;"
            " font-family: 'Segoe UI', 'Noto Sans TC', sans-serif;"
        )
        self.sub_label.setStyleSheet("color: #8f959e; font-size: 11px;")

    # ---------- 資料 ----------

    def update_state(self, state) -> None:
        """接 TimerController.state_changed。"""
        color = getattr(state.time_color, "value", "#d6d9de")
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
                if not self._compact else "剩餘時間"
            )
        self._apply_style(color)
        self.adjustSize()

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
            self.moved.emit(self.x(), self.y())

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: D102, ARG002
        """雙擊切換顯示密度（只看剩餘 ↔ 連已用/目標一起看）。"""
        self._compact = not self._compact

    def closeEvent(self, event) -> None:  # noqa: D102
        self.closed.emit()
        super().closeEvent(event)
