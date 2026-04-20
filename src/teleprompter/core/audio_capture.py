"""麥克風擷取（串流式滑動窗口）。

設計：
- 16 kHz 單聲道 PCM，連續擷取進入 ring buffer。
- 每 EMIT_INTERVAL_MS 發出一個 AudioWindow（最近 WINDOW_SEC 秒）給辨識器。
- VAD（webrtcvad）標記每個 frame 是否有人聲；
  - 若視窗內有任何最近的人聲活動 → 發出視窗
  - 若連續 SILENCE_RESET_MS 全為靜音 → 重置視窗（句子邊界）並通知辨識器 commit
- 不再「等講者停 0.5 秒才送辨識」——可達 200~500ms 端對端延遲。
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_DURATION_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_DURATION_MS // 1000

WINDOW_SEC = 4.0                # 串流視窗長度（從 6s→4s 縮短 Whisper 推論時間 ~30%）
EMIT_INTERVAL_MS = 350          # 每 350ms 觸發一次辨識（從 400→350 更靈敏；backlog 已由 drop policy 處理）
RECENT_VOICE_WINDOW_MS = 1500   # 視窗內近 1.5s 有過語音才送辨識（過濾純靜音）
SILENCE_RESET_MS = 1500         # 連續 1.5 秒靜音 → 視窗重置（句子邊界）
MIN_EMIT_MS = 600               # 視窗內音訊低於此時長不送辨識


@dataclass
class AudioWindow:
    """一個滑動視窗的 PCM 樣本。"""

    samples: np.ndarray            # float32 mono 16kHz
    duration_ms: int
    is_boundary: bool = False      # 句子邊界（讓辨識器 commit 並重置 hypothesis）


def list_input_devices() -> list[dict]:
    try:
        import sounddevice as sd
    except Exception as e:  # pragma: no cover
        logger.warning("sounddevice 未安裝: %s", e)
        return []
    devices = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0:
            devices.append({
                "index": idx,
                "name": dev.get("name", f"Device {idx}"),
                "default_samplerate": dev.get("default_samplerate", SAMPLE_RATE),
            })
    return devices


class AudioCaptureWorker(QObject):
    window_ready = Signal(object)   # AudioWindow
    level_changed = Signal(float)
    error = Signal(str)
    # 原始樣本 tap：給錄音功能訂閱（bytes 為 int16 little-endian 單聲道 16kHz）
    raw_frame = Signal(bytes)

    def __init__(self, device: Optional[int | str] = None) -> None:
        super().__init__()
        self.device = device if device not in ("", None) else None
        self._stop = False
        self._stream = None
        self._vad = None

        max_window_samples = int(WINDOW_SEC * SAMPLE_RATE)
        self._buffer = collections.deque(maxlen=max_window_samples)
        self._buffer_lock = threading.Lock()

        # VAD 狀態
        self._frames_since_voice = 99999  # 從上次有聲音算起經過了幾個 frame
        self._silence_ms = 0
        self._has_voice_in_window = False
        self._boundary_pending = False

        self._last_emit_t = 0.0
        self._emit_interval = EMIT_INTERVAL_MS / 1000.0

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            import sounddevice as sd
            import webrtcvad
        except Exception as e:
            self.error.emit(f"音訊套件載入失敗: {e}")
            return

        try:
            self._vad = webrtcvad.Vad(2)  # 0~3，越大越嚴格
        except Exception as e:
            self.error.emit(f"VAD 初始化失敗: {e}")
            return

        try:
            self._stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=FRAME_SAMPLES,
                device=self.device,
                channels=1,
                dtype="int16",
                callback=self._on_audio,
            )
            self._stream.start()
        except Exception as e:
            self.error.emit(f"無法開啟麥克風: {e}")
            return

        self._last_emit_t = time.monotonic()
        try:
            while not self._stop:
                QThread.msleep(50)
                self._maybe_emit()
        finally:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass

    def _on_audio(self, indata, frames, time_info, status) -> None:
        if status:
            logger.debug("audio status: %s", status)
        try:
            raw = bytes(indata)
            samples = np.frombuffer(raw, dtype=np.int16)
            # Tap：原始音訊給錄音功能（不影響後續辨識流程）
            try:
                self.raw_frame.emit(raw)
            except Exception:
                pass

            # 音量回饋
            if len(samples) > 0:
                rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
                level = min(1.0, rms / 8000.0)
                self.level_changed.emit(level)

            # VAD（在 audio thread 跑，計算很輕）
            try:
                is_speech = self._vad.is_speech(raw, SAMPLE_RATE)
            except Exception:
                is_speech = False

            if is_speech:
                self._frames_since_voice = 0
                self._silence_ms = 0
                self._has_voice_in_window = True
            else:
                self._frames_since_voice += 1
                self._silence_ms += FRAME_DURATION_MS

            # 累入 ring buffer（加鎖避免與主線程讀取競態）
            with self._buffer_lock:
                self._buffer.extend(samples.astype(np.float32) / 32768.0)

            # 偵測「夠長靜音」→ 標記下次發送為 boundary，並清空 buffer
            if self._has_voice_in_window and self._silence_ms >= SILENCE_RESET_MS:
                self._boundary_pending = True
                self._has_voice_in_window = False
        except Exception as e:
            logger.exception("audio callback error: %s", e)

    def _maybe_emit(self) -> None:
        now = time.monotonic()
        if now - self._last_emit_t < self._emit_interval and not self._boundary_pending:
            return
        self._last_emit_t = now

        # 視窗內最近 RECENT_VOICE_WINDOW_MS 是否有人聲
        recent_voice = (
            self._frames_since_voice * FRAME_DURATION_MS
        ) <= RECENT_VOICE_WINDOW_MS
        boundary = self._boundary_pending

        if not (recent_voice or boundary):
            return

        # 安全快照 + 若為 boundary 同時清空（最小化 lock 持有時間）
        with self._buffer_lock:
            if len(self._buffer) < int(MIN_EMIT_MS / 1000.0 * SAMPLE_RATE) and not boundary:
                return
            samples = np.array(self._buffer, dtype=np.float32)
            if boundary:
                self._buffer.clear()

        duration_ms = int(len(samples) * 1000 / SAMPLE_RATE)
        if duration_ms < 200:
            self._boundary_pending = False
            return
        try:
            self.window_ready.emit(
                AudioWindow(samples=samples, duration_ms=duration_ms, is_boundary=boundary)
            )
        except Exception as e:  # pragma: no cover
            logger.exception("emit error: %s", e)

        if boundary:
            self._boundary_pending = False
            self._frames_since_voice = 99999


class AudioCaptureController(QObject):
    window_ready = Signal(object)
    level_changed = Signal(float)
    error = Signal(str)
    raw_frame = Signal(bytes)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[AudioCaptureWorker] = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, device: Optional[int | str] = None) -> None:
        if self.is_running():
            return
        self._thread = QThread()
        self._worker = AudioCaptureWorker(device=device)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.window_ready.connect(self.window_ready)
        self._worker.level_changed.connect(self.level_changed)
        self._worker.error.connect(self.error)
        self._worker.raw_frame.connect(self.raw_frame)
        self._thread.start()

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
        self._thread = None
        self._worker = None
