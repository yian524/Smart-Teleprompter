"""Q&A 面板：即時顯示觀眾提問辨識結果 + 匹配到的預備答案。

使用場景：
- 報告結束進入 Q&A 環節
- 使用者按「🎤 Q&A 模式」切換
- Whisper 辨識觀眾提問文字顯示在上方
- 自動匹配預備庫的答案顯示在下方
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from rapidfuzz import fuzz

from ..core.qa_library import QALibrary, QAMatch, load_qa, normalize_query
from ..core.translator import TranslatorController


class ToggleSwitch(QCheckBox):
    """手機式滑動開關：pill 底 + 圓形滑塊，勾選時滑塊移到右側並轉綠。

    繼承 QCheckBox 以沿用既有的 toggled/isChecked/setChecked API，
    只覆寫繪製與尺寸；文字仍由外部 QLabel 提供，避免與滑塊重疊。
    """

    _W, _H, _PAD = 46, 24, 3

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self._W, self._H)

    def sizeHint(self):  # noqa: D102
        return QSize(self._W, self._H)

    def paintEvent(self, event) -> None:  # noqa: D102, ARG002
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        on = self.isChecked()
        track = QColor("#2E9E5B") if on else QColor("#4A4A4A")
        painter.setPen(Qt.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(0, 0, self._W, self._H, self._H / 2, self._H / 2)
        d = self._H - self._PAD * 2
        x = self._W - d - self._PAD if on else self._PAD
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(x, self._PAD, d, d)


class QAPanel(QWidget):
    """Q&A 模式主面板。"""

    qa_loaded = Signal(int)
    close_qa_mode = Signal()
    language_changed = Signal(str)   # 'zh' / 'en' / 'auto'
    goto_page = Signal(int)          # 命中的題目帶投影片頁碼 → 主視窗翻頁（1-based）
    karaoke_toggled = Signal(bool)   # 答稿卡拉 OK 開關
    qa_path_changed = Signal(str)    # 成功載入的 QA 庫路徑 → 主視窗寫回設定

    # 低於此分數視為「沒有合適答案」，不顯示答案正文（避免照唸到錯內容）
    NO_MATCH_SCORE = 70.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.library = QALibrary()
        self._recognized_accum = ""  # 累積目前的提問文字
        self._backup_start_page = 0  # B01 對應的實際投影片頁（0 = 不換算）
        self._last_emitted_page: int | None = None  # 避免同一題重複發翻頁訊號
        self._answer_shown = ""        # 目前顯示中的答稿（卡拉 OK 對齊基準）
        self._answer_progress = 0      # 已念到的字元位置

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

        # 辨識語言下拉（Q&A 時觀眾可能講英文或中文）
        # 建議手動選擇，auto 模式對短片段辨識不穩容易出亂碼
        self.lang_combo = QComboBox()
        self.lang_combo.addItem("🇺🇸 英文（國際會議推薦）", "en")
        self.lang_combo.addItem("🇹🇼 中文", "zh")
        self.lang_combo.addItem("🌍 自動偵測（不穩定）", "auto")
        self.lang_combo.setCurrentIndex(0)  # 預設英文（國際場合）
        self.lang_combo.currentIndexChanged.connect(self._on_lang_changed)
        toolbar.addWidget(QLabel("辨識語言"))
        toolbar.addWidget(self.lang_combo)

        # 翻譯開關
        self.translate_check = QCheckBox("🌐 翻譯中文")
        self.translate_check.setStyleSheet("color: #80D8FF;")
        self.translate_check.toggled.connect(self._on_translate_toggled)
        toolbar.addWidget(self.translate_check)

        # 答稿卡拉 OK：手機式滑動開關（QCheckBox + indicator 造型）
        karaoke_label = QLabel("🎤 答稿卡拉 OK")
        karaoke_label.setStyleSheet("color: #80D8FF;")
        toolbar.addWidget(karaoke_label)
        self.karaoke_switch = ToggleSwitch()
        self.karaoke_switch.setToolTip("開啟後，念答稿時會逐字高亮（與講稿模式相同）")
        self.karaoke_switch.toggled.connect(self._on_karaoke_toggled)
        toolbar.addWidget(self.karaoke_switch)

        self.close_btn = QPushButton("✖ 結束 Q&A")
        self.close_btn.clicked.connect(self.close_qa_mode)
        toolbar.addWidget(self.close_btn)

        layout.addLayout(toolbar)

        # 1) 觀眾提問（原始辨識）— 放在最上面
        q_label = QLabel("🎤 觀眾提問（即時辨識，原文）")
        q_label.setStyleSheet("color: #FFD54A; font-size: 14px; font-weight: 600;")
        layout.addWidget(q_label)

        self.question_text = QTextEdit()
        self.question_text.setReadOnly(True)
        self.question_text.setFixedHeight(110)
        self.question_text.setPlaceholderText("等待提問中…")
        layout.addWidget(self.question_text)

        # 2) 中文翻譯（下方，只在翻譯開關啟用時顯示）
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
        self.translator.engine_ready.connect(self._on_translator_ready)
        self.translator.status_changed.connect(self._on_translator_status)

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

    def set_backup_start_page(self, page: int) -> None:
        """設定 Q&A 庫裡 B01 對應的實際投影片頁（0 = 不換算備答編號）。"""
        self._backup_start_page = max(0, int(page or 0))

    def load_qa_file(self, path: str) -> None:
        try:
            self.library = load_qa(path, self._backup_start_page)
        except Exception as e:
            self.status_label.setText(f"載入失敗: {e}")
            self.status_label.setStyleSheet("color: #F44336;")
            return
        count = len(self.library)
        paged = sum(1 for it in self.library.items if it.slide_page)
        extra = f"（{paged} 題可自動翻頁）" if paged else ""
        self.status_label.setText(f"✅ 已載入 {count} 組 Q&A{extra}")
        self.status_label.setStyleSheet("color: #4CAF50;")
        self._last_emitted_page = None
        self.qa_loaded.emit(count)
        self.qa_path_changed.emit(path)

    def _on_clear_clicked(self) -> None:
        self.clear_question()

    def append_recognized(self, text: str) -> None:
        """Speech recognizer 呼叫此函數餵入新辨識的文字。

        濾掉：
        - 太短（< 3 字）
        - 明顯重複 N-gram（Whisper hallucination）
        - 與選擇語言不符（例：選了 en 卻吐中文，或選了 zh 卻吐純英文）
        """
        text = text.strip()
        if len(text) < 3:
            return
        if self._looks_like_hallucination(text):
            return
        if self._mismatches_selected_language(text):
            return
        self._recognized_accum = (self._recognized_accum + " " + text).strip()
        if len(self._recognized_accum) > 300:
            self._recognized_accum = self._recognized_accum[-300:]
        self.question_text.setPlainText(self._recognized_accum)
        self._refresh_match()
        # 若啟用翻譯 → 送去翻譯（含英文才翻）
        if self.translate_check.isChecked() and self.translator.is_running():
            self.translator.translate(self._recognized_accum)

    def _mismatches_selected_language(self, text: str) -> bool:
        """輸出文字與使用者選的辨識語言不符 → 擋掉（多一層保護）。"""
        lang = self.get_language()
        if lang == "auto":
            return False  # auto 模式不檢查
        cjk = sum(1 for c in text if 0x4E00 <= ord(c) <= 0x9FFF)
        ratio_cjk = cjk / max(1, len(text))
        if lang == "en" and ratio_cjk > 0.2:
            return True  # 選英文卻出中文
        if lang == "zh" and len(text) >= 8 and cjk == 0:
            return True  # 選中文卻全英文
        return False

    @staticmethod
    def _looks_like_hallucination(text: str) -> bool:
        """偵測重複 N-gram 的 hallucination。"""
        if len(text) < 10:
            return False
        # 任意 3-5 gram 在文字中出現 ≥ 3 次 → hallucination
        for n in (3, 4, 5):
            if len(text) < n * 3:
                continue
            counts: dict[str, int] = {}
            for i in range(len(text) - n + 1):
                ng = text[i:i + n]
                if not ng.strip():
                    continue
                counts[ng] = counts.get(ng, 0) + 1
                if counts[ng] >= 3:
                    return True
        return False

    def clear_question(self) -> None:
        """清空累積的提問（新一輪 Q&A 時用）。"""
        self._last_emitted_page = None
        self._recognized_accum = ""
        self.question_text.clear()
        self.answer_text.clear()
        self.translation_text.clear()
        self.match_info.setText("")
        self.candidates_label.setText("")

    def get_language(self) -> str:
        return self.lang_combo.currentData() or "auto"

    def _on_lang_changed(self) -> None:
        self.language_changed.emit(self.get_language())

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

    def _on_translator_ready(self, engine_name: str) -> None:
        self.translation_label.setText(f"🌐 中文翻譯（引擎：{engine_name}）")

    def _on_translator_status(self, status_text: str) -> None:
        """載入進度 / 狀態提示（讓使用者知道下載進度等）。"""
        if self.translation_text.isVisible():
            # 若翻譯尚未就緒，把狀態顯示為暫時內容
            cur = self.translation_text.toPlainText()
            if not cur or cur.startswith("(") or cur.startswith("…"):
                self.translation_text.setPlainText(f"… {status_text}")

    def _refresh_match(self) -> None:
        if not self.library.items or not self._recognized_accum:
            return
        match = self.library.match(self._recognized_accum)
        if match is None:
            self.answer_text.clear()
            self.match_info.setText("")
            return
        if match.score < self.NO_MATCH_SCORE:
            # 守門優先於信心判定：拼音比對會讓任意中文句都拿到基礎分，
            # 光看 is_confident 會把無關提問誤判成命中（實測 61 分仍 confident）
            self.match_info.setText(
                f"❔ 未匹配到合適答案（最高信心僅 {match.score:.0f}），請自由回答"
            )
            self.match_info.setStyleSheet("color: #F44336;")
            self.answer_text.clear()
            self.candidates_label.setText("")
            return

        if match.is_confident:
            self.match_info.setText(
                f"🎯 匹配到：「{match.item.question}」（信心 {match.score:.0f}）"
            )
            self.match_info.setStyleSheet("color: #4CAF50;")
            self._set_answer(match.item.answer)
            self.candidates_label.setText("")
            self._maybe_goto_page(match.item.slide_page)
            return

        top3 = self.library.top_k(self._recognized_accum, k=3)
        if match.score < self.NO_MATCH_SCORE:
            # 全都不像 → 不硬塞答案，避免照唸到錯的內容
            self.match_info.setText(
                f"❔ 未匹配到合適答案（最高信心僅 {match.score:.0f}），請自由回答"
            )
            self.match_info.setStyleSheet("color: #F44336;")
            self.answer_text.clear()
        else:
            self.match_info.setText(
                f"🤔 有多個可能答案（最高信心 {match.score:.0f}），請參考下方候選："
            )
            self.match_info.setStyleSheet("color: #FFC107;")
            if top3:
                self._set_answer(top3[0].item.answer)
                self._maybe_goto_page(top3[0].item.slide_page)
            else:
                self.answer_text.clear()
        if top3:
            self.candidates_label.setText(
                "其他可能:" + chr(10)
                + chr(10).join(f"  {i + 2}. Q: {m.item.question}"
                               for i, m in enumerate(top3[1:]))
            )
        else:
            self.candidates_label.setText("")

    def _set_answer(self, text: str) -> None:
        """顯示答稿並重置卡拉 OK 進度（換題時重新從頭對齊）。"""
        if text != self._answer_shown:
            self._answer_shown = text
            self._answer_progress = 0
        self.answer_text.setPlainText(text)
        self._repaint_answer_highlight()

    def advance_answer_karaoke(self, spoken: str) -> None:
        """把剛念出的文字對到答稿上，推進高亮位置。

        用「已念文字的尾段」在答稿剩餘部分做模糊定位；找不到就不動，
        避免亂跳（同 AlignmentEngine 的保守精神，但這裡只需單段落等級）。
        """
        if not self.karaoke_switch.isChecked() or not self._answer_shown:
            return
        tail = normalize_query(spoken)[-24:]
        if len(tail) < 4:
            return
        rest = self._answer_shown[self._answer_progress:]
        if not rest:
            return
        window = rest[:400]
        best_end, best_score = -1, 0.0
        # 以字元為步進找最相似的結束點（步進 2 以控制成本）
        for end in range(len(tail), min(len(window), 300) + 1, 2):
            seg = normalize_query(window[max(0, end - len(tail) * 2):end])
            if not seg:
                continue
            sc = float(fuzz.partial_ratio(tail, seg))
            if sc > best_score:
                best_score, best_end = sc, end
        if best_score >= 72 and best_end > 0:
            self._answer_progress = min(len(self._answer_shown),
                                        self._answer_progress + best_end)
            self._repaint_answer_highlight()

    def _repaint_answer_highlight(self) -> None:
        """把 0.._answer_progress 的區段標成已念（灰）。"""
        selections = []
        if self.karaoke_switch.isChecked() and self._answer_progress > 0:
            sel = QTextEdit.ExtraSelection()
            fmt = QTextCharFormat()
            fmt.setForeground(QColor("#7A7A7A"))
            sel.format = fmt
            cursor = self.answer_text.textCursor()
            cursor.setPosition(0)
            cursor.setPosition(self._answer_progress, QTextCursor.KeepAnchor)
            sel.cursor = cursor
            selections.append(sel)
        self.answer_text.setExtraSelections(selections)

    def _maybe_goto_page(self, page: int | None) -> None:
        """命中的題目若帶投影片頁，發出翻頁訊號（同一頁不重複發）。"""
        if not page or page <= 0 or page == self._last_emitted_page:
            return
        self._last_emitted_page = page
        self.goto_page.emit(int(page))

    def _on_karaoke_toggled(self, checked: bool) -> None:
        if not checked:
            self._answer_progress = 0
        self._repaint_answer_highlight()
        self.karaoke_toggled.emit(bool(checked))
