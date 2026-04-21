"""使用者設定持久化（QSettings）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QSettings


ORG = "SmartTeleprompter"
APP = "Teleprompter"


def _settings() -> QSettings:
    return QSettings(ORG, APP)


@dataclass
class AppConfig:
    font_family: str = "Microsoft JhengHei"
    font_size: int = 36
    line_spacing: float = 1.6
    theme: str = "dark"  # dark | light | high_contrast
    highlight_color: str = "#FFD54A"
    spoken_color: str = "#6B6B6B"
    upcoming_color: str = "#F0F0F0"
    skipped_color: str = "#FF1744"  # 漏講段落顏色（亮紅色 + 紅色背景 + 刪除線）
    mic_device: str = ""  # 空字串 = 系統預設
    model_size: str = "large-v3-turbo"  # tiny/base/small/medium/large-v3/large-v3-turbo
    compute_type: str = "auto"  # auto/float16/int8_float16/int8
    language: str = "zh"  # zh/en/auto
    target_duration_sec: int = 900  # 預設 15 分鐘
    milestone_marks_sec: tuple[int, ...] = (300, 60)  # 剩 5 分、剩 1 分
    last_transcript_path: str = ""
    window_geometry: bytes = b""
    prefer_secondary_screen: bool = True
    karaoke_smooth_ms: int = 150
    # 時間軸軟推進：根據語速估算自動前進位置
    # 預設關閉（避免使用者講話時字幕被多推幾字）；需要時可在設定開啟
    enable_soft_advance: bool = False
    # 穩定性模式：conservative (生產級) / balanced (預設) / aggressive (練習用)
    stability_mode: str = "balanced"
    # 最大允許往前跳的句子數（防止從開頭被誤匹配到結尾）
    # 0 = 不限制；正常會議報告建議 5-10 句，嚴格場景 3 句
    max_forward_sentences: int = 10
    # 最大允許往前跳的字元數（細粒度控制，0 = 不限制）
    max_forward_chars: int = 0
    # QA 模式使用系統聲音 loopback（Windows WASAPI）：抓 Teams/Zoom 觀眾聲音
    # True = 進 QA 時自動切 loopback；退出 QA 切回麥克風
    qa_use_system_audio: bool = True


_MIGRATION_KEY = "_migration_streaming_v1"


def load_config() -> AppConfig:
    s = _settings()
    cfg = AppConfig()
    # 一次性遷移：舊版預設 large-v3 升級到 large-v3-turbo（速度顯著提升）
    if not s.contains(_MIGRATION_KEY):
        old_model = s.value("model_size")
        if old_model == "large-v3":
            s.remove("model_size")  # 清掉，讓它套用新預設
        s.setValue(_MIGRATION_KEY, True)
        s.sync()
    for field_name in cfg.__dataclass_fields__:
        if s.contains(field_name):
            value = s.value(field_name)
            current = getattr(cfg, field_name)
            if isinstance(current, bool):
                value = str(value).lower() in ("true", "1", "yes")
            elif isinstance(current, int):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            elif isinstance(current, float):
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
            elif isinstance(current, tuple):
                if isinstance(value, str):
                    try:
                        value = tuple(int(x) for x in value.split(",") if x)
                    except ValueError:
                        continue
                elif isinstance(value, (list, tuple)):
                    value = tuple(int(x) for x in value)
            setattr(cfg, field_name, value)
    return cfg


def save_config(cfg: AppConfig) -> None:
    s = _settings()
    for field_name in cfg.__dataclass_fields__:
        value: Any = getattr(cfg, field_name)
        if isinstance(value, tuple):
            value = ",".join(str(x) for x in value)
        s.setValue(field_name, value)
    s.sync()
