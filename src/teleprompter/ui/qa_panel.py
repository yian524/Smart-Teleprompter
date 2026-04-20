"""Q&A 面板：即時顯示觀眾提問辨識結果 + 匹配到的預備答案。

使用場景：
- 報告結束進入 Q&A 環節
- 使用者按「🎤 Q&A 模式」切換
- Whisper 辨識觀眾提問文字顯示在上方
- 自動匹配預備庫的答案顯示在下方
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.qa_library import QALibrary, QAMatch, load_qa
from ..core.translator import TranslatorController


class QAPanel(QWidget):
    """Q&A 模式主面板。"""

    qa_loaded = Signal(int)         # emit 載入的 QA 數量
    close_qa_mode = Signal()         # 使用者按「結束 Q&A」

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.library = QALibrary()
        self._recognized_accum = ""  # 累積目前的提問文字

        self.setStyleSheet(
            "QWidget { background-color: #1E1E1E; color: #F0F0F0; }"
            " QTextEdit { background-color: #2A2A2A; border: 1px solid #3A3A3A;"
            "   border-radius: 6px; padding: 8px; font-size: 16px; }"
            " QPushButton { background-color: #3A3A3A; color: white; border: none;"
            "   padding: 6px 12px; border-radius: 4px; }"
            " QPushButton:hover { background-color: #4A4A4A; }"
            " QLabel { color: #CCCCCC; font-size: 13px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 頂部工具列
        toolbar = QHBoxLayout()
        self.load_btn = QPushButton("📂 載入 Q&A 檔")
        self.load_btn.clicked.connect(self._on_load_clicked)
        toolbar.addWidget(self.load_btn)

        self.status_label = QLabel("尚未載入 Q&A 庫")
        self.status_label.setStyleSheet("color: #80D8FF; padding: 0 12px;")
        toolbar.addWidget(self.status_label)
        toolbar.addStretch(1)

        self.clear_btn = QPushButton("清空提問")
        self.clear_btn.clicked.connect(self._on_clear_clicked)
        toolbar.addWidget(self.clear_btn)

        # 翻譯開關
        self.translate_check = QCheckBox("🌐 英→中翻譯")
        self.translate_check.setStyleSheet("color: #80D8FF;")
        self.translate_check.toggled.connect(self._on_translate_toggled)
        toolbar.addWidget(self.translate_check)

        self.close_btn = QPushButton("✖ 結束 Q&A")
        self.close_btn.clicked.connect(self.close_qa_mode)
        toolbar.addWidget(self.close_btn)

        layout.addLayout(toolbar)

        # 翻譯結果顯示區（預設隱藏）
        self.translation_label = QLabel("🌐 中文翻譯")
        self.translation_label.setStyleSheet("color: #80D8FF; font-size: 13px;")
        self.translation_label.hide()
        layout.addWidget(self.translation_label)

        self.translation_text = QTextEdit()
        self.translation_text.setReadOnly(True)
        self.translation_text.setFixedHeight(80)
        self.translation_text.setStyleSheet(
            "QTextEdit { background-color: #1A1E2B; border: 1px solid #3F51B5;"
            "   border-radius: 6px; padding: 8px; font-size: 15px;"
            "   color: #E0E0E0; }"
        )
        self.translation_text.hide()
        layout.addWidget(self.translation_text)

        # 翻譯 Controller
        self.translator = TranslatorController(self)
        self.translator.translated.connect(self._on_translation_ready)
        self.translator.error.connect(self._on_translate_error)

        # 提問顯示區
        q_label = QLabel("🎤 觀眾提問（即時辨識）")
        q_label.setStyleSheet("color: #FFD54A; font-size: 14px; font-weight: 600;")
        layout.addWidget(q_label)

        self.question_text = QTextEdit()
        self.question_text.setReadOnly(True)
        self.question_text.setFixedHeight(120)
        self.question_text.setPlaceholderText("等待提問中…")
        layout.addWidget(self.question_text)

        # 匹配信心顯示
        self.match_info = QLabel("")
        self.match_info.setStyleSheet("color: #80D8FF; font-size: 12px;")
        layout.addWidget(self.match_info)

        # 答案顯示區
        a_label = QLabel("💡 建議答案（自動匹配）")
        a_label.setStyleSheet("color: #4CAF50; font-size: 14px; font-weight: 600;")
        layout.addWidget(a_label)

        self.answer_text = QTextEdit()
        self.answer_text.setReadOnly(True)
        self.answer_text.setStyleSheet(
            "QTextEdit { background-color: #1A2B1A; border: 2px solid #4CAF50;"
            "   border-radius: 6px; padding: 12px; font-size: 18px;"
            "   color: #F0F0F0; }"
        )
        self.answer_text.setPlaceholderText("載入 Q&A 庫並開始聽提問後，相符答案會顯示在此。")
        layout.addWidget(self.answer_text, 1)

        # 候選列表（備用）
        self.candidates_label = QLabel("")
        self.candidates_label.setStyleSheet("color: #999999; font-size: 12px;")
        self.candidates_label.setWordWrap(True)
        layout.addWidget(self.candidates_label)

    def _on_load_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "載入 Q&A 庫",
            "",
            "Q&A 檔案 (*.json *.md *.txt);;所有檔案 (*.*)",
        )
        if path:
            self.load_qa_file(path)

    def load_qa_file(self, path: str) -> None:
        try:
            self.library = load_qa(path)
        except Exception as e:
            self.status_label.setText(f"載入失敗: {e}")
            self.status_label.setStyleSheet("color: #F44336;")
            return
        count = len(self.library)
        self.status_label.setText(f"✅ 已載入 {count} 組 Q&A")
        self.status_label.setStyleSheet("color: #4CAF50;")
        self.qa_loaded.emit(count)

    def _on_clear_clicked(self) -> None:
        self._recognized_accum = ""
        self.question_text.clear()
        self.answer_text.clear()
        self.match_info.setText("")
        self.candidates_label.setText("")

    def append_recognized(self, text: str) -> None:
        """Speech recognizer 呼叫此函數餵入新辨識的文字。"""
        if not text.strip():
            return
        self._recognized_accum = (self._recognized_accum + text).strip()
        if len(self._recognized_accum) > 200:
            self._recognized_accum = self._recognized_accum[-200:]
        self.question_text.setPlainText(self._recognized_accum)
        self._refresh_match()
        # 若啟用翻譯 → 送去翻譯
        if self.translate_check.isChecked() and self.translator.is_running():
            self.translator.translate(self._recognized_accum)

    def _on_translate_toggled(self, checked: bool) -> None:
        self.translation_label.setVisible(checked)
        self.translation_text.setVisible(checked)
        if checked:
            if not self.translator.is_running():
                self.translator.start(source_lang="auto", target_lang="zh-TW")
            if self._recognized_accum:
                self.translator.translate(self._recognized_accum)
        else:
            self.translator.stop()
            self.translation_text.clear()

    def _on_translation_ready(self, source: str, translated: str) -> None:
        self.translation_text.setPlainText(translated)

    def _on_translate_error(self, msg: str) -> None:
        self.translation_text.setPlainText(f"(翻譯失敗：{msg})")

    def _refresh_match(self) -> None:
        if not self.library.items or not self._recognized_accum:
            return
        match = self.library.match(self._recognized_accum)
        if match is None:
            self.answer_text.clear()
            self.match_info.setText("")
            return
        # 信心提示
        if match.is_confident:
            self.match_info.setText(
                f"🎯 匹配到：「{match.item.question}」（信心 {match.score:.0f}）"
            )
            self.match_info.setStyleSheet("color: #4CAF50;")
            self.answer_text.setPlainText(match.item.answer)
        else:
            # 顯示 top 3 讓使用者判斷
            top3 = self.library.top_k(self._recognized_accum, k=3)
            self.match_info.setText(
                f"🤔 有多個可能答案（最高信心 {match.score:.0f}），請參考下方候選："
            )
            self.match_info.setStyleSheet("color: #FFC107;")
            if top3:
                self.answer_text.setPlainText(top3[0].item.answer)
                candidates_lines = [
                    f"  {i + 2}. Q: {m.item.question}"
                    for i, m in enumerate(top3[1:])
                ]
                self.candidates_label.setText("其他可能:\n" + "\n".join(candidates_lines))
            else:
                self.answer_text.clear()
