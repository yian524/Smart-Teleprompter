"""faster-whisper 串流辨識器（LocalAgreement-1）。

設計：
- 每收到一個 AudioWindow（最近 N 秒的音訊），對整個視窗做一次 transcribe。
- 用「LocalAgreement」演算法找出與前一次推論共同的前綴 → 那部分是穩定的，可 commit。
- 只把「新 commit 的 delta」emit 出去，避免把不穩定的尾巴餵給對齊引擎造成位置抖動。
- AudioWindow.is_boundary == True 時：當前完整推論直接全 commit 並重置 hypothesis。
- 模型 busy 時新進來的視窗會 drop（只保留最新一份），避免延遲堆積。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import threading
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from .audio_capture import AudioWindow, SAMPLE_RATE
from .transcript_loader import normalize_text

logger = logging.getLogger(__name__)


# ============================================================
# Windows: 註冊 NVIDIA cuBLAS / cuDNN DLL 路徑
# ============================================================

def _register_nvidia_dll_paths() -> None:
    if sys.platform != "win32" or not hasattr(os, "add_dll_directory"):
        return
    for pkg_name in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            spec = importlib.util.find_spec(pkg_name)
        except (ImportError, ValueError):
            continue
        if spec is None or not spec.submodule_search_locations:
            continue
        for base in spec.submodule_search_locations:
            bin_dir = os.path.join(base, "bin")
            if os.path.isdir(bin_dir):
                try:
                    os.add_dll_directory(bin_dir)
                    if bin_dir not in os.environ.get("PATH", ""):
                        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
                    logger.info("registered NVIDIA DLL dir: %s", bin_dir)
                except OSError as e:  # pragma: no cover
                    logger.warning("failed to register %s: %s", bin_dir, e)


def _detect_compute_type() -> tuple[str, str]:
    _register_nvidia_dll_paths()
    try:
        import ctranslate2  # type: ignore

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:  # pragma: no cover
        pass
    return "cpu", "int8"


# ============================================================
# LocalAgreement helpers
# ============================================================

def _common_prefix_len_chars(a: str, b: str) -> int:
    """回傳兩個字串字元級的共同前綴長度。"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _common_prefix_normalized(a: str, b: str) -> int:
    """以標準化文字比對共同前綴，回傳套用到原 a 的字元位置。

    這是為了讓「Transformer」vs「transformer」這類差異不被視為不同。
    回傳長度是「a 中前多少字元屬於穩定前綴」。
    """
    na = normalize_text(a)
    nb = normalize_text(b)
    common_n = _common_prefix_len_chars(na, nb)
    if common_n == 0:
        return 0
    # 把 common_n 個 normalized 字元轉回 a 中對應的字元數
    # 簡單法：逐字累積 a 的 normalized 表示，直到對齊到 common_n
    count_norm = 0
    for i, ch in enumerate(a):
        if normalize_text(ch):
            count_norm += len(normalize_text(ch))
            if count_norm >= common_n:
                return i + 1
    return len(a)


# ============================================================
# Worker
# ============================================================

