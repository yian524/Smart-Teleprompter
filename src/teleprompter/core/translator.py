"""即時翻譯服務（Q&A 時用於翻譯外國提問）。

使用 deep-translator (Google Translate 免費 API)。需要網路連線。
若離線或 API 失敗，回傳原文（gracefully degrade）。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal

logger = logging.getLogger(__name__)


class TranslatorWorker(QObject):
    translated = Signal(str, str)  # (source_text, translated_text)
    error = Signal(str)

    def __init__(self, source_lang: str = "auto", target_lang: str = "zh-TW") -> None:
        super().__init__()
        self.source_lang = source_lang
        self.target_lang = target_lang
        self._lock = threading.Lock()
        self._pending: Optional[str] = None
        self._cv = threading.Condition(self._lock)
        self._stop = False

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def enqueue(self, text: str) -> None:
        with self._cv:
            self._pending = text  # 只保留最新
            self._cv.notify_all()

    def run(self) -> None:
        try:
            from deep_translator import GoogleTranslator
        except ImportError as e:
            self.error.emit(f"翻譯套件未安裝: {e}")
            return

        translator = GoogleTranslator(
            source=self.source_lang,
            target=self.target_lang,
        )

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
            try:
                result = translator.translate(text)
                if result:
                    self.translated.emit(text, result)
            except Exception as e:
                logger.warning("translate failed: %s", e)
                self.error.emit(str(e))


class TranslatorController(QObject):
    translated = Signal(str, str)
    error = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[TranslatorWorker] = None
        self._source_lang = "auto"
        self._target_lang = "zh-TW"

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, source_lang: str = "auto", target_lang: str = "zh-TW") -> None:
        if self.is_running():
            return
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._thread = QThread()
        self._worker = TranslatorWorker(source_lang, target_lang)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.translated.connect(self.translated)
        self._worker.error.connect(self.error)
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
