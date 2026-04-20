"""SessionTabBar：類 VSCode 的多分頁 tab 列。

包裝 `QTabBar` + 右側「+」按鈕，並與 `SessionManager` 同步：
- sessions_changed → 重建 tabs
- active_session_changed → 設定 current_index
- tab click → SessionManager.set_active
- close button → SessionManager.remove
- 「+」鈕 → 發 new_tab_requested signal（由 MainWindow 串接到新 session 建立流程）
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QTabBar, QWidget

from ..core.session import SessionManager


class SessionTabBar(QWidget):
    new_tab_requested = Signal()
    tab_switched = Signal(str)  # session_id
    tab_close_requested = Signal(str)  # session_id
    tab_rename_requested = Signal(str, str)  # session_id, new_title

    def __init__(self, manager: SessionManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._updating = False

        self.setFixedHeight(34)
        self.setStyleSheet(
            "SessionTabBar { background-color: #252525; border-bottom: 1px solid #3A3A3A; }"
            " QTabBar::tab { background-color: #2A2A2A; color: #C0C0C0;"
            "   padding: 6px 14px; border: 1px solid #3A3A3A;"
            "   border-bottom: none;"
            "   border-top-left-radius: 4px; border-top-right-radius: 4px;"
            "   margin-right: 2px; }"
            " QTabBar::tab:selected { background-color: #1E1E1E; color: #F0F0F0;"
            "   border-bottom: 2px solid #4CAF50; font-weight: 600; }"
            " QTabBar::tab:hover:!selected { background-color: #333333; }"
            " QTabBar::close-button { image: none;"
            "   subcontrol-position: right; padding: 0 4px; }"
            " QPushButton#NewTab { background: transparent; color: #80D8FF;"
            "   border: 1px dashed #555; border-radius: 4px;"
            "   padding: 2px 10px; margin: 4px; }"
            " QPushButton#NewTab:hover { background-color: #333; color: #4CAF50; }"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(0)

        self.tab_bar = QTabBar()
        self.tab_bar.setTabsClosable(True)
        self.tab_bar.setMovable(True)
        self.tab_bar.setExpanding(False)
        self.tab_bar.setDocumentMode(True)
        self.tab_bar.tabCloseRequested.connect(self._on_tab_close_requested)
        self.tab_bar.currentChanged.connect(self._on_current_changed)
        self.tab_bar.tabBarDoubleClicked.connect(self._on_tab_double_clicked)
        self.tab_bar.tabMoved.connect(self._on_tab_moved)
        layout.addWidget(self.tab_bar, 1)

        self.new_btn = QPushButton("＋ 新分頁")
        self.new_btn.setObjectName("NewTab")
        self.new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_btn.clicked.connect(self.new_tab_requested)
        layout.addWidget(self.new_btn)

        # 與 manager 連動
        manager.sessions_changed.connect(self._rebuild)
        manager.active_session_changed.connect(self._sync_active)
        self._rebuild()
        self._sync_active(manager.active_id)

    # ---------- 內部 ----------

    def _rebuild(self) -> None:
        """從 SessionManager 重建所有 tab。"""
        self._updating = True
        try:
            self.tab_bar.blockSignals(True)
            while self.tab_bar.count() > 0:
                self.tab_bar.removeTab(0)
            for s in self._manager.sessions:
                idx = self.tab_bar.addTab(s.title)
                self.tab_bar.setTabData(idx, s.session_id)
                self.tab_bar.setTabToolTip(
                    idx,
                    f"{s.title}\n講稿: {s.transcript_path or '(未指定)'}\n"
                    f"投影片: {s.slides_path or '(未指定)'}"
                )
                self._install_close_button(idx, s.session_id)
            self.tab_bar.blockSignals(False)
        finally:
            self._updating = False
        self._sync_active(self._manager.active_id)

    def _install_close_button(self, index: int, session_id: str) -> None:
        """在指定 tab 右側放一顆自訂 × 按鈕，確保深色底下可見。"""
        btn = QPushButton("✕")
        btn.setObjectName("TabClose")
        btn.setFixedSize(18, 18)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            "QPushButton#TabClose {"
            "  color: #CCCCCC; background: transparent;"
            "  border: none; border-radius: 9px;"
            "  font-size: 13px; font-weight: bold; padding: 0;"
            "}"
            "QPushButton#TabClose:hover {"
            "  background-color: #E81123; color: white;"
            "}"
        )
        btn.clicked.connect(lambda _=False, sid=session_id: self.tab_close_requested.emit(sid))
        self.tab_bar.setTabButton(index, QTabBar.ButtonPosition.RightSide, btn)

    def _sync_active(self, session_id: str) -> None:
        if self._updating:
            return
        for i in range(self.tab_bar.count()):
            if self.tab_bar.tabData(i) == session_id:
                self.tab_bar.blockSignals(True)
                self.tab_bar.setCurrentIndex(i)
                self.tab_bar.blockSignals(False)
                return

    def _on_current_changed(self, index: int) -> None:
        if self._updating or index < 0:
            return
        sid = self.tab_bar.tabData(index)
        if isinstance(sid, str) and sid:
            self.tab_switched.emit(sid)

    def _on_tab_close_requested(self, index: int) -> None:
        sid = self.tab_bar.tabData(index)
        if isinstance(sid, str) and sid:
            self.tab_close_requested.emit(sid)

    def _on_tab_double_clicked(self, index: int) -> None:
        """雙擊改名。"""
        if index < 0:
            return
        sid = self.tab_bar.tabData(index)
        if not isinstance(sid, str) or not sid:
            return
        from PySide6.QtWidgets import QInputDialog
        cur = self.tab_bar.tabText(index)
        new, ok = QInputDialog.getText(
            self, "重新命名分頁", "新名稱:", text=cur
        )
        if ok and new.strip():
            self.tab_rename_requested.emit(sid, new.strip())

    def _on_tab_moved(self, from_idx: int, to_idx: int) -> None:
        """拖曳重排 → 通知 manager。"""
        if self._updating:
            return
        sid = self.tab_bar.tabData(to_idx)
        if isinstance(sid, str) and sid:
            self._manager.move(sid, to_idx)
