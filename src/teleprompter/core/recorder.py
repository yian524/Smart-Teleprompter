"""演講記錄：即時螢幕錄影 + 聲音 → 單一 MP4。

設計：
- **零影響既有辨識**：訂閱 `AudioCaptureController.raw_frame` 旁路寫 WAV。
- **即時視訊**：`ScreenVideoEncoder` 30fps 抓 widget/screen pipe 到 ffmpeg。
- **輸出單一 MP4**：停止時 mux video + audio，中介檔（.h264 / .wav）自動刪除。

輸出結構（stop 完成後）：
    {output_root}/session-{timestamp}/
        recording.mp4
"""

from __future__ import annotations

import logging
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QScreen
from PySide6.QtWidgets import QWidget

from .audio_capture import SAMPLE_RATE
from .video_encoder import (
    CaptureSource,
    CaptureTarget,
    ScreenVideoEncoder,
    get_ffmpeg_binary,
)

logger = logging.getLogger(__name__)


class RecordingController(QObject):
    """演講錄影管理器（音訊 + 視訊 → MP4）。"""

    started = Signal(str)           # session_dir path
    stopped = Signal(str)           # mp4 path (mux 完成後才 emit)
    tick = Signal(float)            # elapsed seconds
    muxing_started = Signal()
    error = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._wav: Optional[wave.Wave_write] = None
        self._wav_lock = threading.Lock()
        self._audio_frames_written: int = 0
        self._running = False
        self._start_time: float = 0.0
        self._session_dir: Optional[Path] = None
        self._audio_wav_path: Optional[Path] = None
        self._output_mp4: Optional[Path] = None

        self._video_encoder = ScreenVideoEncoder(self)
        self._video_encoder.mux_started.connect(self.muxing_started)
        self._video_encoder.mux_finished.connect(self._on_mux_finished)
        self._video_encoder.error.connect(self._on_encoder_error)

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(1000)
        self._ui_timer.timeout.connect(self._on_ui_tick)

    # ---------- 狀態 ----------

    def is_running(self) -> bool:
        return self._running

    def is_available(self) -> bool:
        """ffmpeg 是否可用（無 ffmpeg 不能錄）。"""
        return self._video_encoder.is_available()

    def elapsed_seconds(self) -> float:
        if not self._running:
            return 0.0
        return time.monotonic() - self._start_time

    def session_dir(self) -> Optional[Path]:
        return self._session_dir

    # ---------- 控制 ----------

    def start(
        self,
        output_root: Path,
        *,
        target: CaptureTarget,
        fps: int = 30,
    ) -> bool:
        """開始錄製。"""
        if self._running:
            return False
        if not self._video_encoder.is_available():
            self.error.emit("找不到 ffmpeg（請執行 `pip install imageio-ffmpeg`）")
            return False

        try:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            output_root = Path(output_root)
            output_root.mkdir(parents=True, exist_ok=True)
            session_dir = output_root / f"session-{ts}"
            session_dir.mkdir(parents=True, exist_ok=False)

            # 中介 WAV 存到 session_dir 底下（錄完會自動刪）
            audio_wav_path = session_dir / "_tmp_audio.wav"
            wav = wave.open(str(audio_wav_path), "wb")
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)

            output_mp4 = session_dir / "recording.mp4"

            # 啟動視訊編碼器
            ok = self._video_encoder.start(
                target, tmp_dir=session_dir, output_mp4=output_mp4, fps=fps,
            )
            if not ok:
                wav.close()
                try:
                    audio_wav_path.unlink(missing_ok=True)
                except Exception:
                    pass
                try:
                    session_dir.rmdir()
                except Exception:
                    pass
                return False
            self._video_encoder.set_audio_wav_path(audio_wav_path)

            self._session_dir = session_dir
            self._audio_wav_path = audio_wav_path
            self._output_mp4 = output_mp4
            self._wav = wav
            self._audio_frames_written = 0
            self._running = True
            self._start_time = time.monotonic()

            self._ui_timer.start()
            logger.info("開始錄製：%s", session_dir)
            self.started.emit(str(session_dir))
            return True
        except Exception as e:
            logger.exception("錄製啟動失敗")
            self.error.emit(f"錄製啟動失敗：{e}")
            return False

    def stop(self) -> None:
        """停止錄製。mux 在背景進行；完成後 emit stopped(mp4_path)。"""
        if not self._running:
            return
        self._running = False
        self._ui_timer.stop()

        # 關 WAV
        with self._wav_lock:
            if self._wav is not None:
                try:
                    self._wav.close()
                except Exception as e:
                    logger.warning("關 WAV 失敗：%s", e)
                self._wav = None

        logger.info("錄音 frames 寫入：%d", self._audio_frames_written)
        if self._audio_frames_written == 0:
            logger.warning(
                "錄製期間未收到任何音訊 frame！"
                "可能原因：麥克風未啟動 / 裝置無法使用 / raw_frame signal 未連接。"
            )
            self.error.emit(
                "⚠ 錄製期間沒收到麥克風資料，輸出將無聲。\n"
                "請確認：\n"
                "  1. 麥克風已正確連接\n"
                "  2. Windows 設定 → 隱私 → 麥克風 允許此 app 使用\n"
                "  3. 設定 → 語音 → 麥克風裝置選對了"
            )

        # 停視訊 + mux（非阻塞）
        self._video_encoder.stop_and_mux()

    # ---------- 接收音訊（由 AudioCaptureController.raw_frame 轉過來） ----------

    def on_audio_frame(self, raw: bytes) -> None:
        if not self._running:
            return
        with self._wav_lock:
            if self._wav is None:
                return
            try:
                self._wav.writeframes(raw)
                self._audio_frames_written += 1
            except Exception as e:
                logger.warning("WAV 寫入失敗：%s", e)

    # ---------- 內部 callbacks ----------

    def _on_ui_tick(self) -> None:
        if self._running:
            self.tick.emit(time.monotonic() - self._start_time)

    def _on_mux_finished(self, mp4_path: str) -> None:
        logger.info("錄製完成：%s", mp4_path)
        self.stopped.emit(mp4_path)

    def _on_encoder_error(self, msg: str) -> None:
        self.error.emit(msg)
        self._running = False
        self._ui_timer.stop()


def default_recording_root() -> Path:
    """Windows: 文件\\SmartTeleprompter\\recordings"""
    import os
    if os.name == "nt":
        docs = Path(os.environ.get("USERPROFILE", Path.home())) / "Documents"
    else:
        docs = Path.home() / "Documents"
    return docs / "SmartTeleprompter" / "recordings"


# --- 來源選擇輔助（給 UI dialog 用） ---

def build_window_target(widget: QWidget) -> CaptureTarget:
    return CaptureTarget(source=CaptureSource.WIDGET, widget=widget)


def build_screen_target(screen: QScreen) -> CaptureTarget:
    return CaptureTarget(source=CaptureSource.SCREEN, screen=screen)
