"""設定對話框：字體、麥克風、模型、語言、目標時長、顏色、里程碑。"""

from __future__ import annotations

from dataclasses import replace as dataclass_replace

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFontComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig
from ..core.audio_capture import list_input_devices


def _make_color_button(initial: str) -> tuple[QPushButton, list[str]]:
    """回傳一個按鈕與一個 [color] 容器，供 caller 取最新值。"""
    state = [initial]
    btn = QPushButton(initial)
    btn.setStyleSheet(f"background-color: {initial};")

    def pick():
        col = QColorDialog.getColor(QColor(state[0]))
        if col.isValid():
            state[0] = col.name()
            btn.setText(state[0])
            btn.setStyleSheet(f"background-color: {state[0]};")

    btn.clicked.connect(pick)
    return btn, state


class SettingsDialog(QDialog):
    """設定視窗。"""

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.setMinimumWidth(440)
        self._cfg = config

        tabs = QTabWidget(self)

        # ---- 顯示分頁 ----
        display_tab = QWidget()
        df = QFormLayout(display_tab)

        self.font_combo = QFontComboBox()
        self.font_combo.setCurrentText(config.font_family)
        df.addRow("字體", self.font_combo)

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(12, 120)
        self.font_size_spin.setValue(config.font_size)
        self.font_size_spin.setSuffix(" pt")
        df.addRow("字體大小", self.font_size_spin)

        self.line_spacing_spin = QSpinBox()
        self.line_spacing_spin.setRange(100, 300)
        self.line_spacing_spin.setValue(int(config.line_spacing * 100))
        self.line_spacing_spin.setSuffix(" %")
        df.addRow("行距", self.line_spacing_spin)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["dark", "light", "high_contrast"])
        self.theme_combo.setCurrentText(config.theme)
        df.addRow("主題", self.theme_combo)

        self.highlight_btn, self._highlight_state = _make_color_button(config.highlight_color)
        df.addRow("目前字顏色", self.highlight_btn)

        self.spoken_btn, self._spoken_state = _make_color_button(config.spoken_color)
        df.addRow("已念字顏色", self.spoken_btn)

        self.upcoming_btn, self._upcoming_state = _make_color_button(config.upcoming_color)
        df.addRow("未念字顏色", self.upcoming_btn)

        self.skipped_btn, self._skipped_state = _make_color_button(config.skipped_color)
        df.addRow("漏講字顏色", self.skipped_btn)

        self.smooth_spin = QSpinBox()
        self.smooth_spin.setRange(0, 500)
        self.smooth_spin.setValue(config.karaoke_smooth_ms)
        self.smooth_spin.setSuffix(" ms")
        df.addRow("高亮平滑時間", self.smooth_spin)

        tabs.addTab(display_tab, "顯示")

        # ---- 語音分頁 ----
        speech_tab = QWidget()
        sf = QFormLayout(speech_tab)

        self.mic_combo = QComboBox()
        self.mic_combo.addItem("(系統預設)", "")
        for dev in list_input_devices():
            self.mic_combo.addItem(dev["name"], str(dev["index"]))
        # 嘗試還原選擇
        idx = self.mic_combo.findData(config.mic_device)
        if idx >= 0:
            self.mic_combo.setCurrentIndex(idx)
        sf.addRow("麥克風", self.mic_combo)

        self.model_combo = QComboBox()
        self.model_combo.addItems(
            ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]
        )
        self.model_combo.setCurrentText(config.model_size)
        sf.addRow("Whisper 模型", self.model_combo)

        self.compute_combo = QComboBox()
        self.compute_combo.addItems(
            ["auto", "float16", "int8_float16", "int8"]
        )
        self.compute_combo.setCurrentText(config.compute_type)
        sf.addRow("運算精度", self.compute_combo)

        self.lang_combo = QComboBox()
        self.lang_combo.addItem("中文 (zh)", "zh")
        self.lang_combo.addItem("英文 (en)", "en")
        self.lang_combo.addItem("自動偵測", "auto")
        idx = self.lang_combo.findData(config.language)
        if idx >= 0:
            self.lang_combo.setCurrentIndex(idx)
        sf.addRow("主要語言", self.lang_combo)

        self.soft_advance_check = QCheckBox("啟用語速軟推進（卡住超過 4 秒時自動小步前進）")
        self.soft_advance_check.setChecked(config.enable_soft_advance)
        sf.addRow("", self.soft_advance_check)

        self.stability_combo = QComboBox()
        self.stability_combo.addItem("Conservative — 高規格會議推薦（最穩定，較慢）", "conservative")
        self.stability_combo.addItem("Balanced — 預設（速度與穩定的平衡）", "balanced")
        self.stability_combo.addItem("Aggressive — 練習用（最快但容易漂移）", "aggressive")
        idx = self.stability_combo.findData(getattr(config, "stability_mode", "balanced"))
        if idx >= 0:
            self.stability_combo.setCurrentIndex(idx)
        sf.addRow("穩定性模式", self.stability_combo)

        self.max_fwd_spin = QSpinBox()
        self.max_fwd_spin.setRange(0, 200)
        self.max_fwd_spin.setSuffix(" 句 (0 = 不限)")
        self.max_fwd_spin.setValue(getattr(config, "max_forward_sentences", 10))
        sf.addRow("最大往前跳段範圍", self.max_fwd_spin)

        tabs.addTab(speech_tab, "語音")

        # ---- 計時分頁 ----
        time_tab = QWidget()
        tf = QFormLayout(time_tab)

        self.target_spin = QSpinBox()
        self.target_spin.setRange(0, 7200)
        self.target_spin.setValue(config.target_duration_sec)
        self.target_spin.setSuffix(" 秒")
        tf.addRow("目標報告時長", self.target_spin)

        self.milestones_edit = QLineEdit()
        self.milestones_edit.setText(",".join(str(x) for x in config.milestone_marks_sec))
        self.milestones_edit.setPlaceholderText("例: 300,60 表示剩 5 分、剩 1 分提示")
        tf.addRow("里程碑提示 (秒)", self.milestones_edit)

        tabs.addTab(time_tab, "計時")

        # ---- 主版面 ----
        main = QVBoxLayout(self)
        main.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        main.addWidget(buttons)

    def updated_config(self) -> AppConfig:
        try:
            milestones = tuple(
                int(x.strip())
                for x in self.milestones_edit.text().split(",")
                if x.strip().isdigit()
            )
        except ValueError:
            milestones = self._cfg.milestone_marks_sec

        return dataclass_replace(
            self._cfg,
            font_family=self.font_combo.currentFont().family(),
            font_size=self.font_size_spin.value(),
            line_spacing=self.line_spacing_spin.value() / 100.0,
            theme=self.theme_combo.currentText(),
            highlight_color=self._highlight_state[0],
            spoken_color=self._spoken_state[0],
            upcoming_color=self._upcoming_state[0],
            skipped_color=self._skipped_state[0],
            karaoke_smooth_ms=self.smooth_spin.value(),
            mic_device=self.mic_combo.currentData() or "",
            model_size=self.model_combo.currentText(),
            compute_type=self.compute_combo.currentText(),
            language=self.lang_combo.currentData() or "zh",
            enable_soft_advance=self.soft_advance_check.isChecked(),
            stability_mode=self.stability_combo.currentData() or "balanced",
            max_forward_sentences=self.max_fwd_spin.value(),
            target_duration_sec=self.target_spin.value(),
            milestone_marks_sec=milestones,
        )
