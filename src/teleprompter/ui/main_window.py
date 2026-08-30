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
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QDialog,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QSpinBox,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QToolButton,
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
from .empty_state import EmptyStateOverlay
from .floating_timer import FloatingTimer
from .icons import icon
from .module_switch import ModuleToggle
from .notes_import_dialog import NotesImportDialog
from .record_source_dialog import RecordSourceDialog
from .session_tab_bar import SessionTabBar
from .settings_dialog import SettingsDialog
from .slide_mode_view import SlideModeView
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

        self.elapsed_label = QLabel("00:00 / --:--")
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
            self.slide_label.setText(f"Slide {current}/{total} · {title}")
        else:
            self.slide_label.setText(f"Slide {current}/{total}")
        self.slide_label.show()

    def update_state(self, state) -> None:
        elapsed = format_mmss(state.elapsed_ms)
        target = format_mmss(state.target_ms) if state.target_ms > 0 else "--:--"
        self.elapsed_label.setText(f"{elapsed} / {target}")

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

        from .. import __version__
        self._app_title = f"智能語音提詞機 v{__version__}"
        self.setWindowTitle(self._app_title)
        # 直接把講稿或投影片拖進視窗就能載入，不必先開檔案對話框
        self.setAcceptDrops(True)
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

        self._cfg_font_family = config.font_family
        self._cfg_font_size = config.font_size
        self._cfg_line_spacing = config.line_spacing
        self._cfg_upcoming_color = config.upcoming_color
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

        # 巢狀 splitter：外=[內層 + Q&A]，內=[縮圖列(左) + 主內容區(中)]
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.content_splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左側縮圖列（只在投影片模式顯示）— 重用 SlidePreviewPanel
        self.slide_preview = SlidePreviewPanel()
        self.slide_preview.hide()
        # 寬度硬 cap：縮圖列不能吃掉主內容區（拖曳 splitter 也不行）
        self.slide_preview.setMinimumWidth(160)
        self.slide_preview.setMaximumWidth(420)
        self.content_splitter.addWidget(self.slide_preview)

        # 中間：QStackedWidget 裝兩種顯示方式
        #   index 0 = PrompterView（滾動式：講稿 / 分割模式）
        #   index 1 = SlideModeView（單頁式：投影片模式）
        self._content_stack = QStackedWidget()
        self._content_stack.addWidget(self.view)
        self.slide_mode_view = SlideModeView()
        self.slide_mode_view.set_font_family(self._cfg_font_family)
        self.slide_mode_view.set_font_size(self._cfg_font_size)
        self.slide_mode_view.set_line_spacing(self._cfg_line_spacing)
        self.slide_mode_view.set_colors(
            upcoming=config.upcoming_color,
            spoken=config.spoken_color,
            current=config.highlight_color,
            skipped=config.skipped_color,
        )
        self._content_stack.addWidget(self.slide_mode_view)
        self._content_stack.setCurrentIndex(0)  # 預設顯示 PrompterView
        self.content_splitter.addWidget(self._content_stack)

        self.content_splitter.setStretchFactor(0, 0)  # 縮圖列固定寬度
        self.content_splitter.setStretchFactor(1, 1)  # 主內容區拉伸

        self.main_splitter.addWidget(self.content_splitter)
        self.qa_panel = QAPanel()
        self.qa_panel.close_qa_mode.connect(self._exit_qa_mode)
        self.qa_panel.language_changed.connect(self._switch_recognizer_language)
        self.view.pages_missing.connect(self._on_pages_missing)
        self.qa_panel.goto_page.connect(self._on_qa_goto_page)
        self.qa_panel.karaoke_toggled.connect(self._on_qa_karaoke_toggled)
        self.qa_panel.qa_path_changed.connect(self._on_qa_path_changed)
        self.qa_panel.set_backup_start_page(self.cfg.qa_backup_start_page)
        self.qa_panel.karaoke_switch.setChecked(self.cfg.qa_karaoke_enabled)
        self._restore_last_qa_library()
        # 降低最小寬度：原本太寬會吃掉講稿區；280px 夠顯示問答欄 + 語言 combo
        self.qa_panel.setMinimumWidth(280)
        self.main_splitter.addWidget(self.qa_panel)
        self.main_splitter.setStretchFactor(0, 4)   # 提詞區再放寬一點
        self.main_splitter.setStretchFactor(1, 1)   # QA 面板較窄
        self.qa_panel.hide()                          # 預設不顯示，按 Q&A 模式才展開
        central_layout.addWidget(self.main_splitter, 1)

        self.setCentralWidget(central_wrap)

        # 允許拖拉檔案到主視窗（不需經由檔案對話框）
        self.setAcceptDrops(True)

        # 載入遮罩（覆蓋在 stack 上 — 不論顯示 PrompterView 或 SlideModeView 都看得到）
        self.loading_overlay = LoadingOverlay(self._content_stack)
        # 空分頁引導：沒有講稿也沒有投影片時鋪在內容區上
        self.empty_state = EmptyStateOverlay(self._content_stack)
        self.empty_state.hide()
        self.empty_state.open_file_requested.connect(self._open_file)
        self.empty_state.paste_requested.connect(self._paste_text)
        self.empty_state.sample_requested.connect(self.load_sample_bundle)
        self.empty_state.recent_requested.connect(self._load_recent_path)
        self.loading_overlay.hide()
        # 標記「使用者按下開始但模型還沒準備好」的 pending 狀態
        self._pending_start: bool = False
        # QA 模式獨立啟動時的 pending：模型就緒後啟動音訊（loopback）
        self._qa_pending_audio_start: bool = False
        self._pages_missing_guard: bool = False
        self.floating_timer: FloatingTimer | None = None

        # ---- 狀態列 ----
        sb = QStatusBar()
        self.status_recognized = QLabel("等待開始…")
        self.status_recognized.setStyleSheet("padding: 0 8px;")
        sb.addWidget(self.status_recognized, 1)

        # 引擎狀態：句子位置 + 信心 + 原因（生產級可見性）
        self.status_engine = QLabel("位置 — / —   信心 —")
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

        # === 右下角檢視模式切換（Word 風格：📄 ⊞ 🖼）===
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #3A3A3A;")
        sb.addPermanentWidget(sep)

        self._view_mode_group = QButtonGroup(self)
        self._view_mode_group.setExclusive(True)
        self.btn_mode_transcript = QToolButton()
        self.btn_mode_transcript.setText("")
        self.btn_mode_transcript.setIcon(icon("doc"))
        self.btn_mode_transcript.setToolTip("講稿模式：文字滿版 (Ctrl+1)")
        self.btn_mode_transcript.setCheckable(True)
        self.btn_mode_transcript.clicked.connect(lambda: self._set_view_mode("transcript"))
        self.btn_mode_split = QToolButton()
        self.btn_mode_split.setText("")
        self.btn_mode_split.setIcon(icon("split"))
        self.btn_mode_split.setToolTip("分割模式：文左圖右 (Ctrl+2)")
        self.btn_mode_split.setCheckable(True)
        self.btn_mode_split.setChecked(True)   # 預設
        self.btn_mode_split.clicked.connect(lambda: self._set_view_mode("split"))
        self.btn_mode_slide = QToolButton()
        self.btn_mode_slide.setText("")
        self.btn_mode_slide.setIcon(icon("slides"))
        self.btn_mode_slide.setToolTip("投影片模式：加左側縮圖列，左右鍵逐頁切換 (Ctrl+3)")
        self.btn_mode_slide.setCheckable(True)
        self.btn_mode_slide.clicked.connect(lambda: self._set_view_mode("slide"))
        for b in (self.btn_mode_transcript, self.btn_mode_split, self.btn_mode_slide):
            self._view_mode_group.addButton(b)
            b.setStyleSheet(
                "QToolButton { font-size: 14px; padding: 2px 8px; border: none; }"
                "QToolButton:checked { background: #4CAF50; color: white; border-radius: 3px; }"
                "QToolButton:hover { background: #3A3A3A; }"
            )
            sb.addPermanentWidget(b)

        # 版面對調按鈕（⇆ / ⇅）：一鍵把文字/投影片左右或上下互換
        self.btn_swap_layout = QToolButton()
        self.btn_swap_layout.setText("")
        self.btn_swap_layout.setIcon(icon("swap"))
        self.btn_swap_layout.setToolTip("對調文字與投影片位置 (橫屏左右互換、直屏上下互換)")
        self.btn_swap_layout.setStyleSheet(
            "QToolButton { font-size: 14px; padding: 2px 8px; border: none; color: #CCCCCC; }"
            "QToolButton:hover { background: #3A3A3A; border-radius: 3px; }"
        )
        self.btn_swap_layout.clicked.connect(self._toggle_layout_swap)
        sb.addPermanentWidget(self.btn_swap_layout)

        self._view_mode: str = "split"
        self._layout_swapped: bool = False

        self.setStatusBar(sb)

        # ---- 工具列 ----
        self._build_toolbar()
        # ---- 選單列（檔案 / 編輯 / 檢視 / 工具）— 放所有 actions，和工具列共用 QAction ----
        self._build_menu_bar()
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
        # 投影片模式下的方向鍵由 SlideModeView 發出
        self.slide_mode_view.page_navigate_requested.connect(self._navigate_page)
        # 標註變動 → 存到 active session（session_manager 會寫到 sessions.json）
        self.slide_mode_view.annotations_changed.connect(self._on_annotations_changed)
        self.view.annotations_changed.connect(self._on_annotations_changed)
        # 選字複製 → 顯示在狀態列
        self.slide_mode_view.text_copied.connect(self._on_text_copied_from_slide)
        # 兩個 view 要求切工具時（例如貼完便利貼回指標）
        self.slide_mode_view.tool_requested.connect(self._set_annotation_tool)
        self.view.tool_requested.connect(self._set_annotation_tool)
        # 左側縮圖列：點縮圖跳頁、方向鍵逐頁、收合按鈕
        self.slide_preview.page_requested.connect(self._on_slide_page_requested)
        self.slide_preview.page_navigate_requested.connect(self._navigate_page)
        self.slide_preview.collapse_requested.connect(self._on_thumbnail_collapse)
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
        # 這兩個開關的 QAction 在工具列/選單建立後才存在 → 放在這裡還原
        if self.cfg.qa_panel_floating:
            self.act_qa_floating.setChecked(True)
        if self.cfg.floating_timer_enabled:
            self.act_floating_timer.setChecked(True)

        QTimer.singleShot(50, self._restore_sessions_or_bootstrap)
        # 啟動時依視窗大小套一次自適應版面
        QTimer.singleShot(0, self._apply_orientation_layout)

        # ---------- 自動儲存 ----------
        # 編輯時 debounce：最後一次變更後 1.5 秒寫檔
        self._autosave_debounce = QTimer(self)
        self._autosave_debounce.setSingleShot(True)
        self._autosave_debounce.setInterval(1500)
        self._autosave_debounce.timeout.connect(self._perform_autosave)
        # 週期性 backup：每 30 秒寫一次（防止編輯外的狀態改動遺失）
        self._autosave_periodic = QTimer(self)
        self._autosave_periodic.setInterval(30_000)
        self._autosave_periodic.timeout.connect(self._perform_autosave)
        self._autosave_periodic.start()

    # ---------- 介面建立 ----------

    def _build_toolbar(self) -> None:
        tb = QToolBar("主工具列")
        tb.setMovable(False)
        # 溢出按鈕（>>）的 hint：確保直屏 / 窄視窗時使用者看得到「還有更多按鈕」
        tb.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        # 有了圖示之後要明說「圖示＋文字」，否則 Qt 預設 IconOnly 會把標籤吃掉
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._main_toolbar = tb
        self.addToolBar(tb)

        self.act_open = QAction("講稿", self)
        self.act_open.setIcon(icon("folder"))
        self.act_open.setToolTip("開啟講稿檔（txt / md / docx）\n也可以直接把檔案拖進視窗")
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.triggered.connect(self._open_file)

        # 儲存、貼上文字 → 只在選單列「檔案」中，不放工具列
        self.act_save = QAction("儲存", self)
        self.act_save.setIcon(icon("save"))
        self.act_save.setToolTip("把目前講稿寫回檔案 (Ctrl+S)")
        self.act_save.setShortcut("Ctrl+S")
        self.act_save.triggered.connect(self._save_current_transcript)

        self.act_paste = QAction("貼上文字", self)
        self.act_paste.setIcon(icon("text"))
        self.act_paste.triggered.connect(self._paste_text)

        self.act_open_slides = QAction("投影片", self)
        self.act_open_slides.setIcon(icon("slides"))
        self.act_open_slides.setToolTip("載入投影片（pdf / pptx）\n也可以直接把檔案拖進視窗")
        self.act_open_slides.triggered.connect(self._open_slides)

        tb.addSeparator()

        # === 播放區（演講進行中最常用，放在檔案區右側）===
        self.act_start = QAction("開始", self)
        self.act_start.setIcon(icon("play"))
        self.act_start.setShortcut(Qt.Key.Key_Space)
        self.act_start.setToolTip("開始 / 暫停語音辨識（Space）")
        self.act_start.triggered.connect(self._toggle_run)
        tb.addAction(self.act_start)

        self.act_goto_speech = QAction("回目前位置", self)
        self.act_goto_speech.setIcon(icon("locate"))
        self.act_goto_speech.setToolTip(
            "捲回你現在念到的那一句（卡拉OK 高亮處）。\n"
            "手動翻看別頁之後，用這個一鍵回到念稿位置（Ctrl+Home）"
        )
        self.act_goto_speech.setShortcut("Ctrl+Home")
        self.act_goto_speech.triggered.connect(self._goto_speech_position)
        tb.addAction(self.act_goto_speech)

        self.act_reset_pos = QAction("回頂", self)
        self.act_reset_pos.setIcon(icon("top"))
        self.act_reset_pos.setToolTip("講稿跳回第一句")
        self.act_reset_pos.triggered.connect(self._reset_position)

        self.act_clear_skipped = QAction("清漏講", self)
        self.act_clear_skipped.setIcon(icon("skip"))
        self.act_clear_skipped.setToolTip("清除漏講標記（Ctrl+Shift+K）")
        self.act_clear_skipped.setShortcut("Ctrl+Shift+K")
        self.act_clear_skipped.triggered.connect(self._clear_skipped)

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

        self.sb_font_size = QSpinBox()
        self.sb_font_size.setRange(12, 120)
        self.sb_font_size.setValue(self.cfg.font_size)
        self.sb_font_size.setSuffix(" pt")
        self.sb_font_size.setToolTip("字型大小")
        self.sb_font_size.setFixedWidth(80)
        self.sb_font_size.valueChanged.connect(self._on_font_size_spinbox)

        self.act_font_bigger = QAction("A+", self)
        self.act_font_bigger.setToolTip("字型放大 (Ctrl++)")
        self.act_font_bigger.triggered.connect(
            lambda: (
                self.view.set_font_size(min(120, self.view.font_size() + 2)),
                self.sb_font_size.setValue(self.view.font_size()),
            )
        )

        tb.addSeparator()

        # === 計時區 ===
        self.cb_target = QCheckBox("目標時長")
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

        # 懸浮計時：放在時間旁邊，因為它就是「時間」這件事的延伸。
        # 它本身也能獨立計時（自己輸入分鐘、倒數/正數、開始暫停），
        # 只想單純計時的場合不必按主程式的「開始」去啟動語音辨識。
        self.act_floating_timer = QAction("懸浮計時", self)
        self.act_floating_timer.setIcon(icon("clock"))
        self.act_floating_timer.setCheckable(True)
        self.act_floating_timer.setShortcut("Ctrl+Shift+F")
        self.act_floating_timer.setToolTip(
            "另開一個永遠置頂的計時視窗（可獨立計時，不必啟動辨識）\n"
            "Ctrl+Shift+F"
        )
        self.act_floating_timer.toggled.connect(self._toggle_floating_timer)
        tb.addAction(self.act_floating_timer)

        self.act_reset_timer = QAction("重置計時", self)
        self.act_reset_timer.setIcon(icon("reset"))
        self.act_reset_timer.setToolTip("計時歸零（R）")
        self.act_reset_timer.setShortcut("R")
        self.act_reset_timer.triggered.connect(self.timer_ctrl.reset)

        tb.addSeparator()

        # === 模式區 ===
        # 編輯模式 toggle 移到 annotation_toolbar（跟文字工具放一起）
        self.act_edit_mode = QAction("編輯模式", self)
        self.act_edit_mode.setIcon(icon("pen"))
        self.act_edit_mode.setShortcut("Ctrl+E")
        self.act_edit_mode.setCheckable(True)
        self.act_edit_mode.toggled.connect(self._toggle_edit_mode)
        # 注意：稍後在 annotation_toolbar 中 addAction，不放主工具列

        self.act_qa_mode = QAction("Q&A 模式", self)
        self.act_qa_mode.setIcon(icon("mic"))
        self.act_qa_mode.setShortcut("Ctrl+Q")
        self.act_qa_mode.setCheckable(True)
        self.act_qa_mode.triggered.connect(self._toggle_qa_mode)

        self.act_record = QAction("錄影", self)
        self.act_record.setIcon(icon("record"))
        self.act_record.setToolTip("開始 / 停止螢幕錄影 + 麥克風 → MP4")
        self.act_record.setCheckable(True)
        self.act_record.triggered.connect(self._toggle_recording)

        # === 模組開關（右段）：開啟才展開對應的模組工具列 ===
        # 用 spacer 把開關推到右邊，讓左段常駐鈕與右段模組區在視覺上分開
        _spacer = QWidget()
        _spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(_spacer)

        self.module_toggles: dict[str, ModuleToggle] = {}
        for key, label in (("edit", "編輯"), ("annot", "標註"),
                           ("qa", "Q&A"), ("follow", "跟讀")):
            tg = ModuleToggle(label)
            tg.toggled.connect(lambda on, k=key: self._on_module_toggled(k, on))
            tb.addWidget(tg)
            self.module_toggles[key] = tg

        tb.addSeparator()

        # === 系統區（最少用，放最右）===
        self.act_fullscreen = QAction("全螢幕", self)
        self.act_fullscreen.setIcon(icon("fullscreen"))
        self.act_fullscreen.setShortcut("F11")
        self.act_fullscreen.triggered.connect(self._toggle_fullscreen)
        tb.addAction(self.act_fullscreen)

        self.act_settings = QAction("設定", self)
        self.act_settings.setIcon(icon("gear"))
        self.act_settings.triggered.connect(self._open_settings)
        tb.addAction(self.act_settings)

        # === 模組工具列 ×4：預設全隱藏，由模組開關控制展開 ===
        # 每條左緣一個標籤（模組名 + 英文小字），與 design/toolbar_mockup_v2.html 對齊
        self.module_bars: dict[str, QToolBar] = {}
        for key, zh, en in (("edit", "編輯", "EDIT"), ("annot", "標註", "ANNOTATE"),
                            ("qa", "Q&A", "Q&A"), ("follow", "跟讀", "FOLLOW")):
            bar = QToolBar(f"{zh}模組", self)
            bar.setObjectName(f"moduleBar_{key}")
            bar.setMovable(False)
            bar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
            bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self.addToolBarBreak()
            self.addToolBar(bar)
            tag = QLabel(f"  {zh}  <span style='color:#5d636b;font-size:10px'>{en}</span>  ")
            tag.setObjectName("moduleTag")
            bar.addWidget(tag)
            bar.setVisible(False)
            self.module_bars[key] = bar


        # === 主工具列延伸（row 2）：直屏時顯示，把 secondary 群組移到這裡 ===
        self._main_toolbar_row2 = QToolBar("主工具列延伸", self)
        self._main_toolbar_row2.setMovable(False)
        self.addToolBarBreak()
        self.addToolBar(self._main_toolbar_row2)
        self._main_toolbar_row2.setVisible(False)

        # 記錄 primary vs secondary 分界：以工具列上第一個 separator 為界。
        # 舊版用 act_clear_skipped 定位，但那顆 action 從未加進工具列，
        # index() 每次都丟 ValueError 走 fallback → secondary 永遠是空的。
        all_acts = list(tb.actions())
        split = next(
            (idx + 1 for idx, a in enumerate(all_acts) if a.isSeparator()),
            len(all_acts),
        )
        self._toolbar_primary_acts = all_acts[:split]
        self._toolbar_secondary_acts = all_acts[split:]

        # === 標註工具列（鉛筆/便利貼/橡皮擦/選字）— 獨立一條，常駐顯示不會被 overflow 吃掉 ===
        self.annotation_toolbar = QToolBar("標註工具", self)
        self.annotation_toolbar.setMovable(False)
        self.addToolBarBreak()
        self.addToolBar(self.annotation_toolbar)

        # ↖ 游標
        self.act_tool_pointer = QAction("游標", self)
        self.act_tool_pointer.setIcon(icon("cursor"))
        self.act_tool_pointer.setToolTip("一般游標（編輯/選取/跳位）(V)")
        self.act_tool_pointer.setCheckable(True)
        self.act_tool_pointer.setChecked(True)
        self.act_tool_pointer.triggered.connect(lambda: self._set_annotation_tool("pointer"))
        self.annotation_toolbar.addAction(self.act_tool_pointer)

        self.annotation_toolbar.addSeparator()

        # 🖊 鉛筆 + 🎨 顏色（放在鉛筆正旁邊）
        self.act_tool_pencil = QAction("鉛筆", self)
        self.act_tool_pencil.setIcon(icon("draw"))
        self.act_tool_pencil.setToolTip(
            "【標註層】自由手繪畫在畫面上（不動講稿文字本身）(P)"
        )
        self.act_tool_pencil.setCheckable(True)
        self.act_tool_pencil.triggered.connect(lambda: self._set_annotation_tool("pencil"))
        self.annotation_toolbar.addAction(self.act_tool_pencil)

        self._current_color = "#FFEB3B"
        self._color_preset_btns: list[QToolButton] = []
        for color, tip in (
            ("#FFEB3B", "黃 (Yellow)"),
            ("#F44336", "紅 (Red)"),
            ("#2196F3", "藍 (Blue)"),
        ):
            btn = QToolButton()
            btn.setToolTip(tip)
            btn.setFixedSize(22, 22)
            btn.setStyleSheet(
                f"QToolButton {{ background: {color}; border: 2px solid #3A3A3A; border-radius: 11px; }}"
                f"QToolButton:checked {{ border: 2px solid #FFFFFF; }}"
            )
            btn.setCheckable(True)
            btn.clicked.connect(lambda _c=False, col=color: self._set_annotation_color(col))
            self._color_preset_btns.append(btn)
            # 不掛進舊的 annotation_toolbar：widget 只能屬於一條工具列，
            # 由「標註」模組列在 _build_toolbar 末端統一 addWidget
        self._color_preset_btns[0].setChecked(True)

        self.btn_color_custom = QToolButton()
        self.btn_color_custom.setText("")
        self.btn_color_custom.setIcon(icon("palette"))
        self.btn_color_custom.setToolTip("自訂顏色（套用到鉛筆 + 螢光筆 + 便利貼）")
        self.btn_color_custom.clicked.connect(self._pick_custom_color)

        # 🖍 螢光筆（從 edit_toolbar 搬過來，共用顏色）
        self.act_highlight = QAction("螢光筆", self)
        self.act_highlight.setIcon(icon("highlight"))
        self.act_highlight.setToolTip(
            "把選取的講稿文字加上背景色（顏色跟鉛筆共用）(Ctrl+H)"
        )
        self.act_highlight.setShortcut("Ctrl+H")
        self.act_highlight.triggered.connect(self.view.toggle_highlight)
        self.annotation_toolbar.addAction(self.act_highlight)

        self.annotation_toolbar.addSeparator()

        # 🗒 便利貼
        self.act_tool_note = QAction("便利貼", self)
        self.act_tool_note.setIcon(icon("note"))
        self.act_tool_note.setToolTip("插入便利貼筆記 (N)")
        self.act_tool_note.setCheckable(True)
        self.act_tool_note.triggered.connect(lambda: self._set_annotation_tool("note"))
        self.annotation_toolbar.addAction(self.act_tool_note)

        self.annotation_toolbar.addSeparator()

        # 🧽 橡皮擦 + 🧹 清除本頁
        self.act_tool_eraser = QAction("橡皮擦", self)
        self.act_tool_eraser.setIcon(icon("eraser"))
        self.act_tool_eraser.setToolTip(
            "【標註層】塗抹式刪除筆劃與便利貼 (E)"
        )
        self.act_tool_eraser.setCheckable(True)
        self.act_tool_eraser.triggered.connect(lambda: self._set_annotation_tool("eraser"))
        self.annotation_toolbar.addAction(self.act_tool_eraser)

        self.act_clear_page = QAction("清除標註", self)
        self.act_clear_page.setIcon(icon("trash"))
        self.act_clear_page.setToolTip(
            "一鍵清除當前頁的所有「標註」（鉛筆筆劃 + 便利貼）；"
            "這跟「清除文字格式」不同 — 不會動到講稿文字本身。"
        )
        self.act_clear_page.triggered.connect(self._clear_current_page_annotations)
        self.annotation_toolbar.addAction(self.act_clear_page)

        # ── 文字工具分群（編輯模式 + 格式 + 結構）──
        self.annotation_toolbar.addSeparator()
        self.annotation_toolbar.addAction(self.act_edit_mode)

        # B / I / U（格式類，直接作用於選取，永遠可用）
        self.act_bold = QAction("B", self)
        self.act_bold.setToolTip("粗體 (Ctrl+B)")
        self.act_bold.setShortcut("Ctrl+B")
        self.act_bold.triggered.connect(self.view.toggle_bold)
        self.annotation_toolbar.addAction(self.act_bold)

        self.act_italic = QAction("I", self)
        self.act_italic.setToolTip("斜體 (Ctrl+I)")
        self.act_italic.setShortcut("Ctrl+I")
        self.act_italic.triggered.connect(self.view.toggle_italic)
        self.annotation_toolbar.addAction(self.act_italic)

        self.act_underline = QAction("U", self)
        self.act_underline.setToolTip("底線 (Ctrl+U)")
        self.act_underline.setShortcut("Ctrl+U")
        self.act_underline.triggered.connect(self.view.toggle_underline)
        self.annotation_toolbar.addAction(self.act_underline)

        self.act_clear_fmt = QAction("清格式", self)
        self.act_clear_fmt.setIcon(icon("clearfmt"))
        self.act_clear_fmt.setToolTip("清除選取範圍的格式 (Ctrl+\\)")
        self.act_clear_fmt.setShortcut("Ctrl+\\")
        self.act_clear_fmt.triggered.connect(self.view.clear_format)
        self.annotation_toolbar.addAction(self.act_clear_fmt)

        self.act_clear_all_fmt = QAction("清文字格式", self)
        self.act_clear_all_fmt.setIcon(icon("clearfmt"))
        self.act_clear_all_fmt.setToolTip(
            "清除整篇文字的粗體/斜體/底線/螢光筆格式（會跳確認視窗）"
        )
        self.act_clear_all_fmt.triggered.connect(self._clear_all_formatting)
        self.annotation_toolbar.addAction(self.act_clear_all_fmt)

        self.annotation_toolbar.addSeparator()

        # 結構類（會改動講稿文字，按下彈確認視窗）
        self.act_insert_annotation = QAction("註解", self)
        self.act_insert_annotation.setIcon(icon("comment"))
        self.act_insert_annotation.setToolTip("⚠ 會改動講稿文字：在游標位置插入備忘註解")
        self.act_insert_annotation.triggered.connect(self._insert_annotation)
        self.annotation_toolbar.addAction(self.act_insert_annotation)

        self.act_compact_ws = QAction("清空白", self)
        self.act_compact_ws.setIcon(icon("broom"))
        self.act_compact_ws.setToolTip("⚠ 會改動講稿文字：移除多餘空白行與行尾空白")
        self.act_compact_ws.triggered.connect(self._compact_whitespace)
        self.annotation_toolbar.addAction(self.act_compact_ws)

        # === 標註工具列延伸（row 2）：直屏時顯示，把文字編輯類工具移到這裡 ===
        # 避免直屏寬度不足時，B/I/U/格式/插入註解/清理空白 被擠到 overflow chevron 看不到
        self.annotation_toolbar_row2 = QToolBar("標註工具延伸", self)
        self.annotation_toolbar_row2.setMovable(False)
        self.addToolBarBreak()
        self.addToolBar(self.annotation_toolbar_row2)
        self.annotation_toolbar_row2.setVisible(False)
        # 直屏時要搬到 row2 的「文字編輯」類 actions
        # 注意：act_edit_mode 留在 row 1（它一定可見且只佔 1 個 button，留 row 1 才不會空著佔一整列）
        # row 2 只放「編輯模式開啟才會顯示」的 actions，這樣關閉編輯模式時 row 2 整個隱藏
        self._annotation_secondary_acts = [
            self.act_bold, self.act_italic, self.act_underline,
            self.act_clear_fmt, self.act_clear_all_fmt,
            self.act_insert_annotation, self.act_compact_ws,
        ]

        # 工具互斥（不含 clear_page / edit_mode；選字改成指標模式下自動偵測 PDF 文字）
        from PySide6.QtGui import QActionGroup
        self._tool_group = QActionGroup(self)
        self._tool_group.setExclusive(True)
        for a in (
            self.act_tool_pointer, self.act_tool_pencil,
            self.act_tool_note, self.act_tool_eraser,
        ):
            self._tool_group.addAction(a)

        # （原獨立的 edit_toolbar 已合併到 annotation_toolbar；保留空的 edit_toolbar
        #   作為測試與舊參考的 no-op 容器）
        # 舊的標註工具列不再常駐：其工具已由「標註」模組列承載（同一批 QAction），
        # 物件保留以維持既有測試與 _layout_annotation_toolbar 的相容性。
        self.annotation_toolbar.setVisible(False)

        self.edit_toolbar = QToolBar("編輯工具列（已併入上方）", self)
        self.edit_toolbar.setMovable(False)
        self.edit_toolbar.hide()

        # 預設隱藏：只有 ✏ 編輯模式 開啟後才顯示 B/I/U/格式/插入註解/清理空白
        # （enabled 永遠為 True —— 快捷鍵 / visible 才是 gate）
        for act in (
            self.act_bold, self.act_italic, self.act_underline,
            self.act_clear_fmt, self.act_clear_all_fmt,
            self.act_insert_annotation, self.act_compact_ws,
        ):
            act.setVisible(False)
            act.setEnabled(True)   # 明確標記為 enabled，讓 isEnabled() 一致回 True

        # 編輯模式切換時重設結果（MD 重新 parse）
        self.view.text_edited.connect(self._on_transcript_edited)
        self.view.edit_mode_changed.connect(self._on_edit_mode_changed)

        # === 模組工具列內容掛載（此時所有 QAction 都已建立）===
        eb = self.module_bars["edit"]
        for act in (self.act_bold, self.act_italic, self.act_underline, self.act_highlight):
            eb.addAction(act)
        eb.addSeparator()
        for act in (self.act_clear_fmt, self.act_clear_all_fmt,
                    self.act_insert_annotation, self.act_compact_ws):
            eb.addAction(act)
        eb.addSeparator()
        eb.addAction(self.act_font_smaller)
        eb.addWidget(self.sb_font_size)
        eb.addAction(self.act_font_bigger)
        _es = QWidget()
        _es.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        eb.addWidget(_es)
        eb.addAction(self.act_save)

        ab = self.module_bars["annot"]
        # 順序＝實際作業流程：選取 → 畫（筆＋顏色）→ 擦掉 → 貼便利貼
        # 橡皮擦與清除緊接顏色之後，跟「畫」是同一組動作，不必跨過便利貼去找
        ab.addAction(self.act_tool_pointer)
        ab.addSeparator()
        ab.addAction(self.act_tool_pencil)
        for btn in self._color_preset_btns:
            ab.addWidget(btn)
        ab.addWidget(self.btn_color_custom)
        ab.addAction(self.act_tool_eraser)
        ab.addAction(self.act_clear_page)
        ab.addSeparator()
        ab.addAction(self.act_tool_note)

        qb = self.module_bars["qa"]
        qb.addAction(self.act_qa_mode)

        fb = self.module_bars["follow"]
        fb.addAction(self.act_reset_pos)
        fb.addAction(self.act_clear_skipped)
        fb.addSeparator()
        fb.addAction(self.act_reset_timer)
        fb.addAction(self.act_record)

    def _on_module_toggled(self, key: str, on: bool) -> None:
        """模組開關 → 展開／收合對應的模組工具列，並連動該模組的實際功能。

        工具列只是外殼；真正的行為仍由既有的 QAction 負責，這裡只做
        「顯隱 + 與既有模式 action 同步」，避免出現兩套互相打架的狀態。
        """
        bar = self.module_bars.get(key)
        if bar is not None:
            bar.setVisible(on)
        if key == "edit":
            # 編輯模組 = 編輯模式；用 setChecked 觸發既有的完整流程
            if self.act_edit_mode.isChecked() != on:
                self.act_edit_mode.setChecked(on)
        elif key == "annot":
            # 收合標註模組時回到游標工具，避免畫布仍停在鉛筆/橡皮擦
            if not on and not self.act_tool_pointer.isChecked():
                self.act_tool_pointer.setChecked(True)
                self._set_annotation_tool("pointer")
        elif key == "qa":
            # _toggle_qa_mode 沒有參數、依面板現況切換 → 只在狀態不符時呼叫一次
            self.act_qa_mode.setChecked(on)
            if self.qa_panel.isVisible() != on:
                self._toggle_qa_mode()
        elif key == "follow":
            self.view.set_karaoke_enabled(on) if hasattr(self.view, "set_karaoke_enabled") else None

    def sync_module_toggle(self, key: str, on: bool) -> None:
        """外部（快捷鍵／選單）改變模式時，讓對應的模組開關跟上，不重複觸發。"""
        tg = self.module_toggles.get(key)
        if tg is None or tg.isChecked() == on:
            return
        tg.switch.blockSignals(True)
        tg.setChecked(on)
        tg.switch.blockSignals(False)
        bar = self.module_bars.get(key)
        if bar is not None:
            bar.setVisible(on)

    def _toggle_qa_floating(self, on: bool) -> None:
        """Q&A 面板：獨立視窗 ↔ 收回主視窗右側。

        獨立視窗時可以擺到第二螢幕或疊在簡報軟體旁邊，講稿區就不必再讓出寬度。
        """
        panel = self.qa_panel
        was_visible = panel.isVisible()
        if on:
            self.main_splitter.setSizes(self.main_splitter.sizes())  # 記住目前比例
            panel.setParent(None)
            panel.setWindowFlags(
                Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint
            )
            panel.setWindowTitle("Q&A 助手")
            geo = self.cfg.qa_panel_geometry
            if geo:
                try:
                    x, y, w, h = (int(v) for v in geo.split(","))
                    panel.setGeometry(x, y, w, h)
                except ValueError:
                    panel.resize(460, 720)
            else:
                panel.resize(460, 720)
            if was_visible:
                panel.show()
        else:
            if panel.isVisible():
                g = panel.geometry()
                self.cfg = dataclass_replace(
                    self.cfg,
                    qa_panel_geometry=f"{g.x()},{g.y()},{g.width()},{g.height()}",
                )
            panel.setWindowFlags(Qt.WindowType.Widget)
            self.main_splitter.addWidget(panel)
            panel.setVisible(was_visible)
        self.cfg = dataclass_replace(self.cfg, qa_panel_floating=bool(on))
        save_config(self.cfg)

    def _toggle_floating_timer(self, on: bool) -> None:
        """開關懸浮計時視窗（切到投影片或瀏覽器時仍看得到剩餘時間）。"""
        if on:
            if getattr(self, "floating_timer", None) is None:
                self.floating_timer = FloatingTimer()
                self.timer_ctrl.state_changed.connect(self.floating_timer.update_state)
                self.floating_timer.geometry_changed.connect(
                    self._on_floating_timer_geometry
                )
                self.floating_timer.closed.connect(
                    lambda: self.act_floating_timer.setChecked(False)
                )
            self._place_floating_timer()
            self.floating_timer.show()
        elif getattr(self, "floating_timer", None) is not None:
            self.floating_timer.hide()
        self.cfg = dataclass_replace(self.cfg, floating_timer_enabled=bool(on))
        save_config(self.cfg)

    def _place_floating_timer(self) -> None:
        """還原上次的位置與大小；沒有記錄就放到主螢幕右上角。"""
        ft = getattr(self, "floating_timer", None)
        if ft is None:
            return
        w, h = self.cfg.floating_timer_w, self.cfg.floating_timer_h
        if w > 0 and h > 0:
            ft.resize(w, h)
        x, y = self.cfg.floating_timer_x, self.cfg.floating_timer_y
        if x >= 0 and y >= 0:
            ft.move(x, y)
            return
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            ft.move(geo.right() - ft.width() - 40, geo.top() + 40)

    def _on_floating_timer_geometry(self, x: int, y: int, w: int, h: int) -> None:
        self.cfg = dataclass_replace(
            self.cfg,
            floating_timer_x=x, floating_timer_y=y,
            floating_timer_w=w, floating_timer_h=h,
        )
        save_config(self.cfg)

    def _build_menu_bar(self) -> None:
        """選單列（Menu Bar）：把不常用的 actions 放進下拉選單，與工具列共用 QAction。"""
        mb = self.menuBar()

        # 檔案
        m_file = mb.addMenu("檔案(&F)")
        m_file.addAction(self.act_open)
        m_file.addAction(self.act_open_slides)
        m_file.addAction(self.act_paste)
        m_file.addSeparator()
        # 最近使用：開啟選單當下才重建內容（檔案可能已被移動或刪除）
        self.menu_recent = m_file.addMenu("最近使用")
        self.menu_recent.aboutToShow.connect(self._rebuild_recent_menu)
        m_file.addSeparator()
        act_sample = QAction("🧪 載入範例（投影片＋講稿＋Q&A）", self)
        act_sample.setStatusTip("一鍵載入內建示範素材，用來快速試玩所有功能")
        act_sample.triggered.connect(self.load_sample_bundle)
        m_file.addAction(act_sample)
        m_file.addSeparator()
        m_file.addAction(self.act_save)
        m_file.addSeparator()
        m_file.addAction(self.act_settings)
        m_file.addSeparator()
        act_quit = QAction("離開(&Q)", self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_quit)

        # 編輯
        m_edit = mb.addMenu("編輯(&E)")
        m_edit.addAction(self.act_edit_mode)
        m_edit.addSeparator()
        m_edit.addAction(self.act_bold)
        m_edit.addAction(self.act_italic)
        m_edit.addAction(self.act_underline)
        m_edit.addAction(self.act_highlight)
        m_edit.addSeparator()
        m_edit.addAction(self.act_clear_fmt)
        m_edit.addAction(self.act_clear_all_fmt)
        m_edit.addSeparator()
        m_edit.addAction(self.act_insert_annotation)
        m_edit.addAction(self.act_compact_ws)

        # 檢視
        m_view = mb.addMenu("檢視(&V)")
        act_mode_transcript = QAction("講稿模式(&1)", self)
        act_mode_transcript.setShortcut(QKeySequence("Ctrl+1"))
        act_mode_transcript.triggered.connect(lambda: self._set_view_mode("transcript"))
        act_mode_split = QAction("分割模式(&2)", self)
        act_mode_split.setShortcut(QKeySequence("Ctrl+2"))
        act_mode_split.triggered.connect(lambda: self._set_view_mode("split"))
        act_mode_slide = QAction("投影片模式(&3)", self)
        act_mode_slide.setShortcut(QKeySequence("Ctrl+3"))
        act_mode_slide.triggered.connect(lambda: self._set_view_mode("slide"))
        m_view.addAction(act_mode_transcript)
        m_view.addAction(act_mode_split)
        m_view.addAction(act_mode_slide)
        m_view.addSeparator()
        self.act_qa_floating = QAction("Q&A 面板獨立視窗", self)
        self.act_qa_floating.setCheckable(True)
        self.act_qa_floating.setStatusTip(
            "把 Q&A 面板拉成獨立視窗，講稿區不必讓出寬度"
        )
        self.act_qa_floating.toggled.connect(self._toggle_qa_floating)
        m_view.addAction(self.act_qa_floating)

        m_view.addAction(self.act_floating_timer)
        m_view.addAction(self.act_fullscreen)
        m_view.addSeparator()
        m_view.addAction(self.act_font_bigger)
        m_view.addAction(self.act_font_smaller)

        # 工具
        m_tools = mb.addMenu("工具(&T)")
        m_tools.addAction(self.act_start)
        m_tools.addAction(self.act_goto_speech)
        m_tools.addAction(self.act_reset_pos)
        m_tools.addSeparator()
        m_tools.addAction(self.act_qa_mode)
        m_tools.addAction(self.act_record)
        m_tools.addSeparator()
        m_tools.addAction(self.act_reset_timer)
        m_tools.addAction(self.act_clear_skipped)

        # 標註
        m_annot = mb.addMenu("標註(&A)")
        m_annot.addAction(self.act_tool_pointer)
        m_annot.addAction(self.act_tool_pencil)
        m_annot.addAction(self.act_tool_note)
        m_annot.addAction(self.act_tool_eraser)
        m_annot.addSeparator()
        m_annot.addAction(self.act_clear_page)

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
        # 檢視模式切換（Word 風格）
        QShortcut(QKeySequence("Ctrl+1"), self, activated=lambda: self._set_view_mode("transcript"))
        QShortcut(QKeySequence("Ctrl+2"), self, activated=lambda: self._set_view_mode("split"))
        QShortcut(QKeySequence("Ctrl+3"), self, activated=lambda: self._set_view_mode("slide"))
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
        # 檢視模式 + 縮圖列寬度
        session.view_mode = getattr(self, "_view_mode", "split")
        sizes = self.content_splitter.sizes()
        if self.slide_preview.isVisible() and len(sizes) >= 2 and sizes[0] > 0:
            session.thumbnail_panel_width = sizes[0]

    def _has_any_content(self) -> bool:
        """這個分頁有沒有東西可以念／看。"""
        has_script = self.transcript is not None and bool(self.transcript.sentences)
        return has_script or self.slide_deck is not None

    def refresh_empty_state(self) -> None:
        """依目前分頁內容決定要不要顯示引導頁。"""
        es = getattr(self, "empty_state", None)
        if es is None:
            return
        if self._has_any_content():
            es.hide()
            return
        es.set_recent(self._recent_paths())
        es.cover(self._content_stack)

    def _recent_paths(self) -> list[str]:
        """最近使用過的講稿與投影片（新到舊）。"""
        joined = f"{self.cfg.recent_scripts}|{self.cfg.recent_slides}"
        seen, out = set(), []
        for item in joined.split("|"):
            item = item.strip()
            if item and item not in seen and Path(item).exists():
                seen.add(item)
                out.append(item)
        return out

    def _remember_recent(self, path: str, *, slides: bool) -> None:
        """把剛載入的檔案推到最近清單最前面，最多留 5 筆。"""
        if not path:
            return
        current = (self.cfg.recent_slides if slides else self.cfg.recent_scripts)
        items = [p for p in current.split("|") if p and p != path]
        items.insert(0, path)
        joined = "|".join(items[:5])
        self.cfg = dataclass_replace(
            self.cfg,
            **({"recent_slides": joined} if slides else {"recent_scripts": joined}),
        )
        save_config(self.cfg)

    def _rebuild_recent_menu(self) -> None:
        """每次展開「最近使用」都重建：檔案可能已被移走或刪除。"""
        menu = getattr(self, "menu_recent", None)
        if menu is None:
            return
        menu.clear()
        paths = self._recent_paths()
        if not paths:
            act = menu.addAction("（尚無紀錄）")
            act.setEnabled(False)
            return
        for path in paths:
            act = menu.addAction(Path(path).name)
            act.setStatusTip(path)
            act.setToolTip(path)
            act.triggered.connect(lambda _=False, p=path: self._load_recent_path(p))

    def _load_recent_path(self, path: str) -> None:
        """引導頁的最近檔案按鈕：依副檔名決定走哪條載入。"""
        if Path(path).suffix.lower() in self.SLIDE_SUFFIXES:
            self.load_slides(path)
        else:
            self.load_file(path)

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
            self.setWindowTitle(f"{self._app_title} — {session.title}")
            page_info = f"，{len(session.transcript.pages)} 頁" if session.transcript.pages else ""
            self.status_recognized.setText(
                f"{session.title} · {len(session.transcript.sentences)} 句{page_info}"
            )
        else:
            self.view.set_text("")
            self.setWindowTitle(f"{self._app_title} — {session.title}")
            self.status_recognized.setText("請先載入講稿")
        self.refresh_empty_state()

        # 嵌入式投影片 → 直接餵給 PrompterView
        self.view.set_slide_deck(session.slide_deck)
        if session.slide_deck is not None:
            self._sync_slide_to_current_sentence()

        # 還原標註（doc 錨點給 PrompterView；slide 錨點在 _set_view_mode("slide") 時載入）
        self.view.set_annotations(
            [a for a in session.annotations if a.anchor == "doc"]
        )

        # 還原檢視模式（放最後，因為 _set_view_mode 會根據 session.slide_deck 決定縮圖列內容）
        self._set_view_mode(session.view_mode or "split")

    def _restore_last_qa_library(self) -> None:
        """啟動時還原上次載入的 Q&A 庫（檔案不在就靜默略過）。"""
        path = (self.cfg.last_qa_path or "").strip()
        if path and Path(path).exists():
            self.qa_panel.load_qa_file(path)

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

    def _sanitize_legacy_format_spans(self, session: Session) -> None:
        """檢查並清掉舊版 bug 殘留的壞 format_spans（覆蓋整篇的那種）。"""
        if not session.format_spans:
            return
        if session.transcript is None or not session.transcript.full_text:
            return
        text_len = len(session.transcript.full_text)
        if text_len <= 0:
            return
        # 計算總覆蓋
        in_doc = [(max(0, s.start), min(text_len, s.end))
                  for s in session.format_spans
                  if s.end <= text_len * 1.05 and s.end > s.start]
        if not in_doc:
            return
        merged: list[tuple[int, int]] = []
        for s, e in sorted(in_doc):
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        total = sum(e - s for s, e in merged)
        if total > text_len * 0.8:
            logger.warning(
                "session %s 的 format_spans 覆蓋 %.0f%% 全文，清除（壞資料救援）",
                session.session_id, 100 * total / text_len,
            )
            session.format_spans = []
            session.dirty = True

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
        # 清理舊版 bug 殘留的壞 format_spans
        self._sanitize_legacy_format_spans(session)

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
        """開啟講稿：直接開檔案對話框。

        以前這裡會先跳一個「檔案 or 貼上文字」的三選一視窗，但「貼上文字」
        在檔案選單與空分頁引導頁都已有獨立入口，多一層只是擋路。
        """
        self._open_file_from_disk()

    def _open_file_from_disk(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "開啟講稿",
            str(Path(self.cfg.last_transcript_path).parent if self.cfg.last_transcript_path else Path.home()),
            "講稿檔案 (*.txt *.md *.markdown *.docx);;所有檔案 (*.*)",
        )
        if path:
            self.load_file(path)

    # ---------- 拖放載入 ----------

    # 副檔名 → 要走哪條載入流程；.md 兩邊都可能，預設當講稿
    SCRIPT_SUFFIXES = (".txt", ".md", ".markdown", ".docx")
    SLIDE_SUFFIXES = (".pdf", ".pptx", ".ppt")

    def _classify_dropped(self, paths: list[str]) -> tuple[list[str], list[str]]:
        """把拖進來的檔案分成「講稿」與「投影片」兩堆，不認得的忽略。"""
        scripts, slides = [], []
        for raw in paths:
            suffix = Path(raw).suffix.lower()
            if suffix in self.SLIDE_SUFFIXES:
                slides.append(raw)
            elif suffix in self.SCRIPT_SUFFIXES:
                scripts.append(raw)
        return scripts, slides

    def dragEnterEvent(self, event) -> None:  # noqa: D102
        if not event.mimeData().hasUrls():
            return
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        scripts, slides = self._classify_dropped(paths)
        if not scripts and not slides:
            return
        event.acceptProposedAction()
        self.status_recognized.setText(self.describe_drop(paths))
        if getattr(self, "empty_state", None) is not None and self.empty_state.isVisible():
            self.empty_state.set_drag_highlight(True)

    def describe_drop(self, paths: list[str]) -> str:
        """拖到視窗上方時的提示：先講清楚放開會載入什麼。"""
        scripts, slides = self._classify_dropped(paths)
        kinds = []
        if scripts:
            kinds.append(f"講稿 {Path(scripts[0]).name}")
        if slides:
            kinds.append(f"投影片 {Path(slides[0]).name}")
        return "放開以載入：" + "　·　".join(kinds)

    def dragMoveEvent(self, event) -> None:  # noqa: D102
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: D102, ARG002
        self.status_recognized.setText("已取消拖放")
        if getattr(self, "empty_state", None) is not None:
            self.empty_state.set_drag_highlight(False)

    def dropEvent(self, event) -> None:  # noqa: D102
        if getattr(self, "empty_state", None) is not None:
            self.empty_state.set_drag_highlight(False)
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if self.load_dropped_paths(paths):
            event.acceptProposedAction()

    def load_dropped_paths(self, paths: list[str]) -> bool:
        """載入拖進來的檔案；回傳是否有東西被載入。"""
        scripts, slides = self._classify_dropped(paths)
        if not scripts and not slides:
            self.status_recognized.setText(
                "這個檔案格式不支援（講稿：txt/md/docx；投影片：pdf/pptx）"
            )
            return False
        loaded = []
        # 先載講稿再載投影片：投影片流程會依講稿現況決定要不要抽取備忘稿
        if scripts:
            self.load_file(scripts[0])
            loaded.append(Path(scripts[0]).name)
        if slides:
            self.load_slides(slides[0])
            loaded.append(Path(slides[0]).name)
        ignored = len(paths) - len(loaded)
        msg = "已載入：" + "　·　".join(loaded)
        if ignored > 0:
            msg += f"（另有 {ignored} 個檔案格式不支援，已略過）"
        self.status_recognized.setText(msg)
        return True

    def load_file(self, path: str | Path) -> None:
        try:
            transcript = load_transcript(path)
        except Exception as e:
            QMessageBox.critical(self, "載入失敗", f"無法載入檔案:\n{e}")
            return
        self._apply_transcript(transcript, source_path=str(path))
        self._remember_recent(str(path), slides=False)
        self.refresh_empty_state()

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

    def load_sample_bundle(self) -> None:
        """一鍵載入內建示範素材：投影片（含備忘稿）＋ Q&A 庫。

        目的是讓第一次使用（或想快速回歸測試）的人不必先自備檔案。
        素材放在 `resources/samples/`，PyInstaller 已把整個 resources 打包進去。
        """
        base = Path(__file__).resolve().parent.parent / "resources" / "samples"
        deck_file = base / "範例投影片_含講稿.pptx"
        qa_file = base / "範例QA問答庫.md"
        if not deck_file.exists():
            QMessageBox.warning(self, "找不到範例",
                                f"範例素材不存在：" + str(deck_file))
            return
        # 先清掉現有講稿，讓抽取流程會被觸發
        self._apply_transcript(load_from_string(""), source_path="")
        self.load_slides(deck_file)
        if qa_file.exists():
            self.qa_panel.load_qa_file(str(qa_file))

    def load_slides(self, path: str | Path) -> None:
        """載入投影片檔（PDF/PPTX → 轉 PDF → 渲染），掛到 active session。"""
        session = self._ensure_active_session()
        p = Path(path)
        pdf_path: Path
        if p.suffix.lower() in (".pptx", ".ppt"):
            try:
                self.status_recognized.setText("正在轉換 PPTX → PDF…")
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
        # 若目前尚無講稿 → 先試著從原始檔抽 speaker notes，讓使用者預覽/編輯後載入；
        # 抽不到或使用者略過 → 退回 scaffold（每頁一個「# Slide N」+ placeholder）
        if self.transcript is None or not self.transcript.full_text.strip():
            if not self._offer_extracted_notes(p, deck):
                self._create_default_transcript_from_slides(deck.page_count)
        # 注意：這裡刻意「不」自動補齊講稿頁數。既有契約是「載入投影片不得改動
        # 使用者現有講稿」（tests/test_view_modes.py 有兩個測試在守）。頁數對齊改在
        # 進入編輯模式時做（_toggle_edit_mode → _expand_transcript_for_slides）。
        # 嵌入到 PrompterView（左文右圖）
        self.view.set_slide_deck(deck)
        self._sync_slide_to_current_sentence()
        QTimer.singleShot(200, self._update_slide_label_from_viewport)
        # 關鍵：剛載入投影片時，若目前是 split 模式 → 重跑 _set_view_mode("split")
        # 讓「直屏 + 有 deck」的分支能切到 SlideModeView（不重跑的話會卡在純 PrompterView）
        # landscape 走 PrompterView 嵌入式（同樣需要重跑來把 deck 綁進去）
        if getattr(self, "_view_mode", "split") == "split":
            self._set_view_mode("split")
        self.status_recognized.setText(
            f"✅ 投影片已載入 ({deck.page_count} 頁)"
        )
        self._remember_recent(str(p), slides=True)
        self.cfg = dataclass_replace(self.cfg, last_slides_path=str(p))
        save_config(self.cfg)
        self.refresh_empty_state()
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
        self.status_recognized.setText(f"已回到念稿位置（char {char}）")

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
            return
        page = self.transcript.pages[page_no - 1]
        result = self.engine.jump_to_sentence(page.sentence_start)
        self.view.set_position(result.global_char_pos, animate=False)
        # 若目前用的是 SlideModeView（slide 模式 or 直屏 split）→ 同步新頁
        if self._content_stack.currentIndex() == 1:
            self.slide_mode_view.set_current_page(page_no - 1)

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
            # 貼上沒有檔名 → 用首句前幾個字當分頁標題，不要停在「未命名」
            active = self.session_manager.active
            if active is not None:
                first = next(
                    (ln.strip() for ln in text.splitlines()
                     if ln.strip() and not ln.strip().startswith("#")),
                    "",
                )
                active.title = (
                    (first[:12] + "…") if len(first) > 12 else (first or "貼上的講稿")
                )
                self.session_manager.sessions_changed.emit()
            self.refresh_empty_state()

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
        # 同步 SlideModeView（若目前在投影片模式，立即可見新文稿）
        self.slide_mode_view.set_transcript(transcript)
        self.slide_mode_view.set_current_page(0)
        self.slide_mode_view.set_karaoke_position(transcript.sentences[0].start)
        self.slide_mode_view.set_skipped_ranges([])
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

    def _ensure_startable_transcript(self) -> bool:
        """按「開始」前確保 transcript 有可念句子；救得回來就救，救不回才擋。

        使用者視角：「畫面上有字就應該能開始」。self.transcript 可能因為
        編輯模式尚未離開、或補頁流程 re-parse 出 0 句而落後於畫面 —— 所以
        檢查失敗時先用畫面文字重建，只有畫面真的沒有可念內文才彈窗。
        """
        def _has_sentences() -> bool:
            return self.transcript is not None and bool(self.transcript.sentences)

        # 還在編輯模式 → 一律先走完整離開流程（re-parse），讓剛打的字生效；
        # 否則就算檢查通過，開念對齊的仍是進編輯前的舊講稿
        if self.act_edit_mode.isChecked():
            self.act_edit_mode.setChecked(False)
        if _has_sentences():
            return True
        # 救援 2：直接用畫面文字重建
        view_text = self.view.toPlainText()
        if view_text.strip():
            rebuilt = load_from_string(view_text)
            if rebuilt.sentences:
                self._apply_transcript(rebuilt, source_path="")
                return _has_sentences()
            QMessageBox.information(
                self, "講稿沒有可念的內文",
                "目前講稿只有標題或分頁符（# Slide N、---），沒有可以念的句子。\n"
                "請在各頁補上講稿內文後再開始。",
            )
            return False
        QMessageBox.information(self, "尚未載入講稿", "請先載入或貼上講稿。")
        return False

    def _start(self) -> None:
        if not self._ensure_startable_transcript():
            return

        if not self.recognizer.is_running():
            # 模型還沒載入 → 顯示遮罩、啟動載入、**不啟動計時**
            self._pending_start = True
            self.loading_overlay.set_status(
                "正在載入語音辨識模型…",
                f"模型：{self.cfg.model_size}（首次載入約需 10-30 秒）\n請稍候，模型就緒後會自動開始計時",
            )
            self.loading_overlay.show_over(self._content_stack)
            self.recognizer.start(
                model_size=self.cfg.model_size,
                language=self.cfg.language,
                compute_type=self.cfg.compute_type,
                initial_prompt=self.transcript.full_text[:200],
            )
            self.act_start.setText("取消")
            self.act_start.setIcon(icon("close"))
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
        # QA 模式 + 啟用 system audio loopback → 抓 Teams/Zoom 的觀眾聲音
        use_loopback = bool(
            self.qa_panel.isVisible() and getattr(self.cfg, "qa_use_system_audio", True)
        )
        self.audio.start(device=device_arg, loopback=use_loopback)
        self.timer_ctrl.start()
        self.act_start.setText("暫停")
        self.act_start.setIcon(icon("pause"))
        self.status_recognized.setText("辨識中…")
        self._pending_start = False

    def _pause(self) -> None:
        # 若模型還在載入中，點「取消」→ 取消 pending 狀態並隱藏遮罩
        if self._pending_start and self.loading_overlay.isVisible():
            self._pending_start = False
            self.loading_overlay.fade_out_and_hide()
            self.act_start.setText("開始")
            self.act_start.setIcon(icon("play"))
            self.status_recognized.setText("已取消載入")
            return
        self.audio.stop()
        self.timer_ctrl.pause()
        self.act_start.setText("繼續")
        self.act_start.setIcon(icon("play"))
        self.status_recognized.setText("已暫停。")

    def _reset_position(self) -> None:
        if not self.transcript or not self.transcript.sentences:
            return
        result = self.engine.jump_to_sentence(0)
        self.view.set_position(result.global_char_pos, animate=False)
        self.view.clear_skipped()
        self._sync_karaoke_to_slide_view()

    def _clear_skipped(self) -> None:
        self.view.clear_skipped()
        self._sync_karaoke_to_slide_view()

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
        # Q&A 模式啟用中：文字同時路由到 Q&A 面板 + 底部狀態列（讓使用者確認有收音）
        if self.qa_panel.isVisible():
            self.qa_panel.append_recognized(delta)
            # 卡拉 OK 開啟時，同一段辨識文字也用來推進答稿高亮
            self.qa_panel.advance_answer_karaoke(delta)
            preview = delta.strip()
            if len(preview) > 60:
                preview = preview[:57] + "…"
            self.status_recognized.setText(f"QA 收音：{preview}")
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
            # SlideModeView 同步：karaoke 位置 + skipped ranges（slide / 直屏 split 模式才看得到）
            self._sync_karaoke_to_slide_view()
            # 跨頁 → 自動換頁（SlideModeView + 縮圖列；PrompterView 嵌入式靠 scroll 自動跟上）
            self._maybe_auto_advance_page()
        self._update_recognizer_prompt()
        # 更新引擎狀態列（不論是否更新位置都顯示，讓使用者隨時可見引擎在做什麼）
        self._update_engine_status(result)

    def _sync_karaoke_to_slide_view(self) -> None:
        """把 PrompterView 的 karaoke 狀態（位置 + 漏講範圍）推給 SlideModeView，
        讓 slide 模式 / 直屏 split 模式也看得到提詞效果。"""
        if not hasattr(self, "slide_mode_view"):
            return
        try:
            self.slide_mode_view.set_karaoke_position(self.engine.current_global_char)
            self.slide_mode_view.set_skipped_ranges(list(self.view._skipped_ranges))
        except Exception:
            pass

    def _maybe_auto_advance_page(self) -> None:
        """語者講到新頁的內容時 → 自動切換投影片與縮圖列到對應頁。

        - SlideModeView（slide 模式或直屏 split 模式）：呼叫 set_current_page 切頁
        - slide_preview 縮圖列：scroll_to_page 讓縮圖高亮跟著走
        - PrompterView 嵌入式投影片（橫屏 split）：不用處理，其 paintEvent 已綁在 scroll 位置
        """
        if self.transcript is None or not self.transcript.pages:
            return
        idx = self.engine.current_sentence_index
        page = self.transcript.page_of_sentence(idx)
        if page is None:
            return
        page_idx_0 = page.number - 1
        # SlideModeView 切頁
        if self._content_stack.currentIndex() == 1:
            if self.slide_mode_view.current_page() != page_idx_0:
                self.slide_mode_view.set_current_page(page_idx_0)
        # 縮圖列同步（slide 模式下才可見）
        if self.slide_deck is not None and self.slide_preview.isVisible():
            if 1 <= page.number <= self.slide_deck.page_count:
                self.slide_preview.scroll_to_page(page.number)

    def _maybe_auto_advance_to_next_page_on_silence(self) -> None:
        """頁尾啟發式：當引擎停在當頁最後一句、位置已過 60% 字元、且 ≥ 3 秒未 commit
        → 自動把位置跳到下一頁第一句，讓 karaoke 視覺繼續推進。

        防止下列情境失效：使用者把當頁最後一句講完 → 短暫停頓 → 開始念下一頁；
        但因為最後一句有少量沒被辨到的尾巴，引擎仍卡在當頁，下一頁的字一直沒高亮。
        """
        if self.transcript is None or not self.transcript.pages:
            return
        if not getattr(self, "audio", None) or not self.audio.is_running():
            return
        if getattr(self, "_qa_active", False):
            return
        idx = self.engine.current_sentence_index
        page = self.transcript.page_of_sentence(idx)
        if page is None:
            return
        # 必須是當頁最後一句
        if idx != page.sentence_end - 1:
            return
        # 必須有下一頁
        pages = self.transcript.pages
        try:
            page_pos = pages.index(page)
        except ValueError:
            return
        if page_pos + 1 >= len(pages):
            return
        next_page = pages[page_pos + 1]
        if next_page.sentence_start >= len(self.transcript.sentences):
            return
        # 位置必須過該句 40% 才算「大致講完」（從 60% 降低 → 更靈敏）
        sent = self.transcript.sentences[idx]
        sent_len = max(1, sent.end - sent.start)
        progress = (self.engine.current_global_char - sent.start) / sent_len
        if progress < 0.4:
            return
        # 必須卡 ≥ 1.5 秒（從 3 秒降低 → 更即時切頁）
        stuck_s = time.monotonic() - self.engine._last_commit_time
        if stuck_s < 1.5:
            return
        # 觸發跳頁
        next_sent = self.transcript.sentences[next_page.sentence_start]
        self.engine.jump_to_sentence(next_page.sentence_start)
        self.view.set_position(next_sent.start, animate=True)
        self._sync_karaoke_to_slide_view()
        self._maybe_auto_advance_page()
        if hasattr(self, "status_recognized"):
            self.status_recognized.setText(
                f"⏭ 自動跳到第 {next_page.number} 頁（上頁停頓 {stuck_s:.1f}s）"
            )

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
        # 頁尾停頓 → 自動跳到下一頁第一句（每 500ms 檢查一次）
        self._maybe_auto_advance_to_next_page_on_silence()

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
            f"句 {idx + 1}/{total}   信心 {conf:.0f}   {symbol} {nice}{stuck_indicator}"
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
        prompt 會剝掉 `#/##/###` 標題、`---` 分隔、`<!-- ... -->` 註解 —
        這些記號若進到 prompt 會讓 Whisper 把單字 char 當 context，誘發 repeat loop。
        """
        if self.transcript is None:
            return
        import re
        full = self.transcript.full_text
        cur = self.engine.current_global_char
        start = max(0, cur - 100)
        end = min(len(full), cur + 100)
        raw = full[start:end]
        # 移除 <!-- ... --> 備忘、行首 #/##/### 標題、分隔線 ---/===/***
        cleaned = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
        cleaned = re.sub(r"(?m)^\s*#{1,6}\s.*$", "", cleaned)
        cleaned = re.sub(r"(?m)^\s*(?:-{3,}|={3,}|\*{3,})\s*$", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            self.recognizer.update_prompt(cleaned)

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
        elif getattr(self, "_qa_pending_audio_start", False):
            # QA 模式獨立啟動：模型就緒後啟動音訊（loopback）
            self._qa_pending_audio_start = False
            self._start_audio_for_qa()
            self.status_recognized.setText(
                "🎤 Q&A 模式已啟動：辨識中（含系統聲音 loopback）"
            )

    def _on_audio_error(self, msg: str) -> None:
        QMessageBox.warning(self, "麥克風錯誤", msg)
        self._pause()

    def _on_recognizer_error(self, msg: str) -> None:
        QMessageBox.warning(self, "語音辨識錯誤", msg)

    def _on_time_up(self) -> None:
        self.time_panel.flash()
        self.status_recognized.setText("⚠ 已達設定時長。")

    def _on_view_clicked(self, global_char: int) -> None:
        """使用者單擊/雙擊講稿位置。

        正式報告中（audio running）→ 點擊只跳位置、不彈編輯對話框（避免台上誤觸）。
        暫停 / 尚未開始 → 彈出對話框問「編輯或跳轉」。
        """
        # 報告進行中 → 不允許點擊進編輯，只當作跳位
        if self.audio.is_running():
            result = self.engine.jump_to_global_char(global_char)
            self.view.set_position(result.global_char_pos, animate=False)
            return
        ret = QMessageBox.question(
            self, "編輯或跳轉",
            "要從這裡開始編輯講稿嗎？\n\n"
            "  是：進入編輯模式，游標自動定位到這裡\n"
            "  否：只把念稿位置跳到這裡（不編輯）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes:
            # 進編輯模式（toggle act_edit_mode 觸發完整流程，含 toolbar 按鈕狀態）
            if not self.act_edit_mode.isChecked():
                self.act_edit_mode.setChecked(True)
            # 把 cursor 放到點擊位置
            cur = self.view.textCursor()
            cur.setPosition(max(0, min(global_char, len(self.view.toPlainText()))))
            self.view.setTextCursor(cur)
            self.view.setFocus()
            self.status_recognized.setText("✏ 已進入編輯模式，游標已定位")
        else:
            # 原本行為：跳到該位置（不編輯）
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

    def _apply_orientation_layout(self) -> None:
        """依視窗寬高比調整 UI：直屏時縮短 mic bar + 主工具列拆成兩欄常駐。
        split 模式也要跟著橫/直屏切換底層 view（嵌入式 vs SlideModeView 堆疊）。"""
        is_portrait = self.width() < self.height()
        if hasattr(self, "mic_level"):
            self.mic_level.setFixedWidth(60 if is_portrait else 120)
        self._layout_main_toolbar(is_portrait)
        self._layout_annotation_toolbar(is_portrait)
        # split 模式：橫/直屏切換時重跑 _set_view_mode 讓底層 view 換對
        if getattr(self, "_view_mode", None) == "split":
            active = self.session_manager.active if hasattr(self, "session_manager") else None
            deck = active.slide_deck if active is not None else None
            want_portrait_split = is_portrait and deck is not None
            currently_on_slide_view = (
                hasattr(self, "_content_stack")
                and self._content_stack.currentIndex() == 1
            )
            if want_portrait_split != currently_on_slide_view:
                self._set_view_mode("split")

    # 主工具列 action 在直屏的「短文字」對照表（只留 emoji；下拉/checkbox 也一起壓縮）
    # 直屏時只顯示圖示（不顯示文字）的常駐鈕；有圖示才收得乾淨
    _ICON_ONLY_IN_PORTRAIT: tuple[str, ...] = (
        "act_open", "act_open_slides", "act_goto_speech",
        "act_fullscreen", "act_settings",
    )

    def _layout_main_toolbar(self, is_portrait: bool) -> None:
        """主工具列：landscape 全部展開文字；portrait 同樣全部一行，但壓縮 emoji-only 讓擠得下，
        達到「直屏也只有 2 列（main + annotation）」的目標。"""
        if not hasattr(self, "_main_toolbar_row2"):
            return
        tb1 = self._main_toolbar
        tb2 = self._main_toolbar_row2
        secondary = getattr(self, "_toolbar_secondary_acts", None) or []
        # 1) secondary 永遠回到 row 1（不再分割），row 2 永久隱藏
        for a in secondary:
            if a in tb2.actions():
                tb2.removeAction(a)
            if a not in tb1.actions():
                tb1.addAction(a)
        tb2.setVisible(False)

        # 2) 依 orientation 切換按鈕呈現：直屏只留圖示（文字仍在 tooltip）
        for attr in self._ICON_ONLY_IN_PORTRAIT:
            act = getattr(self, attr, None)
            if act is None:
                continue
            btn = tb1.widgetForAction(act)
            if btn is None:
                continue
            btn.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonIconOnly if is_portrait
                else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
            if not act.toolTip():
                act.setToolTip(act.text())

        # 3) 壓縮 widget：checkbox / spinbox 在直屏改窄
        if hasattr(self, "cb_target"):
            if not hasattr(self, "_orig_cb_target_text"):
                self._orig_cb_target_text = self.cb_target.text()
            self.cb_target.setText("" if is_portrait else self._orig_cb_target_text)
        if hasattr(self, "sb_font_size"):
            self.sb_font_size.setFixedWidth(60 if is_portrait else 80)
        if hasattr(self, "sb_target_min"):
            self.sb_target_min.setFixedWidth(60 if is_portrait else 80)

    def _layout_annotation_toolbar(self, is_portrait: bool) -> None:
        """標註工具列的直橫屏配置（v1.5 起兩條都不再常駐顯示）。

        標註工具已改由「標註」模組列承載，這兩條舊工具列只保留物件相容性，
        因此這裡直接收起、不再參與版面計算。
        """
        if not hasattr(self, "annotation_toolbar_row2"):
            return
        self.annotation_toolbar.setVisible(False)
        self.annotation_toolbar_row2.setVisible(False)
        return
        tb1 = self.annotation_toolbar
        tb2 = self.annotation_toolbar_row2
        secondary = getattr(self, "_annotation_secondary_acts", None)
        if not secondary:
            tb2.setVisible(False)
            return
        target = tb2 if is_portrait else tb1
        current = tb1 if is_portrait else tb2
        # 把 secondary 移到對應的 toolbar（保持順序）
        if secondary[0] not in target.actions():
            for a in secondary:
                if a in current.actions():
                    current.removeAction(a)
                target.addAction(a)
        # 只有「直屏」且至少有一個 secondary 可見（= 編輯模式開啟）才顯示 row 2
        any_visible = any(a.isVisible() for a in secondary)
        tb2.setVisible(is_portrait and any_visible)

    def _clear_current_page_annotations(self) -> None:
        """清除當前檢視的所有標註（slide mode 清該 slide 頁；其他模式清所有 doc 錨點）。"""
        from PySide6.QtWidgets import QMessageBox
        if self._view_mode == "slide":
            target_page = self.slide_mode_view.current_page() + 1
            # 找該 slide_page 有多少 annotations
            existing = [
                a for a in self.slide_mode_view.annotations()
                if a.anchor == "slide" and a.slide_page == target_page
            ]
            if not existing:
                self.status_recognized.setText("本頁沒有標註可清除")
                return
            ret = QMessageBox.question(
                self, "清除本頁標註",
                f"確定清除第 {target_page} 頁的 {len(existing)} 個標註嗎？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
            kept = [
                a for a in self.slide_mode_view.annotations()
                if not (a.anchor == "slide" and a.slide_page == target_page)
            ]
            self.slide_mode_view.set_annotations(kept)
            self.slide_mode_view.annotations_changed.emit()
            self.status_recognized.setText(f"🧹 已清除第 {target_page} 頁的 {len(existing)} 個標註")
        else:
            # transcript / split：清所有 doc 錨點
            existing = self.view.annotations()
            if not existing:
                self.status_recognized.setText("沒有講稿標註可清除")
                return
            ret = QMessageBox.question(
                self, "清除講稿標註",
                f"確定清除講稿上所有 {len(existing)} 個標註嗎？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
            self.view.set_annotations([])
            self.view.annotations_changed.emit()
            self.status_recognized.setText(f"🧹 已清除 {len(existing)} 個講稿標註")

    def _set_annotation_color(self, color: str) -> None:
        """設定鉛筆/便利貼顏色，派送到兩個 view。"""
        self._current_color = color
        # 同步預設按鈕 checked 狀態
        if hasattr(self, "_color_preset_btns"):
            for btn in self._color_preset_btns:
                c = btn.styleSheet().split("background:")[1].split(";")[0].strip()
                btn.blockSignals(True)
                btn.setChecked(c.lower() == color.lower())
                btn.blockSignals(False)
        self.view.set_tool_color(color)
        self.slide_mode_view.set_tool_color(color)

    def _pick_custom_color(self) -> None:
        """彈 QColorDialog 讓使用者挑顏色。"""
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QColorDialog
        col = QColorDialog.getColor(QColor(self._current_color), self, "選擇顏色")
        if col.isValid():
            self._set_annotation_color(col.name())
            # 任何預設按鈕的 check 都取消（因為是自訂色）
            for btn in self._color_preset_btns:
                btn.blockSignals(True); btn.setChecked(False); btn.blockSignals(False)

    def _set_annotation_tool(self, tool: str) -> None:
        """把標註工具派送給 PrompterView + SlideModeView。

        tool: "pointer" | "pencil" | "note" | "eraser"
        指標模式下，若在 slide 區域拖曳則自動啟動 PDF 文字選取（Word 風格），
        Ctrl+C 複製。不需要獨立的選字 tool。
        """
        self.view.set_tool(tool)
        self.slide_mode_view.set_tool(tool)
        # 同步 toolbar 的 checked 狀態（避免程式改 tool 時按鈕沒跟著）
        tool_to_act = {
            "pointer": getattr(self, "act_tool_pointer", None),
            "pencil": getattr(self, "act_tool_pencil", None),
            "note": getattr(self, "act_tool_note", None),
            "eraser": getattr(self, "act_tool_eraser", None),
        }
        for t, act in tool_to_act.items():
            if act is None:
                continue
            act.blockSignals(True)
            act.setChecked(t == tool)
            act.blockSignals(False)

    def _on_text_copied_from_slide(self, text: str) -> None:
        """投影片上的文字被複製 → status bar 提示。"""
        preview = text[:30] + ("…" if len(text) > 30 else "")
        self.status_recognized.setText(f"📋 已複製 {len(text)} 字：「{preview}」")

    def _on_annotations_changed(self) -> None:
        """兩個 view 的標註都存回 session；分 anchor 合併。"""
        active = self.session_manager.active
        if active is None:
            return
        slide_anns = [
            a for a in self.slide_mode_view.annotations() if a.anchor == "slide"
        ]
        doc_anns = [a for a in self.view.annotations() if a.anchor == "doc"]
        active.annotations = slide_anns + doc_anns
        try:
            self.session_manager.save_to_disk(default_sessions_path())
        except Exception as e:
            logger.warning("存 sessions.json 失敗：%s", e)

    def _toggle_layout_swap(self) -> None:
        """對調文字/投影片位置（SlideModeView + PrompterView 嵌入式兩者都切換）。"""
        self._layout_swapped = not self._layout_swapped
        self.slide_mode_view.set_layout_swapped(self._layout_swapped)
        self.view.set_layout_swapped(self._layout_swapped)
        # 按鈕顯示當前狀態（⇆ / ⇄）
        self.btn_swap_layout.setText("⇄" if self._layout_swapped else "⇆")
        active = self.session_manager.active
        if active is not None:
            active.layout_swapped = self._layout_swapped

    def _set_view_mode(self, mode: str) -> None:
        """切換檢視模式：transcript | split | slide。

        transcript：文字滿版（隱藏投影片欄 + 縮圖列）
        split     ：文左圖右（現在預設；無縮圖列）
        slide     ：文左圖右 + 左側 PDF-style 縮圖列，左右方向鍵逐頁切換
        """
        if mode not in ("transcript", "split", "slide"):
            return
        self._view_mode = mode
        active = self.session_manager.active
        deck = active.slide_deck if active is not None else None

        # 同步按鈕選中狀態（blockSignals 避免 clicked 再觸發 _set_view_mode）
        for btn, m in (
            (self.btn_mode_transcript, "transcript"),
            (self.btn_mode_split, "split"),
            (self.btn_mode_slide, "slide"),
        ):
            btn.blockSignals(True)
            btn.setChecked(m == mode)
            btn.blockSignals(False)

        if mode == "transcript":
            # PrompterView：文字滿版，無投影片
            self._content_stack.setCurrentIndex(0)
            self.view.set_slide_deck(None)
            self.slide_preview.hide()
        elif mode == "split":
            # 直屏 + 有投影片 → 用 SlideModeView（上下堆疊）；否則 PrompterView 嵌入式
            is_portrait = self.width() < self.height()
            if is_portrait and deck is not None:
                self._enter_slide_mode_view(deck, active, show_thumbnails=False)
            else:
                self._content_stack.setCurrentIndex(0)
                self.view.set_slide_deck(deck)
                self.slide_preview.hide()
        elif mode == "slide":
            self._enter_slide_mode_view(deck, active, show_thumbnails=True)

        # 持久化到 active session
        if active is not None:
            active.view_mode = mode

    def _enter_slide_mode_view(self, deck, active, show_thumbnails: bool) -> None:
        """切到 SlideModeView（上下/左右堆疊）。
        show_thumbnails=True 為正式 slide 模式；False 為直屏 split 模式（隱藏縮圖列）。"""
        self.slide_mode_view.set_transcript(self.transcript)
        self.slide_mode_view.set_slide_deck(deck)
        try:
            self.slide_mode_view.set_format_spans(self.view.dump_format_spans())
        except Exception:
            pass
        if active is not None:
            self.slide_mode_view.set_annotations(
                [a for a in active.annotations if a.anchor == "slide"]
            )
            self.view.set_annotations(
                [a for a in active.annotations if a.anchor == "doc"]
            )
        self.slide_mode_view.set_current_page(self._current_page_idx())
        # 進入 slide 模式 → 立刻同步當前 karaoke 狀態
        self._sync_karaoke_to_slide_view()
        swap = active.layout_swapped if active is not None else False
        self._layout_swapped = swap
        self.slide_mode_view.set_layout_swapped(swap)
        self.view.set_layout_swapped(swap)
        if hasattr(self, "btn_swap_layout"):
            self.btn_swap_layout.setText("⇄" if swap else "⇆")
        self._content_stack.setCurrentIndex(1)
        self.slide_mode_view.setFocus()
        if show_thumbnails and deck is not None:
            title = (
                Path(active.slides_path).name
                if active and active.slides_path
                else "投影片"
            )
            self.slide_preview.set_deck(deck, title=title)
            self.slide_preview.show()
            self.slide_preview.scroll_to_page(self._current_page_idx() + 1)
            width = active.thumbnail_panel_width if active is not None else 200
            self._set_thumbnail_width(width)
        else:
            self.slide_preview.hide()
            if show_thumbnails and deck is None:
                self.status_recognized.setText(
                    "投影片模式（未載入投影片：左右鍵仍可跳頁）"
                )

    def _set_thumbnail_width(self, width: int) -> None:
        """設定縮圖列在 content_splitter 的寬度。
        width=0 表示收合；否則 clamp 在 [180, min(400, total*0.4)] 以免吃掉主內容區。"""
        sizes = self.content_splitter.sizes()
        if len(sizes) < 2:
            return
        total = sum(sizes)
        if width <= 0:
            self.content_splitter.setSizes([0, total])
            return
        # 最小 180（太窄看不到縮圖標題）；最大取「total 的 40%」和 400 的較小者
        max_thumb = max(180, min(400, int(total * 0.4)))
        new_thumb = max(180, min(max_thumb, width))
        # 主內容區至少保留 300px，否則縮圖全部讓位
        if total - new_thumb < 300:
            new_thumb = max(0, total - 300)
        self.content_splitter.setSizes([new_thumb, total - new_thumb])

    def _on_thumbnail_collapse(self, collapse: bool) -> None:
        """收合 / 展開縮圖列。收合時顯示一個浮動 ▶ 按鈕可再展開。"""
        active = self.session_manager.active
        if collapse:
            # 記住目前寬度；收合 → 隱藏 + 把 splitter 左欄壓到 0
            if active is not None:
                sizes = self.content_splitter.sizes()
                if len(sizes) >= 2 and sizes[0] > 0:
                    active.thumbnail_panel_width = sizes[0]
            # 關鍵 1：minimumWidth 從 160 暫時降到 0，否則 setSizes([0, ...]) 會被夾回 160
            self.slide_preview.setMinimumWidth(0)
            self.slide_preview.hide()
            sizes = self.content_splitter.sizes()
            if len(sizes) >= 2:
                total = sum(sizes)
                self.content_splitter.setSizes([0, total])
            self._show_thumbnail_expand_btn()
        else:
            # 展開 → 還原最小寬度 + 顯示 + 還原寬度
            self.slide_preview.setMinimumWidth(160)
            self.slide_preview.show()
            width = active.thumbnail_panel_width if active is not None else 200
            self._set_thumbnail_width(max(180, width))
            self._hide_thumbnail_expand_btn()

    def _show_thumbnail_expand_btn(self) -> None:
        """顯示浮動 ▶ 按鈕讓使用者重新展開縮圖列。
        關鍵 2：parent 改 MainWindow（self），不要 parent 到 content_splitter，
        否則 QSplitter 會把它當成 section 重排版面，破壞主內容區顯示。"""
        if not hasattr(self, "_btn_expand_thumb"):
            self._btn_expand_thumb = QToolButton(self)
            self._btn_expand_thumb.setText("▶")
            self._btn_expand_thumb.setToolTip("展開縮圖列")
            self._btn_expand_thumb.setStyleSheet(
                "QToolButton { background: #4CAF50; color: white; "
                "font-size: 13px; padding: 4px 2px; border: none; "
                "border-top-right-radius: 6px; border-bottom-right-radius: 6px; }"
                "QToolButton:hover { background: #66BB6A; }"
            )
            self._btn_expand_thumb.setFixedSize(20, 40)
            self._btn_expand_thumb.clicked.connect(
                lambda: self._on_thumbnail_collapse(False)
            )
        # 對齊到 content_splitter 的左上角；y 取垂直置中
        target = self.content_splitter
        top_left = target.mapTo(self, target.rect().topLeft())
        self._btn_expand_thumb.move(top_left.x(), top_left.y() + target.height() // 2 - 20)
        self._btn_expand_thumb.show()
        self._btn_expand_thumb.raise_()

    def _hide_thumbnail_expand_btn(self) -> None:
        if hasattr(self, "_btn_expand_thumb"):
            self._btn_expand_thumb.hide()

    def _current_page_idx(self) -> int:
        """回傳目前 engine.current_sentence_index 所在頁的 index（0-based）。"""
        if self.transcript is None or not self.transcript.pages:
            return 0
        cur_sent = self.engine.current_sentence_index
        for i, page in enumerate(self.transcript.pages):
            if page.sentence_start <= cur_sent < page.sentence_end:
                return i
        return len(self.transcript.pages) - 1

    def _navigate_page(self, delta: int) -> None:
        """投影片模式左右方向鍵：跳到上/下一頁，所有元件同步更新。"""
        if self.transcript is None or not self.transcript.pages:
            return
        cur_page_idx = self._current_page_idx()
        new_idx = max(0, min(len(self.transcript.pages) - 1, cur_page_idx + delta))
        if new_idx == cur_page_idx:
            return
        page = self.transcript.pages[new_idx]
        # engine 同步（保留對齊狀態，切回 split 模式也會在正確位置）
        result = self.engine.jump_to_sentence(page.sentence_start)
        self.view.set_position(result.global_char_pos, animate=False)
        # 目前用 SlideModeView（slide 模式 or 直屏 split）→ 同步新頁
        if self._content_stack.currentIndex() == 1:
            self.slide_mode_view.set_current_page(new_idx)
        # 縮圖列同步
        if self.slide_deck is not None and 1 <= page.number <= self.slide_deck.page_count:
            self.slide_preview.scroll_to_page(page.number)

    def _toggle_qa_mode(self) -> None:
        """切換 Q&A 模式：右側顯示/隱藏 QA 面板。"""
        if self.qa_panel.isVisible():
            self._exit_qa_mode()
        else:
            self._enter_qa_mode()

    def _enter_qa_mode(self) -> None:
        self.qa_panel.show()
        self.act_qa_mode.setChecked(True)
        self.act_qa_mode.setText("Q&A 模式")
        # 自動勾選「翻譯中文」：觀眾可能用英文提問，翻譯即時給中文
        if hasattr(self.qa_panel, "translate_check") and not self.qa_panel.translate_check.isChecked():
            self.qa_panel.translate_check.setChecked(True)
        # 模型載入進度：用與 ▶ 開始 相同的 loading overlay
        if hasattr(self, "loading_overlay"):
            self.loading_overlay.set_status(
                "Q&A 模式準備中", "正在切換辨識模型…",
            )
            self.loading_overlay.show_over(self._content_stack)
        # 核心修正：QA 模式要獨立可啟動，不需使用者先按 ▶ 開始
        # 1) recognizer 未啟 → 從頭載入（載好後 _on_model_loaded 會啟動音訊）
        # 2) recognizer 已啟 → 切語言 + 重啟音訊改用 loopback
        if not self.recognizer.is_running():
            self._qa_pending_audio_start = True
            self.recognizer.start(
                model_size=self.cfg.model_size,
                language=self.qa_panel.get_language(),
                compute_type=self.cfg.compute_type,
                initial_prompt="",   # QA 模式不帶講稿偏向
            )
        else:
            self._switch_recognizer_language(self.qa_panel.get_language())
            # 音訊如果已在跑 → 重啟切到 loopback；如果還沒跑 → 直接啟動
            if getattr(self.cfg, "qa_use_system_audio", True):
                if self.audio.is_running():
                    self._restart_audio_for_current_mode()
                else:
                    self._start_audio_for_qa()
        self.status_recognized.setText(
            "Q&A 模式：觀眾提問會辨識並匹配預備答案（含遠距 Teams/Zoom 輸出聲音）"
        )

    def _start_audio_for_qa(self) -> None:
        """QA 模式獨立啟動音訊（不用先按 ▶）。使用 loopback（如設定啟用）。"""
        device = self.cfg.mic_device
        device_arg: int | str | None
        if device == "":
            device_arg = None
        else:
            try:
                device_arg = int(device)
            except ValueError:
                device_arg = device
        use_loopback = bool(
            self.qa_panel.isVisible()
            and getattr(self.cfg, "qa_use_system_audio", True)
        )
        self.audio.start(device=device_arg, loopback=use_loopback)

    def _restart_audio_for_current_mode(self) -> None:
        """根據目前是否在 QA 模式 + cfg.qa_use_system_audio 重啟音訊擷取。
        報告中才有意義（audio 本來就 running）；若沒 running 就跳過，待 ▶ 開始 時用正確來源。"""
        if not self.audio.is_running():
            return
        self.audio.stop()
        device = self.cfg.mic_device
        device_arg: int | str | None
        if device == "":
            device_arg = None
        else:
            try:
                device_arg = int(device)
            except ValueError:
                device_arg = device
        use_loopback = bool(
            self.qa_panel.isVisible() and getattr(self.cfg, "qa_use_system_audio", True)
        )
        self.audio.start(device=device_arg, loopback=use_loopback)

    def _on_qa_goto_page(self, page_no: int) -> None:
        """Q&A 命中帶頁碼的題目 → 翻到該投影片頁。

        與 `_on_slide_page_requested` 的差別：QA 模式常常只開投影片、沒有講稿，
        所以這裡不要求 transcript 存在，只要 deck 有那一頁就翻。
        """
        deck = getattr(self, "slide_deck", None)
        total = deck.page_count if deck is not None else 0
        if total <= 0 or page_no < 1 or page_no > total:
            return
        if self.transcript is not None and self.transcript.pages:
            # 有講稿 → 走既有路徑，順帶同步講稿位置
            self._on_slide_page_requested(page_no)
        # 不論目前是 slide 或 split 模式都把大圖切過去：QA 時使用者要立刻看到那一頁
        self.slide_mode_view.set_current_page(page_no - 1)
        self.slide_preview.show_page(page_no)
        self.slide_preview.scroll_to_page(page_no)

    def _on_qa_karaoke_toggled(self, enabled: bool) -> None:
        """記住答稿卡拉 OK 開關狀態。"""
        self.cfg = dataclass_replace(self.cfg, qa_karaoke_enabled=bool(enabled))
        save_config(self.cfg)

    def _on_qa_path_changed(self, path: str) -> None:
        """記住最後載入的 Q&A 庫路徑，下次啟動自動還原。"""
        if not path:
            return
        self.cfg = dataclass_replace(self.cfg, last_qa_path=path)
        save_config(self.cfg)

    def _exit_qa_mode(self) -> None:
        self.qa_panel.hide()
        self.act_qa_mode.setChecked(False)
        self.act_qa_mode.setText("Q&A 模式")
        # 切回預設語言
        self._switch_recognizer_language(self.cfg.language)
        # 切回麥克風輸入（離開 QA → 報告繼續）
        if getattr(self.cfg, "qa_use_system_audio", True):
            self._restart_audio_for_current_mode()
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

            # 啟麥克風（強制 mic 模式 — 錄影預期錄使用者人聲，不是系統 loopback）
            auto_started_audio = False
            device = self.cfg.mic_device
            device_arg: int | str | None
            if device == "":
                device_arg = None
            else:
                try:
                    device_arg = int(device)
                except ValueError:
                    device_arg = device
            if not self.audio.is_running():
                self.audio.start(device=device_arg, loopback=False)
                auto_started_audio = True
            else:
                # 如果目前是 loopback（例如在 QA 模式中），暫時切回 mic 才能錄到人聲
                worker = getattr(self.audio, "_worker", None)
                if worker is not None and getattr(worker, "loopback", False):
                    self.audio.stop()
                    self.audio.start(device=device_arg, loopback=False)
                    auto_started_audio = True
                    self.status_recognized.setText(
                        "🎤 錄影：已切回麥克風（錄影結束自動切回原本來源）"
                    )

            # 截圖頻率：螢幕用 15fps（DWM 合成器在高頻 BitBlt 下會閃爍）；
            # 視窗用 24fps（widget.grab 不經 DWM，可以高頻一些但仍保守）
            from teleprompter.core.video_encoder import CaptureSource
            target_fps = 15 if target.source != CaptureSource.WIDGET else 24
            ok = self.recorder.start(default_recording_root(), target=target, fps=target_fps)
            if not ok:
                self.act_record.blockSignals(True)
                self.act_record.setChecked(False)
                self.act_record.blockSignals(False)
                if auto_started_audio:
                    self.audio.stop()
                return
            self.act_record.setText("停止錄影")
            self.status_recording.setText("🔴 錄影中 00:00")
        else:
            self.recorder.stop()
            self.act_record.setText("錄影")
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
        # 若錄影前曾因 QA 模式把音訊從 loopback 切到 mic → 錄完切回 loopback
        if (
            getattr(self.cfg, "qa_use_system_audio", True)
            and self.qa_panel.isVisible()
            and self.audio.is_running()
        ):
            worker = getattr(self.audio, "_worker", None)
            if worker is not None and not getattr(worker, "loopback", False):
                self._restart_audio_for_current_mode()
        if mp4_path:
            QMessageBox.information(
                self, "錄影已儲存",
                f"MP4 影音檔已儲存於:\n{mp4_path}",
                QMessageBox.StandardButton.Ok,
            )

    def _on_record_error(self, msg: str) -> None:
        if self._mux_dialog is not None:
            self._mux_dialog.close()
            self._mux_dialog = None
        QMessageBox.warning(self, "錄影錯誤", msg)
        self.act_record.blockSignals(True)
        self.act_record.setChecked(False)
        self.act_record.blockSignals(False)
        self.act_record.setText("錄影")
        self.status_recording.setText("")

    def _toggle_edit_mode(self, checked: bool) -> None:
        """切換編輯模式：
        - 進入：記住**視窗可視位置**（不是 engine 位置，因為使用者可能沒開始辨識）
        - 離開：保存格式 → 重 parse → 還原格式 + scroll 位置
        """
        if checked:
            if self.audio.is_running():
                self._pause()
            # slide 模式 / 直屏 split 都是 stack 1（SlideModeView）— 看不到 PrompterView
            # 無法視覺化編輯。記住原本模式，暫時切到 split（橫屏）或 transcript（直屏）
            # 讓使用者在 PrompterView 中直接看游標與輸入。離開編輯模式時再還原。
            self._pre_edit_view_mode: str | None = None
            if self._content_stack.currentIndex() == 1:
                self._pre_edit_view_mode = self._view_mode
                # 決定要暫切到哪個模式：橫屏切 split（保留 slide 參考），直屏切 transcript（滿版文字）
                is_portrait = self.width() < self.height()
                target = "transcript" if is_portrait else "split"
                # 若原本就是 split，強制先切 transcript 再切回，避免 split→split noop
                if target == "split" and self._view_mode == "split":
                    # 直接退回 PrompterView（embedded slide）— split 在橫屏是 stack 0
                    self._set_view_mode("split")
                else:
                    self._set_view_mode(target)
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
            self.view.setFocus()
            if inserted_n > 0:
                self.status_recognized.setText(
                    f"✏ 編輯模式（已為 {inserted_n} 張多餘的投影片追加空白講稿區塊，可點擊輸入）"
                )
            elif self._pre_edit_view_mode is not None:
                self.status_recognized.setText(
                    "✏ 編輯模式：已切到講稿區編輯，離開編輯模式會回到原本檢視"
                )
            else:
                self.status_recognized.setText("✏ 編輯模式：可直接修改講稿，再按一次離開")
        else:
            # 1) dump 格式
            formats_to_keep = self.view.dump_format_spans()
            text_len_before = len(self.view.toPlainText())
            # 2) set_edit_mode(False) → _on_transcript_edited → set_text（清 document）
            self.view.set_edit_mode(False)
            # 3) 若 text_len 變了（_apply_transcript / MD 重繪可能影響），濾除越界 span
            text_len_after = len(self.view.toPlainText())
            if text_len_before != text_len_after:
                logger.warning(
                    "edit-exit: text_len 變化 %d → %d，濾除越界 span",
                    text_len_before, text_len_after,
                )
                formats_to_keep = [
                    s for s in formats_to_keep if s.end <= text_len_after
                ]
            # 4) 重新套格式到新 document
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
            # 6) 若進入編輯模式前曾暫切檢視模式 → 還原
            pre_mode = getattr(self, "_pre_edit_view_mode", None)
            if pre_mode is not None and pre_mode != self._view_mode:
                self._set_view_mode(pre_mode)
            self._pre_edit_view_mode = None

    def _offer_extracted_notes(self, source_path, deck) -> bool:
        """從投影片原始檔抽 speaker notes → 預覽/編輯視窗 → 載入為講稿。

        回傳 True 表示已載入講稿（呼叫端就不用再產 scaffold）。
        任何一步失敗都靜默回 False，維持原本的 scaffold 流程。
        """
        try:
            from ..core.notes_extractor import extract_notes
            notes = extract_notes(source_path)
        except Exception:
            return False
        if notes.page_count == 0 or notes.filled_count == 0:
            return False
        # 頁數對不上（例如 PPTX 有隱藏頁）→ 以 deck 為準補齊／截斷，避免錯位
        if notes.page_count != deck.page_count:
            notes.pages = (notes.pages + [""] * deck.page_count)[:deck.page_count]
            notes.titles = (notes.titles + [""] * deck.page_count)[:deck.page_count]

        dialog = NotesImportDialog(notes, deck=deck, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return False
        text = dialog.transcript_text()
        if not text.strip():
            return False
        from ..core.transcript_loader import load_from_string
        self._apply_transcript(load_from_string(text), source_path="")
        self.status_recognized.setText(
            f"✅ 已從投影片載入講稿（{notes.filled_count}/{notes.page_count} 頁）"
        )
        return True

    def _create_default_transcript_from_slides(self, n_pages: int) -> None:
        """投影片已載入但講稿為空 → 產生 n_pages 頁的 scaffold 讓使用者直接編輯。

        每頁一個 `# Slide N` 標題 + placeholder，頁間用 `---` 分隔。
        """
        if n_pages <= 0:
            return
        from ..core.transcript_loader import load_from_string
        placeholder = "（請在此輸入此頁講稿）"
        parts = [f"# Slide {i}\n\n{placeholder}\n" for i in range(1, n_pages + 1)]
        text = "\n---\n\n".join(parts)
        transcript = load_from_string(text)
        self._apply_transcript(transcript, source_path="")

    def _on_pages_missing(self, n: int) -> None:
        """編輯模式下發現有投影片沒有對應講稿區塊 → 立刻補齊。

        典型情境：使用者在編輯模式把講稿整段刪掉，頁數掉回 1，
        後面幾十頁就變成點不進去的空白。補完重新排版即可恢復可編輯。
        """
        if n <= 0 or self._pages_missing_guard:
            return
        self._pages_missing_guard = True          # 補頁會再觸發 relayout，擋掉遞迴
        try:
            if self._expand_transcript_for_slides() > 0:
                self.view.set_edit_mode(True)     # 保持在編輯模式
        finally:
            self._pages_missing_guard = False

    def _expand_transcript_for_slides(self) -> int:
        """若 slide_deck 的頁數 > 講稿頁數，為每張多餘的 slide 追加一個空白 `---` + `# Slide N` 區塊。
        回傳追加的頁數。只影響講稿文字（不動 view 以外的資料）。

        使用時機：進入編輯模式前。
        """
        if self.transcript is None or self.slide_deck is None:
            return 0
        # 用 view 的分頁符數量當口徑：`_relayout_slide_gaps` 就是照這個決定
        # 哪幾頁有真正的 QTextBlock；用 len(transcript.pages) 會與它錯開，
        # 導致這裡誤判「頁數夠了」而不補，畫面上卻留下點不進去的虛擬頁。
        transcript_pages = max(1, self.view.page_count_from_blocks())
        total_slides = self.slide_deck.page_count
        if total_slides <= transcript_pages:
            return 0
        current_text = self.view.toPlainText().rstrip()
        extra_parts: list[str] = []
        # 只放標題，不放「（請在此輸入…）」：那串字會被語音對齊當成待念的講稿
        for i in range(transcript_pages + 1, total_slides + 1):
            extra_parts.append(chr(10)*2 + "---" + chr(10)*2 + f"# Slide {i}" + chr(10))
        new_text = current_text + "".join(extra_parts)
        # 直接 setPlainText（view 在進入 edit 前會清 formats，是預期行為）
        self.view.set_text(new_text)
        # 重新 parse 並套用（不動位置；位置在 _toggle_edit_mode 最後統一處理）
        from ..core.transcript_loader import load_from_string
        transcript = load_from_string(new_text)
        # 防呆：re-parse 出 0 句（原稿只剩標題/分頁符時會發生）就不覆寫 —
        # 靜默把 self.transcript 換成空的，會讓畫面有字卻按不了「開始」
        # （_apply_transcript 與 _on_transcript_edited 都有同款防護）
        if transcript.sentences:
            if self.session_manager.active is not None:
                self.session_manager.active.transcript = transcript
            self.transcript = transcript
            self.engine.set_transcript(transcript)
        return total_slides - transcript_pages

    def _insert_annotation(self) -> None:
        """在游標處插入 <!-- ... --> 註解。會新增文字 → 先彈確認視窗。"""
        ret = QMessageBox.question(
            self, "新增文字確認",
            "將在目前游標位置插入備忘註解 `<!-- 備忘 -->`。\n這會改動講稿文字。\n\n確定要插入嗎？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        was_edit = self.view.is_edit_mode()
        if not was_edit:
            self.view.set_edit_mode(True)
        self.view.insert_annotation_at_cursor("")
        if not was_edit:
            # 切回唯讀（會觸發 text_edited 信號把變更寫回 session）
            self.view.set_edit_mode(False)

    def _clear_all_formatting(self) -> None:
        """清除整篇文字格式（粗體/斜體/底線/螢光筆）。會動到文字外觀 → 先彈確認視窗。"""
        ret = QMessageBox.question(
            self, "清除文字格式確認",
            "將清除整篇講稿的**所有文字格式**（粗體 / 斜體 / 底線 / 螢光筆）。\n"
            "這個動作無法復原。\n\n確定要清除嗎？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.view.clear_all_formatting()
        active = self.session_manager.active
        if active is not None:
            active.format_spans = []
            active.dirty = True
        self.status_recognized.setText("🧽 已清除整篇格式")

    def _compact_whitespace(self) -> None:
        """一鍵清理多餘空白與空行。會刪除文字 → 先彈確認視窗。"""
        ret = QMessageBox.question(
            self, "刪除文字確認",
            "將清除講稿中的多餘空白行與行尾空白。\n這會改動講稿文字。\n\n確定要清理嗎？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        was_edit = self.view.is_edit_mode()
        if not was_edit:
            self.view.set_edit_mode(True)
        self.view.compact_whitespace()
        if not was_edit:
            self.view.set_edit_mode(False)
        self.status_recognized.setText("🧹 已清理多餘空白")

    def _on_edit_mode_changed(self, enabled: bool) -> None:
        # 編輯模式 OFF → 隱藏 B/I/U/格式/清格式/插入註解/清理空白
        # 編輯模式 ON → 顯示上列按鈕
        for act in (
            self.act_bold, self.act_italic, self.act_underline,
            self.act_clear_fmt, self.act_clear_all_fmt,
            self.act_insert_annotation, self.act_compact_ws,
        ):
            act.setVisible(enabled)
        if enabled:
            self.act_edit_mode.setText("編輯模式")
        else:
            self.act_edit_mode.setText("編輯模式")
        # 保持 checkbox 同步（若從程式端呼叫也能一致）
        if self.act_edit_mode.isChecked() != enabled:
            self.act_edit_mode.blockSignals(True)
            self.act_edit_mode.setChecked(enabled)
            self.act_edit_mode.blockSignals(False)
        # 直屏時 row 2 的可見性取決於這些 secondary 是否可見 → 編輯模式切換時要重 layout
        if hasattr(self, "annotation_toolbar_row2"):
            is_portrait = self.width() < self.height()
            self._layout_annotation_toolbar(is_portrait)

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
        # 觸發自動儲存（debounce 1.5 秒）
        if hasattr(self, "_autosave_debounce"):
            self._autosave_debounce.start()

    def _perform_autosave(self) -> None:
        """自動儲存：靜默寫講稿檔 + sessions.json。失敗只記 log，不彈視窗。

        - 有 transcript_path 且檔案存在 → 直接覆寫該檔
        - 無路徑 → 寫到 ~/.smartteleprompter/autosave/{session_id}.txt
        - sessions.json 同時寫一次以保留標註/格式/位置等狀態
        """
        if not hasattr(self, "session_manager"):
            return
        # 1) 把所有 dirty session 的講稿寫回原檔（或 autosave 目錄）
        any_saved = False
        for session in self.session_manager.sessions:
            if not session.dirty:
                continue
            text = session.modified_text or ""
            if not text.strip():
                continue
            target = session.transcript_path
            try:
                if target and Path(target).parent.exists():
                    Path(target).write_text(text, encoding="utf-8")
                else:
                    # 無原檔 → 寫到 autosave 目錄
                    autosave_dir = default_sessions_path().parent / "autosave"
                    autosave_dir.mkdir(parents=True, exist_ok=True)
                    autosave_path = autosave_dir / f"{session.id}.txt"
                    autosave_path.write_text(text, encoding="utf-8")
                    if not session.transcript_path:
                        session.transcript_path = str(autosave_path)
                session.dirty = False
                session.modified_text = ""
                any_saved = True
            except Exception as e:
                logger.warning("自動儲存講稿失敗 (%s): %s", session.title, e)
        # 2) sessions.json：保留標註/位置/格式
        try:
            # 同步當前 active session 的 annotations 進去
            active = self.session_manager.active
            if active is not None and hasattr(self, "slide_mode_view"):
                slide_anns = [
                    a for a in self.slide_mode_view.annotations() if a.anchor == "slide"
                ]
                doc_anns = [a for a in self.view.annotations() if a.anchor == "doc"]
                active.annotations = slide_anns + doc_anns
            self.session_manager.save_to_disk(default_sessions_path())
        except Exception as e:
            logger.warning("自動儲存 sessions.json 失敗: %s", e)
        if any_saved and hasattr(self, "status_recognized"):
            from datetime import datetime
            self.status_recognized.setText(
                f"💾 已自動儲存 {datetime.now().strftime('%H:%M:%S')}"
            )

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
        # SlideModeView 同步：字體 + karaoke 全套色
        self.slide_mode_view.set_font_family(self.cfg.font_family)
        self.slide_mode_view.set_font_size(self.cfg.font_size)
        self.slide_mode_view.set_line_spacing(self.cfg.line_spacing)
        self.slide_mode_view.set_colors(
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
        es = getattr(self, "empty_state", None)
        if es is not None and es.isVisible():
            es.setGeometry(self._content_stack.rect())
        # 遮罩跟著 stack 同大小（直屏 slide 模式也看得到）
        if hasattr(self, "loading_overlay") and self.loading_overlay.isVisible():
            self.loading_overlay.resize(self._content_stack.size())
        # 收合縮圖時的浮動展開按鈕也要跟著 splitter 重定位
        if (
            hasattr(self, "_btn_expand_thumb")
            and self._btn_expand_thumb.isVisible()
            and hasattr(self, "content_splitter")
        ):
            target = self.content_splitter
            top_left = target.mapTo(self, target.rect().topLeft())
            self._btn_expand_thumb.move(
                top_left.x(), top_left.y() + target.height() // 2 - 20
            )
        # 直屏/橫屏自適應
        self._apply_orientation_layout()

    def closeEvent(self, event) -> None:
        ft = getattr(self, "floating_timer", None)
        if ft is not None:
            ft.close()
        if getattr(self, "qa_panel", None) is not None and self.qa_panel.isWindow():
            self.qa_panel.close()
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
