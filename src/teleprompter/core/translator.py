"""即時翻譯服務（Q&A 時用於翻譯外國提問）。

主引擎：Argos Translate（本地離線、穩定、無 rate limit）
  - 首次使用時下載 en→zh 模型（約 100MB，一次性）
  - 離線運作，不受網路影響
  - 翻譯結果為簡體中文 → 用 OpenCC 轉為繁體中文

Fallback：deep-translator（Google Translate 網路 API）
  - 當 Argos 不可用時使用
  - 需要網路

優化：
  - 同樣文字 1 秒內不重複翻譯（節流）
  - 只翻譯含英文字母的文字（純中文無需翻譯）
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

logger = logging.getLogger(__name__)

# 含英文字母才需要翻譯（純中文無意義）
_EN_CHAR_RE = re.compile(r"[A-Za-z]")


def _has_english(text: str) -> bool:
    return bool(_EN_CHAR_RE.search(text))


class TranslatorWorker(QObject):
    translated = Signal(str, str)  # (source_text, translated_text)
    error = Signal(str)
    engine_ready = Signal(str)  # emit engine name when ready

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending: Optional[str] = None
        self._stop = False
        self._last_source: str = ""
        self._last_translated_at: float = 0.0
        self._argos_ready = False
        self._s2tw = None  # OpenCC converter

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def enqueue(self, text: str) -> None:
        with self._cv:
            self._pending = text
            self._cv.notify_all()

    def run(self) -> None:
        # 初始化：先試 Argos，失敗則降級到 Google
        self._init_engines()

        while True:
            with self._cv:
                while self._pending is None and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                text = self._pending
                self._pending = None

            if not text or not text.strip():
                continue
            # 節流：同樣文字 1 秒內不重複翻譯
            now = time.monotonic()
            if text == self._last_source and now - self._last_translated_at < 1.0:
                continue
            # 只翻譯含英文的
            if not _has_english(text):
                continue
            self._last_source = text
            self._last_translated_at = now

            try:
                result = self._translate(text)
                if result:
                    self.translated.emit(text, result)
            except Exception as e:
                logger.warning("translate failed: %s", e)
                self.error.emit(str(e))

    def _init_engines(self) -> None:
        # 嘗試載入 Argos Translate
        try:
            import argostranslate.package as ap
            import argostranslate.translate as at
            # 檢查 en→zh 模型是否已安裝
            installed_languages = at.get_installed_languages()
            langs = {lang.code: lang for lang in installed_languages}
            if "en" in langs and "zh" in langs:
                self._argos_ready = True
            else:
                # 嘗試下載
                logger.info("Argos: 下載 en→zh 模型中…")
                ap.update_package_index()
                for pkg in ap.get_available_packages():
                    if pkg.from_code == "en" and pkg.to_code == "zh":
                        ap.install_from_path(pkg.download())
                        self._argos_ready = True
                        break
            if self._argos_ready:
                logger.info("Argos en→zh 就緒")
                # 簡→繁轉換
                try:
                    from opencc import OpenCC
                    self._s2tw = OpenCC("s2tw")
                except Exception as e:
                    logger.warning("opencc 不可用，保留簡體: %s", e)
                self.engine_ready.emit("Argos (離線)")
                return
        except Exception as e:
            logger.warning("Argos 初始化失敗: %s", e)

        # Fallback: Google Translate
        try:
            from deep_translator import GoogleTranslator  # noqa
            self.engine_ready.emit("Google (線上)")
        except ImportError:
            self.error.emit("翻譯引擎無法載入（Argos 與 Google 都失敗）")

    def _translate(self, text: str) -> str:
        # 優先 Argos
        if self._argos_ready:
            try:
                import argostranslate.translate as at
                result = at.translate(text, "en", "zh")
                if result and self._s2tw is not None:
                    result = self._s2tw.convert(result)
                return result
            except Exception as e:
                logger.warning("Argos translate failed, fallback to Google: %s", e)

        # Fallback Google
        try:
            from deep_translator import GoogleTranslator
            return GoogleTranslator(source="auto", target="zh-TW").translate(text)
        except Exception as e:
            raise RuntimeError(f"翻譯全部引擎失敗: {e}")


class TranslatorController(QObject):
    translated = Signal(str, str)
    error = Signal(str)
    engine_ready = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[TranslatorWorker] = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, source_lang: str = "auto", target_lang: str = "zh-TW") -> None:
        """為相容舊 API 保留參數，實際 Argos 固定 en→zh→繁。"""
        if self.is_running():
            return
        self._thread = QThread()
        self._worker = TranslatorWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.translated.connect(self.translated)
        self._worker.error.connect(self.error)
        self._worker.engine_ready.connect(self.engine_ready)
        self._thread.start()

    def translate(self, text: str) -> None:
        if self._worker is not None:
            self._worker.enqueue(text)

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
        self._thread = None
        self._worker = None
