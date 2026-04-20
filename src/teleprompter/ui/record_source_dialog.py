"""錄影來源選擇對話框：本 app 主視窗 / 指定螢幕。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QScreen
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..core.video_encoder import CaptureSource, CaptureTarget


class RecordSourceDialog(QDialog):
    """讓使用者選錄影來源。"""

    def __init__(self, main_widget: QWidget, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("選擇錄影來源")
        self.setMinimumWidth(420)
        self._main_widget = main_widget
        self._result: Optional[CaptureTarget] = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        hint = QLabel("請選擇要錄製的畫面來源：")
        layout.addWidget(hint)

        self._group = QButtonGroup(self)

        # 本 App 主視窗
        self._rb_window = QRadioButton("🪟 本 App 主視窗（提詞機 + 投影片）")
        self._rb_window.setChecked(True)
        self._group.addButton(self._rb_window, 0)
        layout.addWidget(self._rb_window)

        # 所有螢幕（虛擬桌面拼接）— 只在多螢幕環境時顯示
        screens = QGuiApplication.screens()
        self._rb_all_screens: QRadioButton | None = None
        if len(screens) > 1:
            primary = QGuiApplication.primaryScreen()
            vgeo = primary.virtualGeometry() if primary else None
            if vgeo is not None:
                self._rb_all_screens = QRadioButton(
                    f"🖵 所有螢幕（虛擬桌面拼接 {vgeo.width()}×{vgeo.height()}）"
                )
                self._group.addButton(self._rb_all_screens, 1)
                layout.addWidget(self._rb_all_screens)

        # 各螢幕
        self._rb_screens: list[tuple[QRadioButton, QScreen]] = []
        for i, scr in enumerate(screens):
            geo = scr.geometry()
            label = f"🖥 螢幕 {i + 1} — {scr.name()} ({geo.width()}×{geo.height()})"
            rb = QRadioButton(label)
            self._group.addButton(rb, 100 + i)
            layout.addWidget(rb)
            self._rb_screens.append((rb, scr))

        tip = QLabel("💡 錄製中即使把 app 最小化，仍會繼續錄（會用上一張畫面補）。")
        tip.setStyleSheet("color: #888; font-size: 12px;")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("開始錄製")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        if self._rb_window.isChecked():
            self._result = CaptureTarget(
                source=CaptureSource.WIDGET,
                widget=self._main_widget,
            )
        elif self._rb_all_screens is not None and self._rb_all_screens.isChecked():
            self._result = CaptureTarget(source=CaptureSource.ALL_SCREENS)
        else:
            for rb, scr in self._rb_screens:
                if rb.isChecked():
                    self._result = CaptureTarget(
                        source=CaptureSource.SCREEN,
                        screen=scr,
                    )
                    break
        if self._result is not None:
            self.accept()

    def result_target(self) -> Optional[CaptureTarget]:
        return self._result
