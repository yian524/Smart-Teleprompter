"""模組開關（工具列右段的 switch）。

視覺規格與 design/toolbar_mockup_v2.html 一致：26×15 軌道 + 11px 圓形滑塊，
開啟時軌道轉品牌藍、滑塊移到右側，右方接一個文字標籤。

行為上就是一個 QCheckBox（可用 setChecked / isChecked / toggled），
只覆寫繪製與尺寸；文字由外部 QLabel 提供，避免與滑塊爭空間。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QWidget

ACCENT = "#2f6fed"
TRACK_OFF = "#3a3e44"
KNOB_OFF = "#7c828b"


class ModuleSwitch(QCheckBox):
    """手機式滑動開關（純繪製，無文字）。"""

    _W, _H, _PAD = 26, 15, 2

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self._W, self._H)

    def sizeHint(self) -> QSize:  # noqa: D102
        return QSize(self._W, self._H)

    def paintEvent(self, event) -> None:  # noqa: D102, ARG002
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(ACCENT if on else TRACK_OFF))
        p.drawRoundedRect(0, 0, self._W, self._H, self._H / 2, self._H / 2)
        d = self._H - self._PAD * 2
        x = self._W - d - self._PAD if on else self._PAD
        p.setBrush(QColor("#ffffff" if on else KNOB_OFF))
        p.drawEllipse(x, self._PAD, d, d)


class ModuleToggle(QWidget):
    """開關 + 標籤的組合，直接放進工具列用。

    對外只暴露需要的介面：`switch`（ModuleSwitch）、`toggled`（訊號）、
    `setChecked` / `isChecked` 代理，讓呼叫端不必知道內部結構。
    """

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 4, 0)
        lay.setSpacing(7)
        self.switch = ModuleSwitch(self)
        self.label = QLabel(text, self)
        self.label.setObjectName("moduleLabel")
        lay.addWidget(self.switch)
        lay.addWidget(self.label)
        self.toggled = self.switch.toggled
        # 點文字也能切換（放大點擊區）
        self.label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.label.mousePressEvent = self._label_clicked  # type: ignore[method-assign]
        self._sync_label()
        self.switch.toggled.connect(lambda _: self._sync_label())

    def _label_clicked(self, event) -> None:  # noqa: ARG002
        self.switch.toggle()

    def _sync_label(self) -> None:
        on = self.switch.isChecked()
        self.label.setStyleSheet(
            f"color: {'#ffffff' if on else '#8f959e'}; font-size: 12.5px;"
        )

    # ---- 代理 ----
    def setChecked(self, on: bool) -> None:  # noqa: N802
        self.switch.setChecked(on)

    def isChecked(self) -> bool:  # noqa: N802
        return self.switch.isChecked()
