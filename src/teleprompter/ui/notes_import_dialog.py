"""載入投影片時的講稿預覽／編輯視窗。

左：逐頁縮圖清單（顯示該頁有沒有抽到講稿）
右：該頁講稿編輯框（QTextEdit，Word 式操作：可點選、複製貼上、Undo/Redo）

使用者確認後回傳整份講稿文字（標準 `# Slide N` + `---` 格式），
由呼叫端餵給既有的 transcript 載入流程。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.notes_extractor import ExtractedNotes

_SOURCE_NAME = {
    "pptx": "PowerPoint 備忘稿",
    "html": "HTML data-speaker-notes",
    "pdf": "PDF 頁面文字（草稿，建議自行整理）",
}


class NotesImportDialog(QDialog):
    """預覽並編輯自動抽取的講稿。"""

    def __init__(
        self,
        notes: ExtractedNotes,
        deck=None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._notes = notes
        self._deck = deck
        self._pages: list[str] = list(notes.pages)
        self._titles: list[str] = list(notes.titles)
        self._current = 0

        self.setWindowTitle("確認講稿 — 自動從投影片抽取")
        self.setModal(True)
        if parent is not None:
            self.resize(int(parent.width() * 0.82), int(parent.height() * 0.82))
        else:
            self.resize(1100, 720)

        root = QVBoxLayout(self)

        src = _SOURCE_NAME.get(notes.source, notes.source or "未知來源")
        head = QLabel(
            f"📄 來源：{src}　|　共 {notes.page_count} 頁，"
            f"其中 {notes.filled_count} 頁抽到講稿"
        )
        head.setStyleSheet("font-size: 15px; color: #80D8FF; padding: 4px 2px;")
        root.addWidget(head)

        hint = QLabel(
            "左側點選頁面 → 右側可直接編輯（支援複製貼上、Ctrl+Z 復原）。"
            "確認後按「載入講稿」。"
        )
        hint.setStyleSheet("color: #AAAAAA; font-size: 13px; padding: 0 2px 6px 2px;")
        root.addWidget(hint)

        splitter = QSplitter(Qt.Horizontal)

        # 左：頁面清單（含縮圖）
        self.page_list = QListWidget()
        self.page_list.setIconSize(_thumb_size())
        self.page_list.setMinimumWidth(220)
        self.page_list.currentRowChanged.connect(self._on_page_changed)
        splitter.addWidget(self.page_list)

        # 右：編輯區
        right = QWidget()
        rlayout = QVBoxLayout(right)
        rlayout.setContentsMargins(8, 0, 0, 0)
        self.page_label = QLabel("")
        self.page_label.setStyleSheet(
            "font-size: 15px; font-weight: 600; color: #FFD54A; padding: 2px;"
        )
        rlayout.addWidget(self.page_label)

        self.editor = QTextEdit()
        self.editor.setAcceptRichText(False)
        self.editor.setPlaceholderText(
            "這一頁沒有抽到講稿——可以直接在這裡輸入或貼上。"
        )
        self.editor.setStyleSheet(
            "QTextEdit { background:#2A2A2A; color:#F0F0F0; border:1px solid #3A3A3A;"
            " border-radius:6px; padding:10px; font-size:16px; line-height:1.5; }"
        )
        self.editor.textChanged.connect(self._on_text_changed)
        rlayout.addWidget(self.editor, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        # 底部：工具 + 確認
        bottom = QHBoxLayout()
        self.clear_btn = QPushButton("清空本頁")
        self.clear_btn.clicked.connect(lambda: self.editor.clear())
        bottom.addWidget(self.clear_btn)
        bottom.addStretch(1)

        self.buttons = QDialogButtonBox()
        self.ok_btn = self.buttons.addButton("✅ 載入講稿", QDialogButtonBox.AcceptRole)
        self.skip_btn = self.buttons.addButton("略過（只載投影片）",
                                               QDialogButtonBox.DestructiveRole)
        self.cancel_btn = self.buttons.addButton("取消", QDialogButtonBox.RejectRole)
        self.ok_btn.clicked.connect(self.accept)
        self.skip_btn.clicked.connect(self._on_skip)
        self.cancel_btn.clicked.connect(self.reject)
        bottom.addWidget(self.buttons)
        root.addLayout(bottom)

        self._skipped = False
        self._populate()

    # ---------- 內部 ----------

    def _populate(self) -> None:
        self.page_list.blockSignals(True)
        for i, body in enumerate(self._pages, 1):
            title = self._titles[i - 1] if i - 1 < len(self._titles) else ""
            mark = "✅" if body.strip() else "⚠️"
            text = f"{mark} {i:02d}. {title[:18]}" if title else f"{mark} 第 {i} 頁"
            item = QListWidgetItem(text)
            if self._deck is not None:
                try:
                    pix = self._deck.thumbnail(i)
                    if pix is not None:
                        item.setIcon(_icon(pix))
                except Exception:
                    pass
            self.page_list.addItem(item)
        self.page_list.blockSignals(False)
        if self._pages:
            self.page_list.setCurrentRow(0)

    def _on_page_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._pages):
            return
        self._current = row
        title = self._titles[row] if row < len(self._titles) else ""
        self.page_label.setText(
            f"第 {row + 1} / {len(self._pages)} 頁" + (f"　·　{title}" if title else "")
        )
        self.editor.blockSignals(True)
        self.editor.setPlainText(self._pages[row])
        self.editor.blockSignals(False)

    def _on_text_changed(self) -> None:
        if 0 <= self._current < len(self._pages):
            self._pages[self._current] = self.editor.toPlainText()
            item = self.page_list.item(self._current)
            if item is not None:
                mark = "✅" if self._pages[self._current].strip() else "⚠️"
                text = item.text()
                item.setText(mark + text[1:])

    def _on_skip(self) -> None:
        self._skipped = True
        self.reject()

    # ---------- 對外 ----------

    @property
    def skipped(self) -> bool:
        """使用者選擇「只載投影片、不要講稿」。"""
        return self._skipped

    def transcript_text(self) -> str:
        """回傳編輯後的完整講稿（標準 `# Slide N` + `---` 格式）。"""
        edited = ExtractedNotes(
            source=self._notes.source, pages=self._pages, titles=self._titles
        )
        return edited.to_transcript_text()


def _thumb_size():
    from PySide6.QtCore import QSize
    return QSize(96, 54)


def _icon(pixmap):
    from PySide6.QtGui import QIcon
    return QIcon(pixmap)
