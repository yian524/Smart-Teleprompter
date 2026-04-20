"""主視窗：整合所有模組，提供工具列、快捷鍵、狀態列、時間面板。"""

from __future__ import annotations

import logging
import time
from dataclasses import replace as dataclass_replace
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QPropertyAnimation, QEasingCurve

from ..config import AppConfig, load_config, save_config
from ..core.alignment_engine import AlignmentEngine
from ..core.audio_capture import AudioCaptureController, AudioWindow
from ..core.speech_recognizer import SpeechRecognizerController
from ..core.timer_controller import PaceLight, TimeColor, TimerController, format_mmss
from ..core.transcript_loader import Transcript, load_transcript, load_from_string
from .prompter_view import PrompterView
from .qa_panel import QAPanel
from .settings_dialog import SettingsDialog

logger = logging.getLogger(__name__)


PACE_TO_TEXT = {
    PaceLight.GREEN: "節奏剛好",
    PaceLight.BLUE: "稍快",
    PaceLight.YELLOW: "稍慢",
    PaceLight.GRAY: "—",
}
PACE_TO_COLOR = {
    PaceLight.GREEN: "#4CAF50",
    PaceLight.BLUE: "#2196F3",
    PaceLight.YELLOW: "#FFC107",
    PaceLight.GRAY: "#9E9E9E",
}


