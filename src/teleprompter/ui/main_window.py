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
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QSpinBox,
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
from ..core.pdf_renderer import SlideDeck, load_slide_deck
from ..core.pptx_converter import PptxConversionError, convert_pptx_to_pdf
from ..core.recorder import RecordingController, default_recording_root
from ..core.session import Session, SessionManager, default_sessions_path
from .page_divider_overlay import PageDividerOverlay
from .prompter_view import PrompterView
from .qa_panel import QAPanel
from .record_source_dialog import RecordSourceDialog
from .session_tab_bar import SessionTabBar
from .settings_dialog import SettingsDialog
from .slide_preview import SlidePreviewPanel
from .slide_viewer_dialog import SlideViewerDialog

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

        self.setWindowTitle("智能語音提詞機")
        # 套 app icon（若 main.py 沒設也能 fallback）
        try:
            from ..main import make_app_icon
            self.setWindowIcon(make_app_icon())
        except Exception:
            pass
        self.resize(1100, 700)

        # ---- Session 管理（多分頁）----
        self.session_manager = SessionManager(self)
        self.session_manager.active_session_changed.connect(self._on_active_session_changed)

        # runtime references（由 _bind_session_runtime 設定）
        # 保持一個預設 engine 避免某些訊號在空狀態下 crash
        self._placeholder_engine = self._new_engine_for_config()
        self.engine: AlignmentEngine = self._placeholder_engine
        self.transcript: Transcript | None = None
        self.slide_deck: SlideDeck | None = None
        self._bound_session_id: str = ""  # 目前綁在哪個 session

        self.audio = AudioCaptureController(self)
        self.recognizer = SpeechRecognizerController(self)
        # 演講錄影（即時視訊 + 聲音 → MP4）
        self.recorder = RecordingController(self)
        self.audio.raw_frame.connect(self.recorder.on_audio_frame)
        self.recorder.started.connect(self._on_record_started)
        self.recorder.stopped.connect(self._on_record_stopped)
        self.recorder.tick.connect(self._on_record_tick)
        self.recorder.error.connect(self._on_record_error)
        self.recorder.muxing_started.connect(self._on_record_muxing_started)
        self._mux_dialog: "QProgressDialog | None" = None
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
        # 中央布局：tabs + 時間 bar + 下方 splitter (左提詞 / 中投影片 / 右 Q&A)
        central_wrap = QWidget()
        central_layout = QVBoxLayout(central_wrap)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)

        # Session 分頁 bar（頂部）
        self.session_tab_bar = SessionTabBar(self.session_manager, central_wrap)
        self.session_tab_bar.new_tab_requested.connect(self._new_tab)
        self.session_tab_bar.tab_switched.connect(self._on_tab_switched)
        self.session_tab_bar.tab_close_requested.connect(self._on_tab_close_requested)
        self.session_tab_bar.tab_rename_requested.connect(self._on_tab_rename)
        central_layout.addWidget(self.session_tab_bar)

        # 時間/投影片資訊頂部 bar
        self.time_panel = TimePanel(central_wrap)
        central_layout.addWidget(self.time_panel)

        # 巢狀 splitter：外=[內層 + Q&A]，內=[提詞 + 投影片預覽]
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.content_splitter.addWidget(self.view)

        # 投影片改為嵌入 PrompterView 內部（右欄 + 全寬 hr），不需獨立面板
        self.slide_preview = SlidePreviewPanel()
        self.slide_preview.hide()
        self.content_splitter.setStretchFactor(0, 1)

        self.main_splitter.addWidget(self.content_splitter)
        self.qa_panel = QAPanel()
        self.qa_panel.close_qa_mode.connect(self._exit_qa_mode)
        self.qa_panel.language_changed.connect(self._switch_recognizer_language)
        self.main_splitter.addWidget(self.qa_panel)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 2)
        self.qa_panel.hide()                          # 預設不顯示，按 Q&A 模式才展開
        central_layout.addWidget(self.main_splitter, 1)

        self.setCentralWidget(central_wrap)

        # 允許拖拉檔案到主視窗（不需經由檔案對話框）
        self.setAcceptDrops(True)

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

        # 錄製狀態指示
        self.status_recording = QLabel("")
        self.status_recording.setStyleSheet("padding: 0 8px; color: #F44336; font-weight: 600;")
        sb.addPermanentWidget(self.status_recording)

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
        self.view.slide_double_clicked.connect(self._open_slide_viewer)
        self.view.font_size_changed.connect(self._sync_font_spinbox)
        # 滾動時更新右上角頁碼顯示（以 viewport 為準）
        self.view.verticalScrollBar().valueChanged.connect(
            self._update_slide_label_from_viewport
        )

        # 對齊重算 debounce timer
        self._align_timer = QTimer(self)
        self._align_timer.setSingleShot(True)
        self._align_timer.setInterval(120)
        self._align_timer.timeout.connect(self._align_page_heights)

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

        # 啟動後載入 sessions.json（延遲一下讓 UI 先渲染完）
        QTimer.singleShot(50, self._restore_sessions_or_bootstrap)

    # ---------- 介面建立 ----------

    def _build_toolbar(self) -> None:
        tb = QToolBar("主工具列")
        tb.setMovable(False)
        self.addToolBar(tb)

        self.act_open = QAction("📂 開啟講稿", self)
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.triggered.connect(self._open_file)
        tb.addAction(self.act_open)

        self.act_save = QAction("💾 儲存", self)
        self.act_save.setToolTip("把目前講稿寫回檔案 (Ctrl+S)")
        self.act_save.setShortcut("Ctrl+S")
        self.act_save.triggered.connect(self._save_current_transcript)
        tb.addAction(self.act_save)

        self.act_paste = QAction("📋 貼上文字", self)
        self.act_paste.triggered.connect(self._paste_text)
        tb.addAction(self.act_paste)

        self.act_open_slides = QAction("🖼 載入投影片", self)
        self.act_open_slides.setToolTip("載入 PDF 或 PPTX 作為右側視覺參考")
        self.act_open_slides.triggered.connect(self._open_slides)
        tb.addAction(self.act_open_slides)

        tb.addSeparator()

        # === 播放區（演講進行中最常用，放在檔案區右側）===
        self.act_start = QAction("▶ 開始", self)
        self.act_start.setShortcut(Qt.Key.Key_Space)
        self.act_start.setToolTip("開始 / 暫停語音辨識（Space）")
        self.act_start.triggered.connect(self._toggle_run)
        tb.addAction(self.act_start)

        self.act_goto_speech = QAction("📍 回念稿位置", self)
        self.act_goto_speech.setToolTip("把視窗捲回目前辨識的位置（Ctrl+Home）")
        self.act_goto_speech.setShortcut("Ctrl+Home")
        self.act_goto_speech.triggered.connect(self._goto_speech_position)
        tb.addAction(self.act_goto_speech)

        self.act_reset_pos = QAction("⤴ 回頂", self)
        self.act_reset_pos.setToolTip("講稿跳回第一句")
        self.act_reset_pos.triggered.connect(self._reset_position)
        tb.addAction(self.act_reset_pos)

        self.act_clear_skipped = QAction("✖ 清漏講", self)
        self.act_clear_skipped.setToolTip("清除漏講標記（Ctrl+Shift+K）")
        self.act_clear_skipped.setShortcut("Ctrl+Shift+K")
        self.act_clear_skipped.triggered.connect(self._clear_skipped)
        tb.addAction(self.act_clear_skipped)

        tb.addSeparator()

        # === 字級調整 ===
        self.act_font_smaller = QAction("A−", self)
        self.act_font_smaller.setToolTip("字型縮小 (Ctrl+-)")
        self.act_font_smaller.triggered.connect(
            lambda: (
                self.view.set_font_size(max(12, self.view.font_size() - 2)),
                self.sb_font_size.setValue(self.view.font_size()),
            )
        )
        tb.addAction(self.act_font_smaller)

        self.sb_font_size = QSpinBox()
        self.sb_font_size.setRange(12, 120)
        self.sb_font_size.setValue(self.cfg.font_size)
        self.sb_font_size.setSuffix(" pt")
        self.sb_font_size.setToolTip("字型大小")
        self.sb_font_size.setFixedWidth(80)
        self.sb_font_size.valueChanged.connect(self._on_font_size_spinbox)
        tb.addWidget(self.sb_font_size)

        self.act_font_bigger = QAction("A+", self)
        self.act_font_bigger.setToolTip("字型放大 (Ctrl++)")
        self.act_font_bigger.triggered.connect(
            lambda: (
                self.view.set_font_size(min(120, self.view.font_size() + 2)),
                self.sb_font_size.setValue(self.view.font_size()),
            )
        )
        tb.addAction(self.act_font_bigger)

        tb.addSeparator()

        # === 計時區 ===
        self.cb_target = QCheckBox("⏲ 目標時長")
        self.cb_target.setChecked(self.cfg.target_duration_sec > 0)
        self.cb_target.toggled.connect(self._on_target_toggled)
        tb.addWidget(self.cb_target)

        self.sb_target_min = QSpinBox()
        self.sb_target_min.setRange(1, 180)
        self.sb_target_min.setSuffix(" 分")
        self.sb_target_min.setValue(max(1, self.cfg.target_duration_sec // 60 or 15))
        self.sb_target_min.setFixedWidth(80)
        self.sb_target_min.valueChanged.connect(self._on_target_minutes_changed)
        self._sb_target_min_action = tb.addWidget(self.sb_target_min)
        self._sb_target_min_action.setVisible(self.cb_target.isChecked())

        self.act_reset_timer = QAction("🔄 重置計時", self)
        self.act_reset_timer.setToolTip("計時歸零（R）")
        self.act_reset_timer.setShortcut("R")
        self.act_reset_timer.triggered.connect(self.timer_ctrl.reset)
        tb.addAction(self.act_reset_timer)

        tb.addSeparator()

        # === 模式區 ===
        self.act_edit_mode = QAction("✏ 編輯模式", self)
        self.act_edit_mode.setShortcut("Ctrl+E")
        self.act_edit_mode.setCheckable(True)
        self.act_edit_mode.toggled.connect(self._toggle_edit_mode)
        tb.addAction(self.act_edit_mode)

        self.act_qa_mode = QAction("🎤 Q&A 模式", self)
        self.act_qa_mode.setShortcut("Ctrl+Q")
        self.act_qa_mode.setCheckable(True)
        self.act_qa_mode.triggered.connect(self._toggle_qa_mode)
        tb.addAction(self.act_qa_mode)

        self.act_record = QAction("⏺ 錄影", self)
        self.act_record.setToolTip("開始 / 停止螢幕錄影 + 麥克風 → MP4")
        self.act_record.setCheckable(True)
        self.act_record.triggered.connect(self._toggle_recording)
        tb.addAction(self.act_record)

        tb.addSeparator()

        # === 系統區（最少用，放最右）===
        self.act_fullscreen = QAction("⛶ 全螢幕", self)
        self.act_fullscreen.setShortcut("F11")
        self.act_fullscreen.triggered.connect(self._toggle_fullscreen)
        tb.addAction(self.act_fullscreen)

        self.act_settings = QAction("⚙ 設定", self)
        self.act_settings.triggered.connect(self._open_settings)
        tb.addAction(self.act_settings)

        # 第二條工具列：編輯專用（編輯模式開啟才顯示）
        self.edit_toolbar = QToolBar("編輯工具列", self)
        self.edit_toolbar.setMovable(False)
        self.addToolBarBreak()   # 另起一行
        self.addToolBar(self.edit_toolbar)

        self.act_insert_annotation = QAction("💬 插入註解", self)
        self.act_insert_annotation.triggered.connect(self._insert_annotation)
        self.edit_toolbar.addAction(self.act_insert_annotation)

        self.act_compact_ws = QAction("🧹 清理空白", self)
        self.act_compact_ws.setToolTip("移除多餘空白行與行尾空白，段落間只保留一個空白行")
        self.act_compact_ws.triggered.connect(self._compact_whitespace)
        self.edit_toolbar.addAction(self.act_compact_ws)

        self.edit_toolbar.addSeparator()

        self.act_bold = QAction("B", self)
        self.act_bold.setToolTip("粗體 (Ctrl+B)")
        self.act_bold.setShortcut("Ctrl+B")
        self.act_bold.triggered.connect(self.view.toggle_bold)
        self.edit_toolbar.addAction(self.act_bold)

        self.act_italic = QAction("I", self)
        self.act_italic.setToolTip("斜體 (Ctrl+I)")
        self.act_italic.setShortcut("Ctrl+I")
        self.act_italic.triggered.connect(self.view.toggle_italic)
        self.edit_toolbar.addAction(self.act_italic)

        self.act_underline = QAction("U", self)
        self.act_underline.setToolTip("底線 (Ctrl+U)")
        self.act_underline.setShortcut("Ctrl+U")
        self.act_underline.triggered.connect(self.view.toggle_underline)
        self.edit_toolbar.addAction(self.act_underline)

        self.act_highlight = QAction("🖍", self)
        self.act_highlight.setToolTip("螢光筆 (Ctrl+H)")
        self.act_highlight.setShortcut("Ctrl+H")
        self.act_highlight.triggered.connect(self.view.toggle_highlight)
        self.edit_toolbar.addAction(self.act_highlight)

        self.act_clear_fmt = QAction("✖格式", self)
        self.act_clear_fmt.setToolTip("清除選取範圍的格式 (Ctrl+\\)")
        self.act_clear_fmt.setShortcut("Ctrl+\\")
        self.act_clear_fmt.triggered.connect(self.view.clear_format)
        self.edit_toolbar.addAction(self.act_clear_fmt)

        self.act_clear_all_fmt = QAction("🧽 全部清除", self)
        self.act_clear_all_fmt.setToolTip("清除整篇文字的粗體/斜體/底線/螢光筆格式（不需選取）")
        self.act_clear_all_fmt.triggered.connect(self._clear_all_formatting)
        self.edit_toolbar.addAction(self.act_clear_all_fmt)

        # 預設隱藏整條 edit toolbar（只在編輯模式顯示）
        self.edit_toolbar.setVisible(False)
        for act in (
            self.act_insert_annotation, self.act_compact_ws,
            self.act_bold, self.act_italic, self.act_underline,
            self.act_highlight, self.act_clear_fmt, self.act_clear_all_fmt,
        ):
            act.setEnabled(False)

        # 編輯模式切換時重設結果（MD 重新 parse）
        self.view.text_edited.connect(self._on_transcript_edited)
        self.view.edit_mode_changed.connect(self._on_edit_mode_changed)

    def _build_shortcuts(self) -> None:
        QShortcut(Qt.Key.Key_Up, self, activated=lambda: self._jump_relative(-1))
        QShortcut(Qt.Key.Key_Down, self, activated=lambda: self._jump_relative(1))
        QShortcut(QKeySequence("Ctrl++"), self, activated=lambda: self.view.set_font_size(self.view.font_size() + 2))
        QShortcut(QKeySequence("Ctrl+="), self, activated=lambda: self.view.set_font_size(self.view.font_size() + 2))
        QShortcut(QKeySequence("Ctrl+-"), self, activated=lambda: self.view.set_font_size(self.view.font_size() - 2))
        QShortcut(Qt.Key.Key_T, self, activated=self._toggle_time_panel)
        # 手動標漏講：把「上次標位置 → 目前位置」之間整段標為紅色刪除線
        QShortcut(QKeySequence("Ctrl+K"), self, activated=self._manual_mark_skipped)
        # 新分頁
        QShortcut(QKeySequence("Ctrl+T"), self, activated=self._new_tab)
        QShortcut(QKeySequence("Ctrl+W"), self, activated=self._close_current_tab_shortcut)
        # 上次手動標漏講的起點（呼叫一次後更新）
        self._last_manual_mark_pos: int = 0

    # ---------- 載入講稿 ----------

    # ---------- Session 管理 ----------

    def _new_engine_for_config(self) -> AlignmentEngine:
        """建立一個新的 AlignmentEngine 並套用目前 config。"""
        eng = AlignmentEngine()
        eng.apply_stability_mode(getattr(self.cfg, "stability_mode", "balanced"))
        eng.set_max_forward_range(
            max_sentences=getattr(self.cfg, "max_forward_sentences", 0),
            max_chars=getattr(self.cfg, "max_forward_chars", 0),
        )
        return eng

    def _ensure_active_session(self) -> Session:
        """取得 active session；若無則建立並 activate 一個空白 session。"""
        s = self.session_manager.active
        if s is not None:
            if s.engine is None:
                s.engine = self._new_engine_for_config()
            return s
        s = Session(title="未命名")
        s.engine = self._new_engine_for_config()
        self.session_manager.add(s)   # 會自動 set_active → 觸發 _on_active_session_changed
        return s

    def _new_tab(self) -> None:
        """按「+ 新分頁」：建立一個空白 session。"""
        s = Session(title="未命名")
        s.engine = self._new_engine_for_config()
        self.session_manager.add(s)

    def _on_tab_switched(self, session_id: str) -> None:
        self.session_manager.set_active(session_id)

    def _on_tab_close_requested(self, session_id: str) -> None:
        # 若只剩一個且是這個 → 不關（保留空白起始 tab）
        if len(self.session_manager) <= 1:
            s = self.session_manager.get(session_id)
            if s is not None:
                # 清空內容變回初始狀態
                s.transcript = None
                s.transcript_path = ""
                if s.slide_deck is not None:
                    s.slide_deck.close()
                s.slide_deck = None
                s.slides_path = ""
                s.title = "未命名"
                s.current_global_char = 0
                s.skipped_ranges = []
                s.format_spans = []
                self._bound_session_id = ""  # 強制下面 bind 重跑
                self._bind_session_runtime(s)
                self.session_manager.sessions_changed.emit()
            return
        self.session_manager.remove(session_id)

    def _close_current_tab_shortcut(self) -> None:
        """Ctrl+W → 關閉目前 tab。"""
        if self.session_manager.active is not None:
            self._on_tab_close_requested(self.session_manager.active.session_id)

    def _on_tab_rename(self, session_id: str, new_title: str) -> None:
        s = self.session_manager.get(session_id)
        if s is None:
            return
        s.title = new_title
        self.session_manager.sessions_changed.emit()

    def _on_active_session_changed(self, session_id: str) -> None:
        """active session 變更 → 先把目前 view 狀態寫回舊 session，再 bind 新 session。"""
        # 儲存舊 session 的 view 狀態
        if self._bound_session_id:
            old = self.session_manager.get(self._bound_session_id)
            if old is not None:
                self._save_view_state_to(old)
        # 套用新 session
        new_s = self.session_manager.get(session_id)
        if new_s is None:
            return
        if new_s.engine is None:
            new_s.engine = self._new_engine_for_config()
        self._bind_session_runtime(new_s)

    def _save_view_state_to(self, session: Session) -> None:
        """把目前 view/engine 狀態寫回 session（呼叫前要確認 session 是 self 目前 bound 的那個）。"""
        session.current_global_char = self.engine.current_global_char
        session.current_sentence_index = self.engine.current_sentence_index
        session.skipped_ranges = list(self.view._skipped_ranges)
        # 格式化 spans（可能為空）
        try:
            session.format_spans = self.view.dump_format_spans()
        except Exception:
            session.format_spans = []

    def _bind_session_runtime(self, session: Session) -> None:
        """把 session 的 transcript/engine/slide_deck 套到 UI。"""
        self.engine = session.engine or self._new_engine_for_config()
        self.transcript = session.transcript
        self.slide_deck = session.slide_deck
        self._bound_session_id = session.session_id

        if session.transcript is not None:
            if self.engine.transcript is not session.transcript:
                self.engine.set_transcript(session.transcript)
            self.view.set_text(session.transcript.full_text)
            # 還原格式
            if session.format_spans:
                self.view.restore_format_spans(session.format_spans)
            # 還原漏講
            if session.skipped_ranges:
                self.view.mark_skipped_ranges(session.skipped_ranges)
            # 還原位置
            pos = session.current_global_char
            if pos > 0:
                result = self.engine.jump_to_global_char(pos)
                self.view.set_position(result.global_char_pos, animate=False)
            else:
                self.view.set_position(session.transcript.sentences[0].start, animate=False)
            self.recognizer.update_prompt(session.transcript.full_text[:200])
            self.setWindowTitle(f"智能語音提詞機 — {session.title}")
            page_info = f"，{len(session.transcript.pages)} 頁" if session.transcript.pages else ""
            self.status_recognized.setText(
                f"{session.title} · {len(session.transcript.sentences)} 句{page_info}"
            )
        else:
            self.view.set_text("")
            self.setWindowTitle(f"智能語音提詞機 — {session.title}")
            self.status_recognized.setText("請載入講稿（📂 開啟講稿）")

        # 嵌入式投影片 → 直接餵給 PrompterView
        self.view.set_slide_deck(session.slide_deck)
        if session.slide_deck is not None:
            self._sync_slide_to_current_sentence()

    def _restore_sessions_or_bootstrap(self) -> None:
        """啟動時嘗試從 sessions.json 還原；否則 bootstrap 一個空 session，
        再嘗試 legacy `last_transcript_path` 還原。"""
        sessions_path = default_sessions_path()
        self.session_manager.load_from_disk(sessions_path)
        if len(self.session_manager) > 0:
            # 針對每個 session，嘗試載入檔案
            for s in self.session_manager.sessions:
                self._rehydrate_session(s)
            # 確認 active session 已 bind
            active = self.session_manager.active
            if active is not None:
                self._bind_session_runtime(active)
            return
        # 空的 → bootstrap 一個 session
        s = Session(title="未命名")
        s.engine = self._new_engine_for_config()
        self.session_manager.add(s)
        # legacy：若有 last_transcript_path → 載入
        if self.cfg.last_transcript_path and Path(self.cfg.last_transcript_path).exists():
            self.load_file(self.cfg.last_transcript_path)

    def _rehydrate_session(self, session: Session) -> None:
        """對一個從 JSON 讀出的 session，載入 transcript 檔/投影片檔。

        檔案遺失時保留 session 但標題加上 ⚠️，使用者可手動重新載入。
        """
        if session.engine is None:
            session.engine = self._new_engine_for_config()
        missing: list[str] = []
        # 優先使用 modified_text（使用者編輯過的內容），沒有才讀檔
        if session.modified_text:
            try:
                session.transcript = load_from_string(session.modified_text)
                session.engine.set_transcript(session.transcript)
            except Exception as e:
                logger.warning("還原 %s 的編輯內容失敗: %s", session.session_id, e)
                session.transcript = None
                missing.append("講稿(編輯內容)")
        elif session.transcript_path:
            if Path(session.transcript_path).exists():
                try:
                    session.transcript = load_transcript(session.transcript_path)
                    session.engine.set_transcript(session.transcript)
                except Exception as e:
                    logger.warning("還原 %s 的講稿失敗: %s", session.session_id, e)
                    session.transcript = None
                    missing.append("講稿")
            else:
                missing.append("講稿")
        if session.slides_path:
            if Path(session.slides_path).exists():
                try:
                    p = Path(session.slides_path)
                    if p.suffix.lower() in (".pptx", ".ppt"):
                        pdf_path = convert_pptx_to_pdf(p)
                    else:
                        pdf_path = p
                    session.slide_deck = load_slide_deck(pdf_path)
                except Exception as e:
                    logger.warning("還原 %s 的投影片失敗: %s", session.session_id, e)
                    session.slide_deck = None
                    missing.append("投影片")
            else:
                missing.append("投影片")
        if missing:
            session.title = f"⚠️ {session.title}"

    def _save_current_transcript(self) -> None:
        """Ctrl+S：把目前 active session 的講稿存成 .txt。
        若已有 transcript_path 直接覆寫；否則 Save As 對話框。"""
        session = self.session_manager.active
        if session is None:
            return
        if self.transcript is None:
            return
        text_to_save = session.modified_text or self.view.toPlainText()
        target = session.transcript_path
        if not target or not Path(target).exists():
            target, _ = QFileDialog.getSaveFileName(
                self, "另存新檔",
                str(Path(self.cfg.last_transcript_path).parent
                    if self.cfg.last_transcript_path else Path.home()),
                "純文字 (*.txt);;Markdown (*.md);;所有檔案 (*.*)",
            )
            if not target:
                return
        try:
            Path(target).write_text(text_to_save, encoding="utf-8")
            session.transcript_path = target
            session.modified_text = ""   # 已寫回檔案，不再需要備份
            session.dirty = False
            session.title = Path(target).name
            self.cfg = dataclass_replace(self.cfg, last_transcript_path=target)
            save_config(self.cfg)
            self.session_manager.sessions_changed.emit()
            self.status_recognized.setText(f"✅ 已儲存：{target}")
        except Exception as e:
            QMessageBox.critical(self, "儲存失敗", f"{e}")

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

    def _open_slides(self) -> None:
        """選擇 PDF/PPTX 並載入到右側預覽。"""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "載入投影片",
            str(Path(self.cfg.last_transcript_path).parent
                if self.cfg.last_transcript_path else Path.home()),
            "投影片 (*.pdf *.pptx *.ppt);;PDF (*.pdf);;PowerPoint (*.pptx *.ppt);;所有檔案 (*.*)",
        )
        if path:
            self.load_slides(path)

    def load_slides(self, path: str | Path) -> None:
        """載入投影片檔（PDF/PPTX → 轉 PDF → 渲染），掛到 active session。"""
        session = self._ensure_active_session()
        p = Path(path)
        pdf_path: Path
        if p.suffix.lower() in (".pptx", ".ppt"):
            try:
                self.status_recognized.setText("⏳ 正在轉換 PPTX → PDF…")
                QApplication.processEvents()
                pdf_path = convert_pptx_to_pdf(p)
            except PptxConversionError as e:
                QMessageBox.warning(self, "無法轉檔", str(e))
                return
            except Exception as e:
                QMessageBox.critical(self, "轉檔失敗", f"{e}")
                return
        elif p.suffix.lower() == ".pdf":
            pdf_path = p
        else:
            QMessageBox.warning(self, "不支援的格式",
                                f"只支援 PDF / PPTX，收到 {p.suffix}")
            return

        try:
            deck = load_slide_deck(pdf_path)
        except Exception as e:
            QMessageBox.critical(self, "載入失敗", f"無法開啟投影片:\n{e}")
            return

        # 關閉舊 deck 釋放資源
        if session.slide_deck is not None:
            session.slide_deck.close()
        session.slide_deck = deck
        session.slides_path = str(p)
        self.slide_deck = deck
        session.slide_deck = deck
        # 嵌入到 PrompterView（左文右圖）
        self.view.set_slide_deck(deck)
        self._sync_slide_to_current_sentence()
        QTimer.singleShot(200, self._update_slide_label_from_viewport)
        self.status_recognized.setText(
            f"✅ 投影片已載入 ({deck.page_count} 頁)"
        )
        self.session_manager.sessions_changed.emit()   # tab tooltip 會更新

    # ---------- 統一捲動（scroll lock） ----------

    def _on_left_scroll(self, value: int) -> None:
        """左側捲動 → 同步右側 scrollbar value（1:1）。"""
        if self._scroll_lock_guard:
            return
        self._scroll_lock_guard = True
        try:
            right_sb = self.slide_preview.scroll_area().verticalScrollBar()
            right_sb.setValue(value)
        finally:
            self._scroll_lock_guard = False
        self._update_divider_overlay()

    def _on_right_scroll(self, value: int) -> None:
        """右側捲動 → 同步左側 scrollbar value。"""
        if self._scroll_lock_guard:
            return
        self._scroll_lock_guard = True
        try:
            self.view.verticalScrollBar().setValue(value)
        finally:
            self._scroll_lock_guard = False
        self._update_divider_overlay()

    def _on_slide_page_scrolled(self, page_no: int) -> None:
        """右側 slide 面板呼叫的 page_changed signal（目前僅用於狀態顯示）。"""
        # 新版統一捲動後不需做跨側同步，保留空方法避免 connect 失敗
        pass

    # ---------- 頁高對齊（pad-to-align） ----------

    def _compute_page_left_blocks(self) -> list[int]:
        """找出每頁最後一個 block 的 block number（對應 `---` 分隔行；若該頁不是最後一頁則有 `---`）。
        最後一頁沒有 `---`，回傳其最後一個 block。
        """
        if self.transcript is None or not self.transcript.pages:
            return []
        doc = self.view.document()
        pages = self.transcript.pages
        result: list[int] = []
        for i, page in enumerate(pages):
            if i + 1 < len(pages):
                # 該頁結束 → 下一頁的 sentence_start-1 的 char → 的 block；
                # 實務上 `---` 行位於兩頁之間，取下一頁 first sentence 的 block - 1
                next_page = pages[i + 1]
                if 0 <= next_page.sentence_start < len(self.transcript.sentences):
                    next_start_char = self.transcript.sentences[next_page.sentence_start].start
                    # 找 next_start_char 所在 block 的上一個 block
                    cursor = QTextCursor(doc)
                    cursor.setPosition(max(0, next_start_char - 1))
                    result.append(cursor.block().blockNumber())
                    continue
            # 最後一頁 → 用最後一個 block
            result.append(doc.blockCount() - 1)
        return result

    def _align_page_heights(self) -> None:
        """依左右兩側每頁的自然高度取 max，將較短側加 padding 對齊。"""
        if self.transcript is None or not self.transcript.pages:
            self.divider_overlay.set_boundaries([])
            return
        if self.slide_deck is None:
            self.divider_overlay.set_boundaries([])
            return
        pages = self.transcript.pages
        n = min(len(pages), self.slide_deck.page_count)
        if n == 0:
            return

        # 左側：先清掉之前的 padding
        self.view.clear_all_block_bottom_paddings()

        # 算左每頁高度（需 document layout 就緒）
        doc = self.view.document()
        doc.documentLayout().documentSize()  # 強制 layout
        left_page_tops: list[int] = []
        for page in pages[:n]:
            if 0 <= page.sentence_start < len(self.transcript.sentences):
                char = self.transcript.sentences[page.sentence_start].start
                left_page_tops.append(self.view.char_document_y(char))
            else:
                left_page_tops.append(0)
        # 左每頁高度 = tops[i+1] - tops[i]；最後一頁 = doc_size - tops[-1]
        doc_h = int(doc.documentLayout().documentSize().height())
        left_heights: list[int] = []
        for i in range(n):
            if i + 1 < n:
                left_heights.append(max(0, left_page_tops[i + 1] - left_page_tops[i]))
            else:
                left_heights.append(max(0, doc_h - left_page_tops[i]))

        # 右側：先清 padding
        self.slide_preview.set_page_bottom_paddings([0] * n)
        right_heights = self.slide_preview.page_natural_heights()[:n]

        # 每頁 target = max(left, right)
        left_pads: list[int] = []
        right_pads: list[int] = []
        for i in range(n):
            left_h = left_heights[i] if i < len(left_heights) else 0
            right_h = right_heights[i] if i < len(right_heights) else 0
            target = max(left_h, right_h)
            left_pads.append(max(0, target - left_h))
            right_pads.append(max(0, target - right_h))

        # 套 padding
        block_nums = self._compute_page_left_blocks()
        for i, pad in enumerate(left_pads):
            if i >= len(block_nums):
                break
            self.view.set_block_bottom_padding(block_nums[i], pad)
        self.slide_preview.set_page_bottom_paddings(right_pads)

        # 觸發重繪 overlay
        QTimer.singleShot(20, self._update_divider_overlay)

    def _update_divider_overlay(self) -> None:
        """依目前 scrollbar 位置 + 每頁累加高度 → 算出各頁邊界的 viewport Y → 設定 overlay。"""
        if self.transcript is None or not self.transcript.pages:
            self.divider_overlay.set_boundaries([])
            return
        if self.slide_deck is None:
            self.divider_overlay.set_boundaries([])
            return
        pages = self.transcript.pages
        n = min(len(pages), self.slide_deck.page_count)
        scroll_val = self.view.verticalScrollBar().value()
        ov_geo = self.divider_overlay.geometry()
        # 確保 overlay 蓋滿 content_splitter
        if ov_geo.width() != self.content_splitter.width() or ov_geo.height() != self.content_splitter.height():
            self.divider_overlay.setGeometry(
                0, 0, self.content_splitter.width(), self.content_splitter.height()
            )
        boundaries: list[tuple[int, int, int]] = []
        for i, page in enumerate(pages[:n]):
            if 0 <= page.sentence_start < len(self.transcript.sentences):
                char = self.transcript.sentences[page.sentence_start].start
                doc_y = self.view.char_document_y(char)
                viewport_y = doc_y - scroll_val
                # 只在頁開始（不是第一頁 top=0）畫
                if i > 0 and -20 <= viewport_y <= self.divider_overlay.height() + 20:
                    boundaries.append((viewport_y, page.number, n))
        self.divider_overlay.set_boundaries(boundaries)

    def _open_slide_viewer(self, page_no: int) -> None:
        """雙擊 slide → 彈出大圖檢視視窗。"""
        if self.slide_deck is None:
            return
        dlg = SlideViewerDialog(self.slide_deck, page_no, self)
        dlg.exec()

    def _goto_speech_position(self) -> None:
        """把視窗捲回目前 engine 的辨識位置（不動 engine）。"""
        if self.transcript is None:
            return
        char = self.engine.current_global_char
        self.view.scroll_to_char(char)
        self.status_recognized.setText(f"📍 已回到念稿位置（char {char}）")

    def _update_slide_label_from_viewport(self, *_args) -> None:
        """依 viewport 目前滾動位置更新右上角「第 X / M 頁」。"""
        if self.transcript is None:
            return
        boundaries = getattr(self.view, "_page_boundaries", [])
        if not boundaries:
            # 無 slide 載入時：回退用 engine 位置
            idx = self.engine.current_sentence_index
            page = self.transcript.page_of_sentence(idx)
            if page and self.transcript.pages:
                self.time_panel.set_slide(
                    page.number, len(self.transcript.pages), page.title,
                )
            return
        # 以 viewport 最上面看得到的位置判定「當前頁」
        scroll_val = self.view.verticalScrollBar().value()
        anchor_y = scroll_val + 20
        current_page_no = 1
        for i, (top_y, bottom_y) in enumerate(boundaries):
            if top_y <= anchor_y < bottom_y:
                current_page_no = i + 1
                break
            if anchor_y >= bottom_y:
                current_page_no = i + 1
        total = len(boundaries)
        # 取該頁標題（若在講稿範圍內）
        title = ""
        if (
            self.transcript.pages
            and 1 <= current_page_no <= len(self.transcript.pages)
        ):
            title = self.transcript.pages[current_page_no - 1].title
        self.time_panel.set_slide(current_page_no, total, title)

    def _find_sentence_at_char(self, char: int) -> int:
        """找出 char 位置位於第幾句（回傳 sentence index，0-based）。"""
        if self.transcript is None or not self.transcript.sentences:
            return 0
        for i, s in enumerate(self.transcript.sentences):
            if s.start <= char < s.end:
                return i
            if char < s.start:
                return max(0, i - 1)
        return len(self.transcript.sentences) - 1

    def _on_slide_page_requested(self, page_no: int) -> None:
        """使用者點縮圖 → 講稿跳到該頁對應的第一句。"""
        if self.transcript is None or not self.transcript.pages:
            return
        if page_no < 1 or page_no > len(self.transcript.pages):
            # 超出講稿頁數（投影片比講稿多），只顯示投影片即可，不動講稿
            return
        page = self.transcript.pages[page_no - 1]
        # page.sentence_start 是 1-based？不，Transcript.pages 的 sentence_start 是 0-based index
        result = self.engine.jump_to_sentence(page.sentence_start)
        self.view.set_position(result.global_char_pos, animate=False)

    def _sync_slide_to_current_sentence(self) -> None:
        """講稿目前句 → 對應 PDF 頁 → 切換右側大圖。"""
        if self.slide_deck is None or self.transcript is None:
            return
        idx = self.engine.current_sentence_index
        page = self.transcript.page_of_sentence(idx)
        if page is None:
            return
        # 講稿頁碼 ↔ PDF 頁碼 1:1 對應
        if 1 <= page.number <= self.slide_deck.page_count:
            self.slide_preview.show_page(page.number)

    def _paste_text(self) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self, "貼上講稿", "請貼上您的講稿文字："
        )
        if ok and text.strip():
            transcript = load_from_string(text)
            self._apply_transcript(transcript, source_path="")

    def _apply_transcript(self, transcript: Transcript, *, source_path: str) -> None:
        """把 transcript 套用到目前 active session。

        source_path == "" → 通常是「編輯後重新 parse」，保留原本的 transcript_path 不覆蓋。
        """
        if not transcript.sentences:
            QMessageBox.warning(self, "講稿為空", "未能解析出任何句子。")
            return
        session = self._ensure_active_session()
        session.transcript = transcript
        # 只有「明確傳入新路徑」才覆蓋；空字串代表只是重新 parse，保留原路徑
        if source_path:
            session.transcript_path = source_path
            session.title = Path(source_path).name
        elif not session.transcript_path:
            session.title = session.title or "未命名"
        session.current_global_char = transcript.sentences[0].start
        session.current_sentence_index = 0
        session.skipped_ranges = []
        session.format_spans = []
        # 綁到 UI
        self.transcript = transcript
        self.engine.set_transcript(transcript)
        self.view.set_text(transcript.full_text)
        self.view.set_position(transcript.sentences[0].start, animate=False)
        self.view.clear_skipped()
        # 更新 initial_prompt
        prompt = transcript.full_text[:200]
        self.recognizer.update_prompt(prompt)
        # 持久化設定 last_transcript_path（相容舊設定）
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
        # 通知 tab bar 重繪標題
        self.session_manager.sessions_changed.emit()
        # 初始更新右上角頁碼
        QTimer.singleShot(100, self._update_slide_label_from_viewport)

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
        # 投影片頁碼顯示由 viewport 滾動事件主導（_update_slide_label_from_viewport），
        # 此處不再直接 set_slide 以避免「engine 與滾動」互相打架。
        # 但引擎推進後會連帶觸發 view 滾動 → scrollbar.valueChanged → label 自動更新。

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

    # ---------- 演講記錄（錄音 + 截圖） ----------

    def _toggle_recording(self, checked: bool) -> None:
        if checked:
            if not self.recorder.is_available():
                QMessageBox.warning(
                    self, "錄影不可用",
                    "找不到 ffmpeg。\n請執行：\n    pip install imageio-ffmpeg",
                )
                self.act_record.blockSignals(True)
                self.act_record.setChecked(False)
                self.act_record.blockSignals(False)
                return
            # 選來源
            dlg = RecordSourceDialog(self, self)
            if dlg.exec() != dlg.DialogCode.Accepted:
                self.act_record.blockSignals(True)
                self.act_record.setChecked(False)
                self.act_record.blockSignals(False)
                return
            target = dlg.result_target()
            if target is None:
                self.act_record.blockSignals(True)
                self.act_record.setChecked(False)
                self.act_record.blockSignals(False)
                return

            # 啟麥克風
            auto_started_audio = False
            if not self.audio.is_running():
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
                auto_started_audio = True

            ok = self.recorder.start(default_recording_root(), target=target, fps=30)
            if not ok:
                self.act_record.blockSignals(True)
                self.act_record.setChecked(False)
                self.act_record.blockSignals(False)
                if auto_started_audio:
                    self.audio.stop()
                return
            self.act_record.setText("⏹ 停止錄影")
            self.status_recording.setText("🔴 錄影中 00:00")
        else:
            self.recorder.stop()
            self.act_record.setText("⏺ 錄影")
            self.status_recording.setText("")

    def _on_record_started(self, path: str) -> None:
        self.status_recognized.setText(f"🔴 錄影啟動：{path}")

    def _on_record_tick(self, elapsed: float) -> None:
        s = int(elapsed)
        hh = s // 3600
        mm = (s % 3600) // 60
        ss = s % 60
        if hh > 0:
            self.status_recording.setText(f"🔴 錄影中 {hh:d}:{mm:02d}:{ss:02d}")
        else:
            self.status_recording.setText(f"🔴 錄影中 {mm:02d}:{ss:02d}")

    def _on_record_muxing_started(self) -> None:
        """視訊流完成、開始 mux → 顯示非阻塞進度對話框。"""
        self.status_recording.setText("⏳ 合成影音中…")
        self._mux_dialog = QProgressDialog(
            "⏳ 正在合成 MP4 影音檔…\n請稍候（約 5-15 秒）",
            None, 0, 0, self,
        )
        self._mux_dialog.setWindowTitle("錄影合成")
        self._mux_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._mux_dialog.setCancelButton(None)
        self._mux_dialog.setMinimumDuration(0)
        self._mux_dialog.show()

    def _on_record_stopped(self, mp4_path: str) -> None:
        """mux 完成。關進度對話框、顯示結果。"""
        if self._mux_dialog is not None:
            self._mux_dialog.close()
            self._mux_dialog = None
        self.status_recording.setText("")
        if mp4_path:
            reply = QMessageBox.information(
                self, "錄影已儲存",
                f"MP4 影音檔已儲存於:\n{mp4_path}",
                QMessageBox.StandardButton.Ok,
            )
            # 附：可點「開啟資料夾」選項（之後加）

    def _on_record_error(self, msg: str) -> None:
        if self._mux_dialog is not None:
            self._mux_dialog.close()
            self._mux_dialog = None
        QMessageBox.warning(self, "錄影錯誤", msg)
        self.act_record.blockSignals(True)
        self.act_record.setChecked(False)
        self.act_record.blockSignals(False)
        self.act_record.setText("⏺ 錄影")
        self.status_recording.setText("")

    def _toggle_edit_mode(self, checked: bool) -> None:
        """切換編輯模式：
        - 進入：記住**視窗可視位置**（不是 engine 位置，因為使用者可能沒開始辨識）
        - 離開：保存格式 → 重 parse → 還原格式 + scroll 位置
        """
        if checked:
            if self.audio.is_running():
                self._pause()
            # 用「視窗頂端實際看到的 char」為準（使用者視角的位置）
            self._pre_edit_char = self.view.visible_top_char()
            self._pre_edit_scroll = self.view.verticalScrollBar().value()
            logger.info(
                "進入編輯模式：visible_top_char=%d, scroll=%d",
                self._pre_edit_char, self._pre_edit_scroll,
            )
            # 若 slide > 講稿頁數：自動補 placeholder block 讓每頁都能點擊編輯
            inserted_n = self._expand_transcript_for_slides()
            self.view.set_edit_mode(True)
            if inserted_n > 0:
                self.status_recognized.setText(
                    f"✏ 編輯模式（已為 {inserted_n} 張多餘的投影片追加空白講稿區塊，可點擊輸入）"
                )
            else:
                self.status_recognized.setText("✏ 編輯模式：可直接修改講稿，再按一次離開")
        else:
            # 1) dump 格式
            formats_to_keep = self.view.dump_format_spans()
            # 2) set_edit_mode(False) → _on_transcript_edited → set_text（清 document）
            self.view.set_edit_mode(False)
            # 3) 重新套格式到新 document
            if formats_to_keep:
                try:
                    self.view.restore_format_spans(formats_to_keep)
                except Exception as e:
                    logger.warning("restore formats 失敗：%s", e)
            # 4) 還原視窗位置：
            #    優先用 pre_edit_char（char-level，更穩定）
            #    若不行 fallback 到 scroll value
            try:
                pre_char = getattr(self, "_pre_edit_char", 0)
                pre_scroll = getattr(self, "_pre_edit_scroll", 0)
                if pre_char > 0 and self.transcript is not None:
                    pre_char = min(pre_char, len(self.transcript.full_text) - 1)
                    # 把視窗捲到那個 char（不動 engine）
                    self.view.scroll_to_char(pre_char)
                elif pre_scroll > 0:
                    self.view.verticalScrollBar().setValue(pre_scroll)
            except Exception as e:
                logger.warning("還原位置失敗：%s", e)
            # 5) 保存到 session
            if self.session_manager.active is not None:
                self.session_manager.active.format_spans = formats_to_keep

    def _expand_transcript_for_slides(self) -> int:
        """若 slide_deck 的頁數 > 講稿頁數，為每張多餘的 slide 追加一個空白 `---` + `# Slide N` 區塊。
        回傳追加的頁數。只影響講稿文字（不動 view 以外的資料）。

        使用時機：進入編輯模式前。
        """
        if self.transcript is None or self.slide_deck is None:
            return 0
        transcript_pages = max(1, len(self.transcript.pages))
        total_slides = self.slide_deck.page_count
        if total_slides <= transcript_pages:
            return 0
        current_text = self.view.toPlainText().rstrip()
        extra_parts: list[str] = []
        placeholder = "（請在此輸入此頁講稿）"
        for i in range(transcript_pages + 1, total_slides + 1):
            extra_parts.append(f"\n\n---\n\n# Slide {i}\n\n{placeholder}\n")
        new_text = current_text + "".join(extra_parts)
        # 直接 setPlainText（view 在進入 edit 前會清 formats，是預期行為）
        self.view.set_text(new_text)
        # 重新 parse 並套用（不動位置；位置在 _toggle_edit_mode 最後統一處理）
        from ..core.transcript_loader import load_from_string
        transcript = load_from_string(new_text)
        if self.session_manager.active is not None:
            self.session_manager.active.transcript = transcript
        self.transcript = transcript
        self.engine.set_transcript(transcript)
        return total_slides - transcript_pages

    def _insert_annotation(self) -> None:
        """在游標處插入 <!-- ... --> 註解（僅編輯模式可用）。"""
        if not self.view.is_edit_mode():
            return
        self.view.insert_annotation_at_cursor("")

    def _clear_all_formatting(self) -> None:
        """救援按鈕：清除整篇文字的粗體/斜體/底線/螢光筆，並清除 session 中保存的 format_spans。"""
        self.view.clear_all_formatting()
        active = self.session_manager.active
        if active is not None:
            active.format_spans = []
            active.dirty = True
        self.status_recognized.setText("🧽 已清除整篇格式")

    def _compact_whitespace(self) -> None:
        """一鍵清理多餘空白（僅編輯模式可用）。"""
        if not self.view.is_edit_mode():
            return
        self.view.compact_whitespace()
        self.status_recognized.setText("🧹 已清理多餘空白")

    def _on_edit_mode_changed(self, enabled: bool) -> None:
        self.edit_toolbar.setVisible(enabled)
        for act in (
            self.act_insert_annotation,
            self.act_compact_ws,
            self.act_bold,
            self.act_italic,
            self.act_underline,
            self.act_highlight,
            self.act_clear_fmt,
            self.act_clear_all_fmt,
        ):
            act.setEnabled(enabled)
        if enabled:
            self.act_edit_mode.setText("✏ 編輯模式 (ON)")
        else:
            self.act_edit_mode.setText("✏ 編輯模式")
        # 保持 checkbox 同步（若從程式端呼叫也能一致）
        if self.act_edit_mode.isChecked() != enabled:
            self.act_edit_mode.blockSignals(True)
            self.act_edit_mode.setChecked(enabled)
            self.act_edit_mode.blockSignals(False)

    def _on_transcript_edited(self, new_text: str) -> None:
        """使用者離開編輯模式 → 重新 parse、存到 session.modified_text、標記 dirty。"""
        if not new_text.strip():
            return
        transcript = load_from_string(new_text)
        if not transcript.sentences:
            QMessageBox.warning(self, "講稿為空", "編輯後未能解析出任何句子，已保留原始內容。")
            if self.transcript is not None:
                self.view.set_text(self.transcript.full_text)
            return
        self._apply_transcript(transcript, source_path="")
        # 保存到 session + 標記 dirty（避免關閉後遺失）
        active = self.session_manager.active
        if active is not None:
            active.modified_text = new_text
            active.dirty = True
            self.session_manager.sessions_changed.emit()  # 讓 tab 標題若需要可加 *
        self.status_recognized.setText("✅ 已套用修改後的講稿（未儲存到原檔，可按 Ctrl+S）")

    def _on_font_size_spinbox(self, size: int) -> None:
        if self.view.font_size() != size:
            self.view.set_font_size(size)
        self.cfg = dataclass_replace(self.cfg, font_size=size)
        save_config(self.cfg)

    def _sync_font_spinbox(self, size: int) -> None:
        """view 字型變更（如 Ctrl+wheel）→ 更新工具列 spinbox。"""
        if hasattr(self, "sb_font_size") and self.sb_font_size.value() != size:
            self.sb_font_size.blockSignals(True)
            self.sb_font_size.setValue(size)
            self.sb_font_size.blockSignals(False)

    def _on_target_toggled(self, checked: bool) -> None:
        # 用 QAction 控制 visibility 才可靠（setVisible on widget 在 QToolBar 內不穩定）
        self._sb_target_min_action.setVisible(checked)
        if checked:
            minutes = self.sb_target_min.value()
            self.timer_ctrl.set_target_seconds(minutes * 60)
            self.cfg = dataclass_replace(self.cfg, target_duration_sec=minutes * 60)
        else:
            self.timer_ctrl.set_target_seconds(0)
            self.cfg = dataclass_replace(self.cfg, target_duration_sec=0)
        save_config(self.cfg)

    def _on_target_minutes_changed(self, minutes: int) -> None:
        if self.cb_target.isChecked():
            self.timer_ctrl.set_target_seconds(minutes * 60)
            self.cfg = dataclass_replace(self.cfg, target_duration_sec=minutes * 60)
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
        # 套用穩定性模式 + 最大跳段範圍（所有 session 的 engine 都要同步）
        for s in self.session_manager.sessions:
            if s.engine is not None:
                s.engine.apply_stability_mode(getattr(self.cfg, "stability_mode", "balanced"))
                s.engine.set_max_forward_range(
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

    # ---------- 拖拉檔案支援 ----------

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        mime = event.mimeData()
        if mime.hasUrls():
            # 檢查副檔名是否支援
            for url in mime.urls():
                p = Path(url.toLocalFile())
                if p.suffix.lower() in (
                    ".txt", ".md", ".markdown", ".docx",
                    ".pdf", ".pptx", ".ppt",
                ):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        mime = event.mimeData()
        if not mime.hasUrls():
            return
        transcript_exts = (".txt", ".md", ".markdown", ".docx")
        slide_exts = (".pdf", ".pptx", ".ppt")
        for url in mime.urls():
            p = Path(url.toLocalFile())
            suf = p.suffix.lower()
            if suf in transcript_exts:
                self.load_file(str(p))
            elif suf in slide_exts:
                self.load_slides(str(p))
        event.acceptProposedAction()

    def closeEvent(self, event) -> None:
        # 先把當前 view 狀態存回 active session（編輯中可能還沒 flush）
        if self._bound_session_id:
            active = self.session_manager.get(self._bound_session_id)
            if active is not None:
                self._save_view_state_to(active)
        # 檢查有無 dirty（未存回 .txt 檔的編輯）
        dirty_sessions = [s for s in self.session_manager.sessions if s.dirty]
        if dirty_sessions:
            from PySide6.QtWidgets import QMessageBox as _MB
            ret = _MB.question(
                self, "未儲存的編輯",
                f"{len(dirty_sessions)} 個分頁有編輯未存成檔案。\n"
                "仍會自動保存到 app 內部（下次開啟看得到），但不會寫回你的 .txt 檔。\n\n"
                "要現在儲存為 .txt 檔嗎？",
                _MB.StandardButton.Save | _MB.StandardButton.Discard | _MB.StandardButton.Cancel,
                _MB.StandardButton.Save,
            )
            if ret == _MB.StandardButton.Cancel:
                event.ignore()
                return
            if ret == _MB.StandardButton.Save:
                for s in dirty_sessions:
                    # 切到該 session 再存
                    self.session_manager.set_active(s.session_id)
                    QApplication.processEvents()
                    self._save_current_transcript()
        if self.recorder.is_running():
            try:
                self.recorder.stop()
            except Exception:
                pass
        self.audio.stop()
        self.recognizer.stop()
        self.timer_ctrl.pause()
        # 先把目前 view 狀態寫回 active session
        if self._bound_session_id:
            active = self.session_manager.get(self._bound_session_id)
            if active is not None:
                self._save_view_state_to(active)
        # 儲存 sessions.json
        try:
            self.session_manager.save_to_disk(default_sessions_path())
        except Exception as e:
            logger.warning("儲存 sessions.json 失敗: %s", e)
        # 關閉所有 slide decks
        for s in self.session_manager.sessions:
            if s.slide_deck is not None:
                try:
                    s.slide_deck.close()
                except Exception:
                    pass
        self.cfg = dataclass_replace(self.cfg, window_geometry=bytes(self.saveGeometry()))
        save_config(self.cfg)
        super().closeEvent(event)
