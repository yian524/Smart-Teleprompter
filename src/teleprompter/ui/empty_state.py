"""空分頁引導：開新分頁時告訴使用者可以做什麼，而不是給一片全黑。

三條載入途徑在這裡與「檔案」選單、拖放保持一致：
- 直接把檔案拖進來（虛線框會在拖曳經過時亮起）
- 選擇檔案（講稿或投影片）
- 貼上文字／載入範例

有最近使用紀錄時會列出前幾筆，一鍵回載。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

ACCENT = "#2f6fed"


class EmptyStateOverlay(QFrame):
    """覆蓋在內容區上的空狀態引導卡。"""

    open_file_requested = Signal()
    paste_requested = Signal()
    sample_requested = Signal()
    recent_requested = Signal(str)   # 使用者點了某個最近檔案

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("emptyState")
        # 不吃拖放事件，讓檔案能落到主視窗的 handler
        self.setAcceptDrops(False)
        self._highlight = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 48, 48, 48)
        outer.addStretch(1)

        self.card = QFrame(self)
        self.card.setObjectName("emptyCard")
        self.card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card_lay = QVBoxLayout(self.card)
        card_lay.setContentsMargins(40, 36, 40, 32)
        card_lay.setSpacing(10)
        card_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.title = QLabel("把講稿或投影片拖進來")
        self.title.setObjectName("emptyTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_lay.addWidget(self.title)

        self.hint = QLabel("講稿：.txt / .md / .docx　　投影片：.pdf / .pptx")
        self.hint.setObjectName("emptyHint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_lay.addWidget(self.hint)

        self.divider = QLabel("或")
        self.divider.setObjectName("emptyHint")
        self.divider.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_lay.addSpacing(6)
        card_lay.addWidget(self.divider)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch(1)
        self.btn_open = self._button("選擇檔案…", primary=True)
        self.btn_open.clicked.connect(self.open_file_requested)
        btn_row.addWidget(self.btn_open)
        self.btn_paste = self._button("貼上文字")
        self.btn_paste.clicked.connect(self.paste_requested)
        btn_row.addWidget(self.btn_paste)
        self.btn_sample = self._button("載入範例")
        self.btn_sample.clicked.connect(self.sample_requested)
        btn_row.addWidget(self.btn_sample)
        btn_row.addStretch(1)
        card_lay.addSpacing(4)
        card_lay.addLayout(btn_row)

        # 最近使用（有紀錄才顯示）
        self.recent_label = QLabel("最近使用")
        self.recent_label.setObjectName("emptyHint")
        self.recent_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.recent_row = QHBoxLayout()
        self.recent_row.setSpacing(6)
        self.recent_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_lay.addSpacing(10)
        card_lay.addWidget(self.recent_label)
        card_lay.addLayout(self.recent_row)
        self._recent_buttons: list[QPushButton] = []
        self.set_recent([])

        outer.addWidget(self.card, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)
        self._restyle()

    # ---------- 內部 ----------

    def _button(self, text: str, *, primary: bool = False) -> QPushButton:
        btn = QPushButton(text, self.card)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setProperty("primary", primary)
        return btn

    def _restyle(self) -> None:
        border = ACCENT if self._highlight else "#3a3f46"
        bg = "rgba(47, 111, 237, 24)" if self._highlight else "rgba(30, 33, 38, 210)"
        self.setStyleSheet(
            "QFrame#emptyState { background-color: rgba(20, 22, 26, 235); }"
            f"QFrame#emptyCard {{ background-color: {bg};"
            f"  border: 2px dashed {border}; border-radius: 12px; }}"
            "QLabel#emptyTitle { color: #e8eaee; font-size: 19px; font-weight: 600; }"
            "QLabel#emptyHint { color: #8f959e; font-size: 12.5px; }"
            "QPushButton { background:#22262b; color:#d6d9de; border:1px solid #3a3f46;"
            "  border-radius:5px; padding:7px 16px; font-size:13px; }"
            "QPushButton:hover { background:#2c3138; }"
            "QPushButton[primary=\"true\"] { background:" + ACCENT + "; color:#fff;"
            "  border-color:" + ACCENT + "; font-weight:600; }"
            "QPushButton[primary=\"true\"]:hover { background:#4680f0; }"
        )

    # ---------- 對外 ----------

    def set_drag_highlight(self, on: bool) -> None:
        """拖曳經過視窗時把虛線框點亮，讓使用者知道放開就會載入。"""
        if on == self._highlight:
            return
        self._highlight = on
        self.title.setText("放開就會載入" if on else "把講稿或投影片拖進來")
        self._restyle()

    def set_recent(self, entries: list[str]) -> None:
        """列出最近使用的檔案（最多 3 筆）；沒有就整段隱藏。"""
        for btn in self._recent_buttons:
            self.recent_row.removeWidget(btn)
            btn.deleteLater()
        self._recent_buttons.clear()

        entries = [e for e in entries if e][:3]
        has_any = bool(entries)
        self.recent_label.setVisible(has_any)
        for path in entries:
            btn = QPushButton(Path(path).name, self.card)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(path)
            btn.clicked.connect(self._make_recent_handler(path))
            self.recent_row.addWidget(btn)
            self._recent_buttons.append(btn)

    def _make_recent_handler(self, path: str) -> Callable[[], None]:
        def handler() -> None:
            self.recent_requested.emit(path)
        return handler

    def cover(self, target: QWidget) -> None:
        """鋪滿指定容器並顯示。"""
        self.setParent(target)
        self.setGeometry(target.rect())
        self.raise_()
        self.show()

    def resizeEvent(self, event) -> None:  # noqa: D102
        super().resizeEvent(event)
        self.card.setMaximumWidth(min(560, max(320, self.width() - 96)))