class LoadingOverlay(QFrame):
    """半透明載入遮罩：模型載入期間顯示，完成後淡出。

    顯示：圖示 + 主要訊息 + 次要進度文字。
    覆蓋整個父視窗，使用者看得見但無法操作（防止計時誤啟動）。
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "LoadingOverlay { background-color: rgba(0, 0, 0, 180); }"
            " QLabel { color: white; }"
        )
        # 點擊不穿透（吃掉底層操作）
        self.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(16)

        self.icon_label = QLabel("⏳")
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setStyleSheet("font-size: 64px;")
        layout.addWidget(self.icon_label)

        self.main_label = QLabel("正在準備模型…")
        self.main_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.main_label.setStyleSheet("font-size: 22px; font-weight: 600;")
        layout.addWidget(self.main_label)

        self.detail_label = QLabel("")
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_label.setStyleSheet("font-size: 14px; color: #CCCCCC;")
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # indeterminate spinner
        self.progress_bar.setFixedWidth(280)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(
            "QProgressBar { background: #333; border-radius: 4px; height: 6px; }"
            " QProgressBar::chunk { background: #4CAF50; border-radius: 4px; }"
        )
        pb_wrap = QHBoxLayout()
        pb_wrap.addStretch(1)
        pb_wrap.addWidget(self.progress_bar)
        pb_wrap.addStretch(1)
        layout.addLayout(pb_wrap)

        self.hide()
        self._fade_anim: QPropertyAnimation | None = None

    def set_status(self, main: str, detail: str = "") -> None:
        self.main_label.setText(main)
        self.detail_label.setText(detail)

    def set_ready(self, detail: str = "") -> None:
        self.icon_label.setText("✅")
        self.main_label.setText("模型就緒")
        self.detail_label.setText(detail)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)

    def show_over(self, parent_widget: QWidget) -> None:
        self.resize(parent_widget.size())
        self.move(0, 0)
        self.raise_()
        self.show()

    def fade_out_and_hide(self, duration_ms: int = 600) -> None:
        """淡出動畫後隱藏。"""
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setDuration(duration_ms)
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.finished.connect(self._on_fade_done)
        self._fade_anim.start()

    def _on_fade_done(self) -> None:
        self.hide()
        self.setWindowOpacity(1.0)


class TimePanel(QFrame):
    """頂部橫向時間/語速/投影片資訊 bar（不再 overlay 遮到講稿）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFixedHeight(42)
        self.setStyleSheet(
            "TimePanel { background-color: #252525; border-bottom: 1px solid #3A3A3A; }"
            " QLabel { color: white; }"
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 4, 16, 4)
        layout.setSpacing(16)

        self.elapsed_label = QLabel("⏱ 00:00 / --:--")
        self.elapsed_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(self.elapsed_label)

        self.remaining_label = QLabel("剩餘 --:--")
        self.remaining_label.setStyleSheet("font-size: 14px;")
        layout.addWidget(self.remaining_label)

        self.pace_dot = QLabel("●")
        self.pace_dot.setStyleSheet("color: #9E9E9E; font-size: 16px;")
        layout.addWidget(self.pace_dot)

        self.pace_text = QLabel("—")
        self.pace_text.setStyleSheet("font-size: 12px; color: #BBBBBB;")
        layout.addWidget(self.pace_text)

        layout.addStretch(1)

        # 投影片頁碼
        self.slide_label = QLabel("")
        self.slide_label.setStyleSheet("font-size: 14px; color: #80D8FF; font-weight: 600;")
        layout.addWidget(self.slide_label)

    def set_slide(self, current: int, total: int, title: str = "") -> None:
        """顯示當前投影片頁碼。若 total==0 隱藏。"""
        if total <= 0:
            self.slide_label.setText("")
            self.slide_label.hide()
            return
        if title:
            self.slide_label.setText(f"📄 Slide {current}/{total} · {title}")
        else:
            self.slide_label.setText(f"📄 Slide {current}/{total}")
        self.slide_label.show()

    def update_state(self, state) -> None:
        elapsed = format_mmss(state.elapsed_ms)
        target = format_mmss(state.target_ms) if state.target_ms > 0 else "--:--"
        self.elapsed_label.setText(f"⏱ {elapsed} / {target}")

        if state.target_ms == 0:
            self.remaining_label.setText("無目標時間")
        elif state.overrun_ms > 0:
            self.remaining_label.setText(f"超時 +{format_mmss(state.overrun_ms)}")
        else:
            self.remaining_label.setText(f"剩餘 {format_mmss(state.remaining_ms)}")

        # 顏色
        self.remaining_label.setStyleSheet(
            f"font-size: 14px; color: {state.time_color.value}; font-weight: 600;"
        )
        self.pace_dot.setStyleSheet(
            f"color: {PACE_TO_COLOR[state.pace]}; font-size: 18px;"
        )
        self.pace_text.setText(PACE_TO_TEXT[state.pace])

    def flash(self) -> None:
        """里程碑提示閃爍 1 秒。"""
        original = self.styleSheet()
        self.setStyleSheet(
            "TimePanel { background-color: rgba(255,193,7,200); border-radius: 8px; }"
            " QLabel { color: black; }"
        )
        QTimer.singleShot(900, lambda: self.setStyleSheet(original))


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.cfg = config
        self.transcript: Transcript | None = None

        self.setWindowTitle("智能語音提詞機")
        self.resize(1100, 700)

        # ---- 元件 ----
        self.engine = AlignmentEngine()
        self.engine.apply_stability_mode(getattr(config, "stability_mode", "balanced"))
        self.engine.set_max_forward_range(
            max_sentences=getattr(config, "max_forward_sentences", 0),
            max_chars=getattr(config, "max_forward_chars", 0),
        )
        self.audio = AudioCaptureController(self)
        self.recognizer = SpeechRecognizerController(self)
        self.timer_ctrl = TimerController(
            target_sec=config.target_duration_sec,
            milestones_sec=config.milestone_marks_sec,
            parent=self,
        )

        self.view = PrompterView()
        self.view.set_font_family(config.font_family)
        self.view.set_font_size(config.font_size)
        self.view.set_line_spacing(config.line_spacing)
        self.view.set_animation_duration(config.karaoke_smooth_ms)
        self.view.set_colors(
            spoken=config.spoken_color,
            upcoming=config.upcoming_color,
            current=config.highlight_color,
            skipped=config.skipped_color,
        )
        # 中央布局：頂部時間 bar + 下方 splitter (左提詞 / 右 Q&A)
        central_wrap = QWidget()
        central_layout = QVBoxLayout(central_wrap)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)

        # 時間/投影片資訊頂部 bar
        self.time_panel = TimePanel(central_wrap)
        central_layout.addWidget(self.time_panel)

        # 水平 splitter：左=提詞、右=Q&A（預設收起）
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.addWidget(self.view)
        self.qa_panel = QAPanel()
        self.qa_panel.close_qa_mode.connect(self._exit_qa_mode)
        self.qa_panel.language_changed.connect(self._switch_recognizer_language)
        self.main_splitter.addWidget(self.qa_panel)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 2)
        self.qa_panel.hide()                          # 預設不顯示，按 Q&A 模式才展開
        central_layout.addWidget(self.main_splitter, 1)

        self.setCentralWidget(central_wrap)

        # 載入遮罩（覆蓋在 view 上）
        self.loading_overlay = LoadingOverlay(self.view)
        self.loading_overlay.hide()
        # 標記「使用者按下開始但模型還沒準備好」的 pending 狀態
        self._pending_start: bool = False

        # ---- 狀態列 ----
        sb = QStatusBar()
        self.status_recognized = QLabel("等待開始…")
        self.status_recognized.setStyleSheet("padding: 0 8px;")
        sb.addWidget(self.status_recognized, 1)

        # 引擎狀態：句子位置 + 信心 + 原因（生產級可見性）
        self.status_engine = QLabel("📍 — / —  🎯 —")
        self.status_engine.setStyleSheet("padding: 0 8px; color: #B0B0B0;")
        sb.addPermanentWidget(self.status_engine)

        self.status_model = QLabel("模型: 未載入")
        sb.addPermanentWidget(self.status_model)

        self.mic_level = QProgressBar()
        self.mic_level.setRange(0, 100)
        self.mic_level.setValue(0)
        self.mic_level.setFixedWidth(120)
        self.mic_level.setTextVisible(False)
        sb.addPermanentWidget(QLabel("麥克風"))
        sb.addPermanentWidget(self.mic_level)

        self.setStatusBar(sb)

        # ---- 工具列 ----
        self._build_toolbar()
        self._build_shortcuts()

        # ---- Signal/Slot ----
        self.audio.window_ready.connect(self._on_audio_window)
        self.audio.level_changed.connect(self._on_mic_level)
        self.audio.error.connect(self._on_audio_error)

        self.recognizer.text_committed.connect(self._on_text_committed)
        self.recognizer.hypothesis.connect(self._on_hypothesis)
        self.recognizer.model_loaded.connect(self._on_model_loaded)
        self.recognizer.model_loading.connect(self._on_model_loading)
        self.recognizer.error.connect(self._on_recognizer_error)

        self.timer_ctrl.state_changed.connect(self.time_panel.update_state)
        self.timer_ctrl.milestone_reached.connect(lambda _s: self.time_panel.flash())
        self.timer_ctrl.time_up.connect(self._on_time_up)
        self.timer_ctrl.set_progress_callback(self._script_progress)

        self.view.position_clicked.connect(self._on_view_clicked)

        # 軟推進追蹤（改用 hypothesis 訊號當語音指標，避免被噪音誤觸發）
        self._last_hypothesis_time: float = 0.0
        self._last_soft_advance_time: float = 0.0
        self._soft_advance_timer = QTimer(self)
        self._soft_advance_timer.setInterval(500)
        self._soft_advance_timer.timeout.connect(self._maybe_soft_advance)
        self._soft_advance_timer.start()

        # 每 500ms 刷新 engine status bar（即使無新 commit，也讓「卡住秒數」動態更新）
        self._status_refresh_timer = QTimer(self)
        self._status_refresh_timer.setInterval(500)
        self._status_refresh_timer.timeout.connect(self._refresh_engine_status)
        self._status_refresh_timer.start()

        # 還原視窗
        if self.cfg.window_geometry:
            try:
                self.restoreGeometry(self.cfg.window_geometry)
            except Exception:
                pass
        elif self.cfg.prefer_secondary_screen:
            QTimer.singleShot(0, self._move_to_secondary_screen)

        # 載入最近開啟的檔案
        if self.cfg.last_transcript_path and Path(self.cfg.last_transcript_path).exists():
            QTimer.singleShot(50, lambda: self.load_file(self.cfg.last_transcript_path))

    # ---------- 介面建立 ----------

    def _build_toolbar(self) -> None:
        tb = QToolBar("主工具列")
        tb.setMovable(False)
        self.addToolBar(tb)

        self.act_open = QAction("📂 開啟講稿", self)
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.triggered.connect(self._open_file)
        tb.addAction(self.act_open)

        self.act_paste = QAction("📋 貼上文字", self)
        self.act_paste.triggered.connect(self._paste_text)
        tb.addAction(self.act_paste)

        tb.addSeparator()

        self.act_start = QAction("▶ 開始", self)
        self.act_start.setShortcut(Qt.Key.Key_Space)
        self.act_start.triggered.connect(self._toggle_run)
        tb.addAction(self.act_start)

        self.act_reset_pos = QAction("⤴ 回頂", self)
        self.act_reset_pos.triggered.connect(self._reset_position)
        tb.addAction(self.act_reset_pos)

        self.act_clear_skipped = QAction("✖ 清除漏講標記", self)
        self.act_clear_skipped.setShortcut("Ctrl+Shift+K")
        self.act_clear_skipped.triggered.connect(self._clear_skipped)
        tb.addAction(self.act_clear_skipped)

        tb.addSeparator()

        self.act_target = QAction("⏲ 設定時長", self)
        self.act_target.setShortcut("Ctrl+T")
        self.act_target.triggered.connect(self._ask_target_duration)
        tb.addAction(self.act_target)

        self.act_reset_timer = QAction("🔄 重置計時", self)
        self.act_reset_timer.setShortcut("R")
        self.act_reset_timer.triggered.connect(self.timer_ctrl.reset)
        tb.addAction(self.act_reset_timer)

        tb.addSeparator()

        self.act_settings = QAction("⚙ 設定", self)
        self.act_settings.triggered.connect(self._open_settings)
        tb.addAction(self.act_settings)

        self.act_fullscreen = QAction("⛶ 全螢幕", self)
        self.act_fullscreen.setShortcut("F11")
        self.act_fullscreen.triggered.connect(self._toggle_fullscreen)
        tb.addAction(self.act_fullscreen)

        tb.addSeparator()

        self.act_qa_mode = QAction("🎤 Q&A 模式", self)
        self.act_qa_mode.setShortcut("Ctrl+Q")
        self.act_qa_mode.setCheckable(True)
        self.act_qa_mode.triggered.connect(self._toggle_qa_mode)
        tb.addAction(self.act_qa_mode)

    def _build_shortcuts(self) -> None:
        QShortcut(Qt.Key.Key_Up, self, activated=lambda: self._jump_relative(-1))
        QShortcut(Qt.Key.Key_Down, self, activated=lambda: self._jump_relative(1))
        QShortcut(QKeySequence("Ctrl++"), self, activated=lambda: self.view.set_font_size(self.view.font_size() + 2))
        QShortcut(QKeySequence("Ctrl+="), self, activated=lambda: self.view.set_font_size(self.view.font_size() + 2))
        QShortcut(QKeySequence("Ctrl+-"), self, activated=lambda: self.view.set_font_size(self.view.font_size() - 2))
        QShortcut(Qt.Key.Key_T, self, activated=self._toggle_time_panel)
        # 手動標漏講：把「上次標位置 → 目前位置」之間整段標為紅色刪除線
        QShortcut(QKeySequence("Ctrl+K"), self, activated=self._manual_mark_skipped)
        # 上次手動標漏講的起點（呼叫一次後更新）
        self._last_manual_mark_pos: int = 0

    # ---------- 載入講稿 ----------

    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "開啟講稿",
            str(Path(self.cfg.last_transcript_path).parent if self.cfg.last_transcript_path else Path.home()),
            "講稿檔案 (*.txt *.md *.markdown *.docx);;所有檔案 (*.*)",
        )
        if path:
            self.load_file(path)

    def load_file(self, path: str | Path) -> None:
        try:
            transcript = load_transcript(path)
        except Exception as e:
            QMessageBox.critical(self, "載入失敗", f"無法載入檔案:\n{e}")
            return
        self._apply_transcript(transcript, source_path=str(path))

    def _paste_text(self) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self, "貼上講稿", "請貼上您的講稿文字："
        )
        if ok and text.strip():
            transcript = load_from_string(text)
            self._apply_transcript(transcript, source_path="")

    def _apply_transcript(self, transcript: Transcript, *, source_path: str) -> None:
        if not transcript.sentences:
            QMessageBox.warning(self, "講稿為空", "未能解析出任何句子。")
            return
        self.transcript = transcript
        self.engine.set_transcript(transcript)
        self.view.set_text(transcript.full_text)
        self.view.set_position(transcript.sentences[0].start, animate=False)
        # 更新 initial_prompt
        prompt = transcript.full_text[:200]
        self.recognizer.update_prompt(prompt)
        if source_path:
            self.cfg = dataclass_replace(self.cfg, last_transcript_path=source_path)
            save_config(self.cfg)
            self.setWindowTitle(f"智能語音提詞機 — {Path(source_path).name}")
        else:
            self.setWindowTitle("智能語音提詞機 — (未命名)")
        page_info = f"，{len(transcript.pages)} 頁" if transcript.pages else ""
        self.status_recognized.setText(
            f"已載入 {len(transcript.sentences)} 句{page_info}，{transcript.total_chars} 字。按空白鍵開始辨識。"
        )

    # ---------- 開始/暫停 ----------

    def _toggle_run(self) -> None:
        if self.audio.is_running():
            self._pause()
        else:
            self._start()

    def _start(self) -> None:
        if self.transcript is None or not self.transcript.sentences:
            QMessageBox.information(self, "尚未載入講稿", "請先載入或貼上講稿。")
            return

        if not self.recognizer.is_running():
            # 模型還沒載入 → 顯示遮罩、啟動載入、**不啟動計時**
            self._pending_start = True
            self.loading_overlay.set_status(
                "正在載入語音辨識模型…",
                f"模型：{self.cfg.model_size}（首次載入約需 10-30 秒）\n請稍候，模型就緒後會自動開始計時",
            )
            self.loading_overlay.show_over(self.view)
            self.recognizer.start(
                model_size=self.cfg.model_size,
                language=self.cfg.language,
                compute_type=self.cfg.compute_type,
                initial_prompt=self.transcript.full_text[:200],
            )
            self.act_start.setText("⏸ 取消")
            self.status_recognized.setText("載入模型中…")
            return

        # 模型已載入 → 直接啟動
        self._really_start_session()

    def _really_start_session(self) -> None:
        """真正啟動：模型就緒後執行，啟動麥克風 + 計時。"""
        device = self.cfg.mic_device
        device_arg: int | str | None
        if device == "":
            device_arg = None
        else:
            try:
                device_arg = int(device)
            except ValueError:
                device_arg = device
        self.audio.start(device=device_arg)
        self.timer_ctrl.start()
        self.act_start.setText("⏸ 暫停")
        self.status_recognized.setText("辨識中…")
        self._pending_start = False

    def _pause(self) -> None:
        # 若模型還在載入中，點「取消」→ 取消 pending 狀態並隱藏遮罩
        if self._pending_start and self.loading_overlay.isVisible():
            self._pending_start = False
            self.loading_overlay.fade_out_and_hide()
            self.act_start.setText("▶ 開始")
            self.status_recognized.setText("已取消載入")
            return
        self.audio.stop()
        self.timer_ctrl.pause()
        self.act_start.setText("▶ 繼續")
        self.status_recognized.setText("已暫停。")

    def _reset_position(self) -> None:
        if not self.transcript or not self.transcript.sentences:
            return
        result = self.engine.jump_to_sentence(0)
        self.view.set_position(result.global_char_pos, animate=False)
        self.view.clear_skipped()

    def _clear_skipped(self) -> None:
        self.view.clear_skipped()

    # ---------- 訊號處理 ----------

    def _on_audio_window(self, window: AudioWindow) -> None:
        self.recognizer.enqueue_window(window)

    def _on_mic_level(self, level: float) -> None:
        # 只更新麥克風強度條，不再用此判定語音活動（背景噪音會誤觸發）
        self.mic_level.setValue(int(level * 100))

    def _maybe_soft_advance(self) -> None:
        """軟推進：依語速估算自動前進。預設關閉。

        即使開啟也只在「卡住 ≥ 4 秒 + Whisper 持續產出 hypothesis」時才觸發，
        避免在正常辨識循環中插入「人為推進」造成字幕被多推幾字。
        """
        if not getattr(self.cfg, "enable_soft_advance", False):
            return
        if self.transcript is None or not self.audio.is_running():
            return
        now = time.monotonic()
        # Whisper 最近有持續產出文字 → 真實語音
        if now - self._last_hypothesis_time > 1.5:
            return
        # 真的卡住才推：距離上次 commit ≥ 4 秒
        if now - self.engine._last_commit_time < 4.0:
            return
        # 至少間隔 2 秒才能再推一次
        if now - getattr(self, "_last_soft_advance_time", 0.0) < 2.0:
            return
        old_pos = self.engine.current_global_char
        new_pos = self.engine.soft_time_advance(voice_active=True)
        if new_pos != old_pos:
            self.view.set_position(new_pos)
            self._last_soft_advance_time = now

    def _on_text_committed(self, delta: str) -> None:
        """串流辨識器吐出新穩定下來的文字 → 推進對齊位置。"""
        if not delta.strip():
            return
        # Q&A 模式啟用中：文字同時路由到 Q&A 面板
        if self.qa_panel.isVisible():
            self.qa_panel.append_recognized(delta)
            # Q&A 模式下不推進提詞位置（避免錯亂）
            return
        if self.transcript is None:
            return
        result = self.engine.update(delta)
        if result.updated:
            if result.has_skipped:
                marked = self.view.mark_skipped_ranges(result.skipped_ranges)
                self.view.set_position(result.global_char_pos, animate=False)
                if marked > 0:
                    self._flash_skip_notice(marked)
            else:
                self.view.set_position(result.global_char_pos)
        self._update_recognizer_prompt()
        # 更新引擎狀態列（不論是否更新位置都顯示，讓使用者隨時可見引擎在做什麼）
        self._update_engine_status(result)

    def _refresh_engine_status(self) -> None:
        """每 500ms 刷新引擎狀態（讓卡住秒數動態可見）。"""
        if self.transcript is None:
            return
        from teleprompter.core.alignment_engine import AlignmentResult
        dummy = AlignmentResult(
            global_char_pos=self.engine.current_global_char,
            sentence_index=self.engine.current_sentence_index,
            confidence=0.0,
            updated=False,
            reason="(idle)",
        )
        self._update_engine_status(dummy)

    def _update_engine_status(self, result) -> None:
        if self.transcript is None:
            return
        total = len(self.transcript.sentences)
        idx = self.engine.current_sentence_index
        conf = result.confidence
        reason = result.reason or ""
        symbol = "✅" if result.updated else "⏸"
        # 顯示距上次成功 commit 的秒數（讓使用者知道是否卡住）
        stuck_s = max(0.0, time.monotonic() - self.engine._last_commit_time)
        stuck_indicator = ""
        if stuck_s > 1.0:
            stuck_indicator = f"  ⚠ 卡 {stuck_s:.1f}s"
        # 把難懂的 reason 翻成使用者語言
        friendly = {
            "high confidence": "高信心",
            "mid confirmed": "中信心",
            "mid (relaxed)": "中信心(放寬)",
            "mid pending": "等待確認",
            "low confidence ignored": "信心不足",
            "globally ambiguous": "歧義(等更多字)",
            "stuck-recovery (soft)": "卡住自救",
            "stuck-recovery (hard)": "強力自救",
            "boundary punctuation": "句末標點",
            "internal error (state preserved)": "內部錯誤(已保護)",
            "(idle)": "待機",
        }
        nice = friendly.get(reason, reason)
        self.status_engine.setText(
            f"📍 sent {idx + 1}/{total}  🎯 {conf:.0f}  {symbol} {nice}{stuck_indicator}"
        )
        # 更新投影片頁碼顯示
        page = self.transcript.page_of_sentence(idx)
        if page and self.transcript.pages:
            self.time_panel.set_slide(
                page.number, len(self.transcript.pages), page.title
            )
        else:
            self.time_panel.set_slide(0, 0)

    def _manual_mark_skipped(self) -> None:
        """Ctrl+K：使用者手動把「上次標位置 → 目前位置」之間標為漏講。"""
        if self.transcript is None:
            return
        cur = self.engine.current_global_char
        from_pos = self._last_manual_mark_pos
        rng = self.engine.manual_mark_skipped_to_current(from_pos)
        if rng is None:
            self._flash_skip_notice(0)
            self.status_recognized.setText("（無內容可標）")
            return
        s, e = rng
        self.view.mark_skipped(s, e)
        self._last_manual_mark_pos = cur
        self._flash_skip_notice(e - s)

    def _flash_skip_notice(self, char_count: int) -> None:
        """status bar 顯示「漏講」提示 1.5 秒。"""
        original = self.status_recognized.styleSheet()
        self.status_recognized.setText(f"⚠ 偵測到漏講 {char_count} 字")
        self.status_recognized.setStyleSheet(
            "padding: 0 8px; color: white; background: #FF1744; font-weight: 600;"
        )
        QTimer.singleShot(
            1500,
            lambda: self.status_recognized.setStyleSheet(original),
        )

    def _on_hypothesis(self, text: str) -> None:
        """串流辨識器目前的完整 hypothesis（含尾巴未穩定部分）→ 顯示在 status bar。

        同時更新 _last_hypothesis_time 作為「真實語音活動」訊號（替代 mic_level，
        因為 Whisper 對純背景噪音通常不會吐出文字）。
        """
        # 太短的輸出（< 2 字）視為雜訊，不更新語音活動時間
        if text and len(text.strip()) >= 2:
            self._last_hypothesis_time = time.monotonic()
        snippet = text[-50:] if len(text) > 50 else text
        self.status_recognized.setText(f"🎙 {snippet}")

    def _update_recognizer_prompt(self) -> None:
        """以「過去 100 字 + 未來 100 字」作為 Whisper 的 initial_prompt。

        過去：保持上下文連貫；未來：讓 Whisper 預期下一段詞彙，提升專有名詞精度。
        """
        if self.transcript is None:
            return
        full = self.transcript.full_text
        cur = self.engine.current_global_char
        start = max(0, cur - 100)
        end = min(len(full), cur + 100)
        prompt = full[start:end]
        if prompt:
            self.recognizer.update_prompt(prompt)

    def _on_model_loading(self, msg: str) -> None:
        """Whisper 發出的載入進度訊息（如「下載中」「載入中」）。"""
        self.status_model.setText(msg)
        # 載入遮罩顯示中時更新細節
        if self.loading_overlay.isVisible():
            self.loading_overlay.set_status("正在載入語音辨識模型…", msg)

    def _on_model_loaded(self, info: str) -> None:
        """模型載入完成 → 隱藏遮罩 + 正式啟動會話。"""
        self.status_model.setText(f"模型: {self.cfg.model_size} ({info})")
        if self.loading_overlay.isVisible():
            self.loading_overlay.set_ready(f"設備：{info}  模型：{self.cfg.model_size}")
            # 延遲 400ms 讓使用者看到「✅ 模型就緒」，然後淡出
            QTimer.singleShot(400, self._begin_session_after_load)

    def _begin_session_after_load(self) -> None:
        self.loading_overlay.fade_out_and_hide()
        if self._pending_start:
            # 使用者當初有按開始 → 自動接手正式啟動計時
            self._really_start_session()

    def _on_audio_error(self, msg: str) -> None:
        QMessageBox.warning(self, "麥克風錯誤", msg)
        self._pause()

    def _on_recognizer_error(self, msg: str) -> None:
        QMessageBox.warning(self, "語音辨識錯誤", msg)

    def _on_time_up(self) -> None:
        self.time_panel.flash()
        self.status_recognized.setText("⚠ 已達設定時長。")

    def _on_view_clicked(self, global_char: int) -> None:
        result = self.engine.jump_to_global_char(global_char)
        self.view.set_position(result.global_char_pos, animate=False)

    def _script_progress(self) -> float:
        if self.transcript is None or self.transcript.total_chars == 0:
            return 0.0
        return self.engine.current_global_char / self.transcript.total_chars

    # ---------- 動作 ----------

    def _jump_relative(self, delta: int) -> None:
        if self.transcript is None:
            return
        new_idx = self.engine.current_sentence_index + delta
        result = self.engine.jump_to_sentence(new_idx)
        self.view.set_position(result.global_char_pos)

    def _toggle_time_panel(self) -> None:
        self.time_panel.setVisible(not self.time_panel.isVisible())

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _toggle_qa_mode(self) -> None:
        """切換 Q&A 模式：右側顯示/隱藏 QA 面板。"""
        if self.qa_panel.isVisible():
            self._exit_qa_mode()
        else:
            self._enter_qa_mode()

    def _enter_qa_mode(self) -> None:
        self.qa_panel.show()
        self.act_qa_mode.setChecked(True)
        self.act_qa_mode.setText("🎤 Q&A 模式 (ON)")
        # 預設切換到「自動語言偵測」以辨識英文提問
        self._switch_recognizer_language(self.qa_panel.get_language())
        self.status_recognized.setText("Q&A 模式：觀眾提問會辨識並匹配預備答案")

    def _exit_qa_mode(self) -> None:
        self.qa_panel.hide()
        self.act_qa_mode.setChecked(False)
        self.act_qa_mode.setText("🎤 Q&A 模式")
        # 切回預設語言
        self._switch_recognizer_language(self.cfg.language)
        self.status_recognized.setText("已回到提詞模式")

    def _switch_recognizer_language(self, language: str) -> None:
        """重啟 recognizer 以套用新語言（language 只能在建構時設）。

        Q&A 模式（qa panel visible）時：
        - 不傳中文講稿當 prompt（避免 Whisper 被中文語料誘導輸出中文）
        - 用中性 prompt 或空字串
        """
        if not self.recognizer.is_running():
            return
        self.recognizer.stop()
        # 決定 initial_prompt：提詞模式用講稿；Q&A 模式不帶任何偏向
        if self.qa_panel.isVisible():
            # Q&A 模式：用空 prompt，讓 Whisper 純粹依音訊辨識
            prompt = ""
        else:
            prompt = self.transcript.full_text[:200] if self.transcript else ""
        self.recognizer.start(
            model_size=self.cfg.model_size,
            language=language,
            compute_type=self.cfg.compute_type,
            initial_prompt=prompt,
        )

    def _ask_target_duration(self) -> None:
        seconds, ok = QInputDialog.getInt(
            self,
            "設定目標時長",
            "請輸入目標報告時長（秒）：",
            value=self.cfg.target_duration_sec,
            minValue=0,
            maxValue=7200,
            step=30,
        )
        if ok:
            self.cfg = dataclass_replace(self.cfg, target_duration_sec=seconds)
            self.timer_ctrl.set_target_seconds(seconds)
            save_config(self.cfg)

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec() == dlg.DialogCode.Accepted:
            new_cfg = dlg.updated_config()
            self.cfg = new_cfg
            save_config(new_cfg)
            self._apply_config_to_ui()

    def _apply_config_to_ui(self) -> None:
        self.view.set_font_family(self.cfg.font_family)
        self.view.set_font_size(self.cfg.font_size)
        self.view.set_line_spacing(self.cfg.line_spacing)
        self.view.set_animation_duration(self.cfg.karaoke_smooth_ms)
        self.view.set_colors(
            spoken=self.cfg.spoken_color,
            upcoming=self.cfg.upcoming_color,
            current=self.cfg.highlight_color,
            skipped=self.cfg.skipped_color,
        )
        self.timer_ctrl.set_target_seconds(self.cfg.target_duration_sec)
        self.timer_ctrl.set_milestones(self.cfg.milestone_marks_sec)
        # 套用穩定性模式 + 最大跳段範圍
        self.engine.apply_stability_mode(getattr(self.cfg, "stability_mode", "balanced"))
        self.engine.set_max_forward_range(
            max_sentences=getattr(self.cfg, "max_forward_sentences", 0),
            max_chars=getattr(self.cfg, "max_forward_chars", 0),
        )

    # ---------- 視窗 ----------

    def _move_to_secondary_screen(self) -> None:
        screens = QGuiApplication.screens()
        if len(screens) < 2:
            return
        secondary = screens[1]
        geo = secondary.availableGeometry()
        self.move(geo.x() + 50, geo.y() + 50)
        self.resize(min(self.width(), geo.width() - 100), min(self.height(), geo.height() - 100))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 遮罩跟著 view 同大小
        if hasattr(self, "loading_overlay") and self.loading_overlay.isVisible():
            self.loading_overlay.resize(self.view.size())

    def closeEvent(self, event) -> None:
        self.audio.stop()
        self.recognizer.stop()
        self.timer_ctrl.pause()
        self.cfg = dataclass_replace(self.cfg, window_geometry=bytes(self.saveGeometry()))
        save_config(self.cfg)
        super().closeEvent(event)
