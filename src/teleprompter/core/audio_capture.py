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
RECENT_VOICE_WINDOW_MS = 2500   # 視窗內近 2.5s 有過語音才送辨識（放寬：低音量麥稀疏的 voice 偵測也能觸發）
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

    def __init__(
        self, device: Optional[int | str] = None, *, loopback: bool = False
    ) -> None:
        super().__init__()
        self.device = device if device not in ("", None) else None
        # loopback=True: Windows 用 WASAPI 擷取「系統輸出」（Teams/Zoom 等觀眾聲音）
        # 非 Windows 或無 WASAPI → fallback 回預設麥克風
        self.loopback = bool(loopback)
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
            # webrtcvad 模式 0~3，越大越嚴格。用 1（原本 2）對低音量麥寬容一些，
            # 避免「第一句辨到之後 webrtcvad 沒再偵測到語音 → 視窗不送辨識」
            self._vad = webrtcvad.Vad(1)
        except Exception as e:
            self.error.emit(f"VAD 初始化失敗: {e}")
            return

        # === 麥克風路徑（預設）===
        device_to_use = self.device
        source_label = "麥克風"
        self._capture_channels = 1
        self._capture_rate = SAMPLE_RATE
        self._resample_buffer = np.zeros(0, dtype=np.float32)

        # === WASAPI loopback 路徑：多組參數 fallback ===
        if self.loopback:
            opened = self._try_open_wasapi_loopback(sd)
            if opened:
                source_label = f"系統輸出 (loopback, ch={self._capture_channels}, rate={self._capture_rate})"
                logger.info("音訊輸入：%s", source_label)
            else:
                logger.warning("WASAPI loopback 全部 fallback 失敗 → 退回麥克風")
        # 若 loopback 失敗或未啟用 → 開麥克風（mono 16k）
        if getattr(self, "_stream", None) is None:
            try:
                self._stream = sd.RawInputStream(
                    samplerate=SAMPLE_RATE,
                    blocksize=FRAME_SAMPLES,
                    device=device_to_use,
                    channels=1,
                    dtype="int16",
                    callback=self._on_audio,
                )
                self._stream.start()
                logger.info("音訊輸入：%s (device=%s)", source_label, device_to_use)
            except Exception as e:
                self.error.emit(f"無法開啟音訊輸入: {e}")
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

    def _try_open_wasapi_loopback(self, sd) -> bool:
        """嘗試多組 (channels, samplerate) 開 WASAPI loopback；找到第一個可用設定。

        WASAPI loopback 的 -9998 來源很多：
        - 有些 driver 不接受 max_output_channels 報的數字（如 7.1 報 8 但實際只接受 2）
        - samplerate 必須 match 系統「Windows 音訊 → 進階 → 預設格式」
        - blocksize 給太小會被拒絕
        所以乾脆按優先序試多組常見組合，第一個 .start() 成功就用它。
        """
        try:
            wasapi_idx = next(
                i for i, h in enumerate(sd.query_hostapis())
                if "WASAPI" in h.get("name", "")
            )
        except StopIteration:
            return False
        out_dev = sd.query_hostapis(wasapi_idx).get("default_output_device", -1)
        if out_dev < 0:
            return False
        # 取裝置回報的「期望」設定當第一順位
        try:
            dev_info = sd.query_devices(out_dev)
            preferred_ch = max(1, int(dev_info.get("max_output_channels", 2)))
            preferred_sr = int(dev_info.get("default_samplerate", 48000))
        except Exception:
            preferred_ch, preferred_sr = 2, 48000
        # 候選組合（去重保留順序）
        candidates: list[tuple[int, int]] = []
        for ch in (preferred_ch, 2, 1):
            for sr in (preferred_sr, 48000, 44100):
                key = (ch, sr)
                if key not in candidates:
                    candidates.append(key)
        extra = sd.WasapiSettings(loopback=True)
        for ch, sr in candidates:
            try:
                stream = sd.RawInputStream(
                    samplerate=sr,
                    blocksize=0,                 # let driver pick native
                    device=out_dev,
                    channels=ch,
                    dtype="int16",
                    callback=self._on_audio,
                    extra_settings=extra,
                )
                stream.start()
                self._stream = stream
                self._capture_channels = ch
                self._capture_rate = sr
                logger.info(
                    "WASAPI loopback opened: device=%s ch=%d rate=%d", out_dev, ch, sr
                )
                return True
            except Exception as e:
                logger.warning(
                    "loopback try (ch=%d, rate=%d) 失敗: %s", ch, sr, e
                )
                continue
        return False

    def _on_audio(self, indata, frames, time_info, status) -> None:
        if status:
            logger.debug("audio status: %s", status)
        try:
            raw_in = bytes(indata)
            ch = getattr(self, "_capture_channels", 1)
            cap_rate = getattr(self, "_capture_rate", SAMPLE_RATE)

            # ---- Fast path：mic 模式（已是 mono 16k 30ms frame）→ 直接 emit ----
            # 不做 concat / astype，避免每 30ms 重複拷貝陣列
            if ch == 1 and cap_rate == SAMPLE_RATE and len(raw_in) == FRAME_SAMPLES * 2:
                samples_view = np.frombuffer(raw_in, dtype=np.int16)
                self._process_mono_16k_frame(raw_in, samples_view)
                return

            # ---- Slow path：loopback / 任意 blocksize → 降混 + 重採樣 + 對齊 30ms frame ----
            samples = np.frombuffer(raw_in, dtype=np.int16)
            # 1) 多聲道（loopback stereo）→ 降混為 mono
            if ch > 1 and len(samples) >= ch:
                trimmed = samples[: (len(samples) // ch) * ch]
                samples = (
                    trimmed.reshape(-1, ch).astype(np.int32).mean(axis=1).astype(np.int16)
                )
            # 2) 重採樣到 16kHz
            if cap_rate != SAMPLE_RATE and len(samples) > 0:
                ratio = SAMPLE_RATE / cap_rate
                new_len = int(len(samples) * ratio)
                if new_len > 0:
                    x_old = np.linspace(0, 1, len(samples), endpoint=False)
                    x_new = np.linspace(0, 1, new_len, endpoint=False)
                    samples = np.interp(x_new, x_old, samples).astype(np.int16)
            # 3) 切成 30ms frame 餵 webrtcvad
            self._resample_buffer = np.concatenate([self._resample_buffer, samples])
            n_frames = len(self._resample_buffer) // FRAME_SAMPLES
            if n_frames == 0:
                return
            consumed = n_frames * FRAME_SAMPLES
            for fi in range(n_frames):
                frame = self._resample_buffer[fi * FRAME_SAMPLES:(fi + 1) * FRAME_SAMPLES].astype(np.int16)
                self._process_mono_16k_frame(frame.tobytes(), frame)
            self._resample_buffer = self._resample_buffer[consumed:]
        except Exception as e:
            logger.exception("audio callback error: %s", e)

    def _process_mono_16k_frame(self, raw: bytes, samples: np.ndarray) -> None:
        """處理已對齊到「mono 16kHz 30ms」的單一 frame：tap / 音量 / VAD / buffer / 邊界。"""
        try:
            self.raw_frame.emit(raw)
        except Exception:
            pass
        if len(samples) > 0:
            rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
            level = min(1.0, rms / 8000.0)
            self.level_changed.emit(level)
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
        with self._buffer_lock:
            self._buffer.extend(samples.astype(np.float32) / 32768.0)
        if self._has_voice_in_window and self._silence_ms >= SILENCE_RESET_MS:
            self._boundary_pending = True
            self._has_voice_in_window = False

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

    def start(
        self, device: Optional[int | str] = None, *, loopback: bool = False
    ) -> None:
        if self.is_running():
            return
        self._thread = QThread()
        self._worker = AudioCaptureWorker(device=device, loopback=loopback)
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
