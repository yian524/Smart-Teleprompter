"""懸浮計時視窗：可跟著念稿走，也可以完全獨立當一個計時器用。

給站在台上的人用，所以優先考慮「餘光瞄一眼就讀得到」：

- **字級跟著視窗大小走**：拖右下角把視窗拉大，數字就跟著變大
- **高對比**：深色底 + 狀態色外框，貼在任何桌布或投影片上都看得清楚
- **可獨立計時**：自己輸入分鐘、選倒數或正數、按開始／暫停，
  完全不必啟動語音辨識——只想單純計時的場合直接用這個就好
- **也能跟著主程式**：主程式按「開始」念稿時，這裡同步顯示念稿計時
- 無邊框、永遠置頂、不搶焦點；位置與尺寸記在設定裡
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizeGrip,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.timer_controller import TimerController, format_mmss

MIN_SIZE = QSize(200, 128)
MAX_SIZE = QSize(1200, 640)
DEFAULT_SIZE = QSize(360, 200)

MODE_FOLLOW = "follow"      # 跟著主程式念稿計時
MODE_STANDALONE = "solo"    # 自己獨立計時


class FloatingTimer(QWidget):
    """置頂的計時顯示，可自由縮放，也可獨立計時。"""

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
        # 圓角外要真的透明 → 背景與外框自己畫（見 paintEvent）；
        # 純靠 stylesheet 在 WA_TranslucentBackground 的 QWidget 上不會生效
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("floatingTimer")
        self.setMinimumSize(MIN_SIZE)
        self.setMaximumSize(MAX_SIZE)

        self._drag_from: QPoint | None = None
        self._color = "#9E9E9E"
        self._mode = MODE_FOLLOW
        self._count_up = False       # False = 倒數；True = 從 0 往上數

        # 自己的計時器：獨立模式用，不影響主程式的念稿計時
        self.solo_timer = TimerController(target_sec=900, parent=self)
        self.solo_timer.state_changed.connect(self._on_solo_state)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 10, 16, 6)
        root.setSpacing(2)

        self.main_label = QLabel("--:--")
        self.main_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.main_label.setMinimumHeight(1)
        root.addWidget(self.main_label, 1)

        self.sub_label = QLabel("跟著念稿計時")
        self.sub_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.sub_label)

        root.addWidget(self._build_controls())

        self.resize(DEFAULT_SIZE)
        self._restyle()

    # ---------- 控制列 ----------

    def _build_controls(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("ftControls")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(4)

        self.spin_minutes = QSpinBox(bar)
        self.spin_minutes.setRange(1, 300)
        self.spin_minutes.setValue(15)
        self.spin_minutes.setSuffix(" 分")
        self.spin_minutes.setFixedWidth(74)
        self.spin_minutes.setToolTip("這個計時器的目標時間（不影響主程式的念稿設定）")
        self.spin_minutes.valueChanged.connect(self._on_minutes_changed)
        lay.addWidget(self.spin_minutes)

        self.btn_mode = QPushButton("倒數", bar)
        self.btn_mode.setCheckable(True)
        self.btn_mode.setFixedWidth(50)
        self.btn_mode.setToolTip("切換「倒數剩餘」與「從 0 正數」")
        self.btn_mode.clicked.connect(self._toggle_count_direction)
        lay.addWidget(self.btn_mode)

        lay.addStretch(1)

        self.btn_run = QPushButton("開始", bar)
        self.btn_run.setFixedWidth(56)
        self.btn_run.setToolTip("只計時，不會啟動語音辨識")
        self.btn_run.clicked.connect(self.toggle_solo_run)
        lay.addWidget(self.btn_run)

        self.btn_reset = QPushButton("歸零", bar)
        self.btn_reset.setFixedWidth(50)
        self.btn_reset.clicked.connect(self.reset_solo)
        lay.addWidget(self.btn_reset)

        self.size_grip = QSizeGrip(bar)
        self.size_grip.setFixedSize(14, 14)
        lay.addWidget(self.size_grip, 0, Qt.AlignmentFlag.AlignBottom)

        self.controls = bar
        return bar

    # ---------- 獨立計時 ----------

    def toggle_solo_run(self) -> None:
        """開始／暫停自己的計時（切到獨立模式，完全不動主程式）。"""
        self._mode = MODE_STANDALONE
        if self.solo_timer.is_running():
            self.solo_timer.pause()
        else:
            self.solo_timer.set_target_seconds(
                0 if self._count_up else self.spin_minutes.value() * 60
            )
            self.solo_timer.start()
        self._sync_run_button()
        self._refresh_idle_display()

    def reset_solo(self) -> None:
        """歸零並停下（維持在獨立模式，顯示回到預備值）。"""
        self._mode = MODE_STANDALONE
        self.solo_timer.reset()
        self._sync_run_button()
        self._refresh_idle_display()

    def _toggle_count_direction(self) -> None:
        """倒數 ↔ 正數。正數把目標設 0，計時器就只累加已用時間。"""
        self._count_up = self.btn_mode.isChecked()
        self.btn_mode.setText("正數" if self._count_up else "倒數")
        self.spin_minutes.setEnabled(not self._count_up)
        self.solo_timer.set_target_seconds(
            0 if self._count_up else self.spin_minutes.value() * 60
        )
        self._mode = MODE_STANDALONE
        self._refresh_idle_display()

    def _on_minutes_changed(self, minutes: int) -> None:
        if not self._count_up:
            self.solo_timer.set_target_seconds(minutes * 60)
            if not self.solo_timer.is_running():
                self._refresh_idle_display()

    def _sync_run_button(self) -> None:
        self.btn_run.setText("暫停" if self.solo_timer.is_running() else "開始")

    def _refresh_idle_display(self) -> None:
        """獨立模式停著時顯示預備值（例如 15:00），而不是 --:--。"""
        if self._mode != MODE_STANDALONE or self.solo_timer.is_running():
            return
        self._color = "#9E9E9E"
        if self._count_up:
            self.main_label.setText(format_mmss(self.solo_timer.elapsed_ms))
            self.sub_label.setText("獨立計時 · 正數")
        else:
            remaining = max(0, self.solo_timer.target_ms - self.solo_timer.elapsed_ms)
            self.main_label.setText(format_mmss(remaining))
            self.sub_label.setText("獨立計時 · 倒數")
        self._restyle()

    def _on_solo_state(self, state) -> None:
        if self._mode != MODE_STANDALONE:
            return
        self._render_state(state, tag="獨立計時")
        self._sync_run_button()

    # ---------- 跟隨主程式 ----------

    def update_state(self, state) -> None:
        """接主程式的念稿計時；使用者一旦自己操作過就不再被覆蓋。"""
        if self._mode == MODE_STANDALONE:
            return
        self._render_state(state, tag="念稿計時")

    def follow_main_timer(self) -> None:
        """切回「跟著念稿」模式（獨立計時會停下）。"""
        if self.solo_timer.is_running():
            self.solo_timer.pause()
        self._mode = MODE_FOLLOW
        self._sync_run_button()
        self.sub_label.setText("跟著念稿計時")

    # ---------- 顯示 ----------

    def _render_state(self, state, *, tag: str) -> None:
        self._color = getattr(state.time_color, "value", "#9E9E9E")
        if state.target_ms == 0:
            self.main_label.setText(format_mmss(state.elapsed_ms))
            self.sub_label.setText(f"{tag} · 正數")
        elif state.overrun_ms > 0:
            self.main_label.setText("+" + format_mmss(state.overrun_ms))
            self.sub_label.setText(f"{tag} · 已超時")
        else:
            self.main_label.setText(format_mmss(state.remaining_ms))
            self.sub_label.setText(
                f"{tag} · {format_mmss(state.elapsed_ms)} / {format_mmss(state.target_ms)}"
            )
        self._restyle()

    def _font_px(self) -> int:
        """依視窗大小推算數字字級（扣掉說明列與控制列佔的高度）。"""
        by_width = int(self.width() / 3.6)
        by_height = int(max(1, self.height() - 64) * 0.68)
        return max(20, min(by_width, by_height, 240))

    def _restyle(self) -> None:
        px = self._font_px()
        self.sub_label.setVisible(self.height() >= 150 and px >= 30)
        self.main_label.setStyleSheet(
            f"color: {self._color}; font-size: {px}px; font-weight: 700;"
            " font-family: 'Segoe UI', 'Noto Sans TC', sans-serif;"
            " letter-spacing: 1px; background: transparent;"
        )
        self.sub_label.setStyleSheet(
            f"color: #b9bec6; font-size: {max(11, int(px * 0.24))}px;"
            " background: transparent;"
        )
        self.controls.setStyleSheet(
            "QWidget#ftControls { background: transparent; }"
            "QPushButton { background:#22262b; color:#d6d9de; border:1px solid #3a3f46;"
            "  border-radius:4px; padding:3px 6px; font-size:11.5px; }"
            "QPushButton:hover { background:#2c3138; }"
            "QPushButton:checked { background:#2f6fed; border-color:#2f6fed; color:#fff; }"
            "QSpinBox { background:#1a1d21; color:#d6d9de; border:1px solid #3a3f46;"
            "  border-radius:4px; padding:2px 4px; font-size:11.5px; }"
            "QSpinBox:disabled { color:#5d636b; }"
        )

    def paintEvent(self, event) -> None:  # noqa: D102, ARG002
        """自繪深色圓角底 + 狀態色外框（貼在任何背景上都讀得到）。"""
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
        target = QSize(640, 340) if self.width() < 520 else DEFAULT_SIZE
        self.resize(target)
        self._emit_geometry()

    def _emit_geometry(self) -> None:
        g = self.geometry()
        self.geometry_changed.emit(g.x(), g.y(), g.width(), g.height())

    def closeEvent(self, event) -> None:  # noqa: D102
        if self.solo_timer.is_running():
            self.solo_timer.pause()
        self._emit_geometry()
        self.closed.emit()
        super().closeEvent(event)