class SpeechRecognizerWorker(QObject):
    text_committed = Signal(str)        # 新穩定下來的 delta 文字
    hypothesis = Signal(str)            # 目前完整 hypothesis（含尚未穩定的尾巴），給 status bar 顯示
    model_loaded = Signal(str)
    model_loading = Signal(str)
    error = Signal(str)

    def __init__(
        self,
        *,
        model_size: str = "large-v3-turbo",
        language: str = "zh",
        compute_type: str = "auto",
        initial_prompt: str = "",
    ) -> None:
        super().__init__()
        self.model_size = model_size
        self.language = None if language == "auto" else language
        self.compute_type_pref = compute_type
        self.initial_prompt = initial_prompt
        self._stop = False
        self._model = None

        # 最新待處理的 window（只保留一份，後到的覆蓋前面）
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending: Optional[AudioWindow] = None

        # LocalAgreement 狀態
        self._prev_hypothesis: str = ""
        self._committed_in_current_window: int = 0  # 已 emit 過的 prefix 字元數（指 hypothesis 的前綴）

    # ---- 對外控制 ----

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def update_prompt(self, prompt: str) -> None:
        self.initial_prompt = prompt[:200] if prompt else ""

    def enqueue_window(self, window: AudioWindow) -> None:
        with self._cv:
            self._pending = window
            self._cv.notify_all()

    # ---- worker 主迴圈 ----

    def run(self) -> None:
        try:
            self.model_loading.emit("載入 faster-whisper 模型中…")
            from faster_whisper import WhisperModel
        except Exception as e:
            self.error.emit(f"無法載入 faster-whisper: {e}")
            return

        device, default_ct = _detect_compute_type()
        compute_type = (
            default_ct if self.compute_type_pref == "auto" else self.compute_type_pref
        )

        try:
            self._model = WhisperModel(
                self.model_size,
                device=device,
                compute_type=compute_type,
            )
            self.model_loaded.emit(f"{device}/{compute_type}")
            logger.info("Whisper loaded: %s on %s/%s", self.model_size, device, compute_type)
            self._warmup()
        except Exception as e:
            logger.warning("primary load failed: %s — fallback to cpu/int8", e)
            try:
                self._model = WhisperModel(
                    self.model_size, device="cpu", compute_type="int8"
                )
                self.model_loaded.emit("cpu/int8 (fallback)")
                self._warmup()
            except Exception as e2:
                self.error.emit(f"模型載入失敗: {e2}")
                return

        cuda_failed_once = False

        while True:
            with self._cv:
                while self._pending is None and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                window = self._pending
                self._pending = None  # 取出後清空，下次 enqueue 新的會覆蓋

            try:
                self._process_window(window)
            except RuntimeError as e:
                msg = str(e).lower()
                is_cuda_dll_err = (
                    "cublas" in msg or "cudnn" in msg or "cuda" in msg or "dll" in msg
                )
                if is_cuda_dll_err and not cuda_failed_once:
                    cuda_failed_once = True
                    logger.warning("CUDA runtime error, falling back to CPU INT8: %s", e)
                    self.error.emit("CUDA 推論失敗，已切換到 CPU 模式繼續執行。")
                    try:
                        from faster_whisper import WhisperModel
                        self._model = WhisperModel(
                            self.model_size, device="cpu", compute_type="int8"
                        )
                        self.model_loaded.emit("cpu/int8 (cuda fallback)")
                        try:
                            self._process_window(window)
                        except Exception as e2:
                            logger.exception("retry on cpu also failed: %s", e2)
                            self.error.emit(f"CPU 重試失敗: {e2}")
                    except Exception as e2:
                        logger.exception("cpu fallback load failed: %s", e2)
                        self.error.emit(f"CPU 模式載入失敗: {e2}")
                else:
                    logger.exception("transcribe error: %s", e)
                    self.error.emit(f"辨識錯誤: {e}")
            except Exception as e:
                logger.exception("transcribe error: %s", e)
                self.error.emit(f"辨識錯誤: {e}")

    # ---- 內部 ----

    def _warmup(self) -> None:
        """預熱：先跑一次 1 秒靜音，避免第一次正式推論時的延遲。"""
        try:
            import numpy as np
            dummy = np.zeros(SAMPLE_RATE, dtype=np.float32)
            list(self._model.transcribe(
                dummy,
                language=self.language,
                beam_size=1,
                vad_filter=False,
                condition_on_previous_text=False,
            )[0])
            logger.info("model warmup done")
        except Exception as e:  # pragma: no cover
            logger.warning("warmup failed: %s", e)

    def _process_window(self, window: AudioWindow) -> None:
        text = self._transcribe(window.samples)
        if not text:
            if window.is_boundary:
                self._reset_hypothesis()
            return

        # 過濾 Whisper hallucination（純靜音/噪音時 Whisper 會吐出重複片段）
        # 例：「我們採用了一個小型的小型的小型的小型...」
        if self._is_hallucination(text):
            logger.info("filtered hallucination: %r", text[:60])
            if window.is_boundary:
                self._reset_hypothesis()
            return

        self.hypothesis.emit(text)

        if window.is_boundary:
            # 直接把 hypothesis 全部當穩定文字 emit（最後 commit）
            delta = text[self._committed_in_current_window:]
            if delta.strip():
                self.text_committed.emit(delta)
            self._reset_hypothesis()
            return

        # LocalAgreement-1：當前 hypothesis 與上一次 hypothesis 的共同前綴 = 穩定區
        stable_len = _common_prefix_normalized(text, self._prev_hypothesis)
        if stable_len > self._committed_in_current_window:
            delta = text[self._committed_in_current_window:stable_len]
            if delta.strip():
                self.text_committed.emit(delta)
            self._committed_in_current_window = stable_len

        # 若 text 比之前短或完全不同 → 視窗已滾動，重設追蹤
        # Bug 修正：原本 text.startswith(text[:N]) 永遠 True，等於這條檢查無效
        # 改為比對「之前的 hypothesis 前綴」是否仍為當前 text 的前綴
        prev_committed_prefix = self._prev_hypothesis[:self._committed_in_current_window]
        if prev_committed_prefix and not text.startswith(prev_committed_prefix):
            self._committed_in_current_window = 0

        self._prev_hypothesis = text

    @staticmethod
    def _is_hallucination(text: str) -> bool:
        """偵測 Whisper 對純靜音/噪音的 hallucination。

        典型特徵：
        1. 同一個 N-gram (3-6 字) 在文字中重複出現 ≥ 3 次
        2. 文字長度 ≥ 12 字但獨特字元比例極低（< 30%）
        """
        s = text.strip()
        if len(s) < 8:
            return False
        # 檢查 3-gram 到 6-gram 是否被重複多次
        for ngram_len in (4, 3, 5, 6):
            if len(s) < ngram_len * 3:
                continue
            seen: dict[str, int] = {}
            for i in range(len(s) - ngram_len + 1):
                ng = s[i:i + ngram_len]
                if not ng.strip():
                    continue
                seen[ng] = seen.get(ng, 0) + 1
                if seen[ng] >= 3:
                    return True
        # 檢查獨特字元比例
        if len(s) >= 12:
            unique = len(set(s))
            if unique / len(s) < 0.30:
                return True
        return False

    def _reset_hypothesis(self) -> None:
        self._prev_hypothesis = ""
        self._committed_in_current_window = 0

    # Whisper 輸出後要剝除的標點（讓比對與顯示更乾淨）
    _PUNCT_TO_STRIP = "。，！？!?；;,：:、…．·"

    def _transcribe(self, samples) -> str:
        segments_iter, info = self._model.transcribe(
            samples,
            language=self.language,
            initial_prompt=self.initial_prompt or None,
            beam_size=5,
            best_of=1,
            temperature=0.0,
            vad_filter=True,
            condition_on_previous_text=False,
            no_speech_threshold=0.7,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )
        pieces: list[str] = []
        for s in segments_iter:
            pieces.append(s.text)
        text = "".join(pieces).strip()
        return self._strip_punctuation(text)

    @classmethod
    def _strip_punctuation(cls, text: str) -> str:
        """剝除常見標點，避免污染 hypothesis 顯示與 commit。"""
        if not text:
            return text
        out = []
        for ch in text:
            if ch in cls._PUNCT_TO_STRIP:
                out.append(" ")  # 標點 → 空白（保留斷詞）
            else:
                out.append(ch)
        # 多個空白 → 單一空白
        result = "".join(out)
        while "  " in result:
            result = result.replace("  ", " ")
        return result.strip()


# ============================================================
# Controller (對外 API)
# ============================================================

class SpeechRecognizerController(QObject):
    text_committed = Signal(str)
    hypothesis = Signal(str)
    model_loaded = Signal(str)
    model_loading = Signal(str)
    error = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[SpeechRecognizerWorker] = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(
        self,
        *,
        model_size: str = "large-v3-turbo",
        language: str = "zh",
        compute_type: str = "auto",
        initial_prompt: str = "",
    ) -> None:
        if self.is_running():
            return
        self._thread = QThread()
        self._worker = SpeechRecognizerWorker(
            model_size=model_size,
            language=language,
            compute_type=compute_type,
            initial_prompt=initial_prompt,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.text_committed.connect(self.text_committed)
        self._worker.hypothesis.connect(self.hypothesis)
        self._worker.model_loaded.connect(self.model_loaded)
        self._worker.model_loading.connect(self.model_loading)
        self._worker.error.connect(self.error)
        self._thread.start()

    def update_prompt(self, prompt: str) -> None:
        if self._worker:
            self._worker.update_prompt(prompt)

    def enqueue_window(self, window: AudioWindow) -> None:
        if self._worker:
            self._worker.enqueue_window(window)

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
        self._thread = None
        self._worker = None
