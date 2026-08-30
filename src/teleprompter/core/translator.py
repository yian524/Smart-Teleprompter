"""即時翻譯服務（Q&A 時把觀眾的提問翻成中文）。

引擎：**CTranslate2 + NLLB-200 distilled 600M**（約 1.2GB）

為什麼是它：辨識器（faster-whisper）本來就跑在 CTranslate2 上，翻譯共用同一個
推論引擎就不會多出一條相依鏈。先前用的 Argos Translate 會經由 stanza 拉進
PyTorch，不只讓打包多背 300MB 以上，打包後 UPX 壓縮 torch 的 DLL 還會讓它在
exe 裡初始化失敗（`operator prims::abs does not exist`），現場等於沒有翻譯。

特性：
  - 完全離線（模型與 Whisper 放同一個 HuggingFace 快取）
  - CPU 上每句約 0.3 秒，足以即時
  - 直接輸出繁體（`zho_Hant`），OpenCC 僅作保險
  - 同樣文字 1 秒內不重複翻譯（節流）

先前試過較小的 Opus-MT（300MB），但它在這類問句上會重複同一句並產生疊字
（「標籤標籤滲漏滲漏」），換解碼參數也救不回來，因此改用 NLLB。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

logger = logging.getLogger(__name__)

# 含英文字母才需要翻譯（純中文無意義）
_EN_CHAR_RE = re.compile(r"[A-Za-z]")


_CJK_CHAR_RE = re.compile(r"[一-鿿]")


def _has_english(text: str) -> bool:
    return bool(_EN_CHAR_RE.search(text))


def _has_cjk(text: str) -> bool:
    return bool(_CJK_CHAR_RE.search(text))


class TranslatorWorker(QObject):
    translated = Signal(str, str)  # (source_text, translated_text)
    error = Signal(str)
    engine_ready = Signal(str)         # engine name when ready
    status_changed = Signal(str)       # human-readable progress text

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending: Optional[str] = None
        self._stop = False
        self._last_source: str = ""
        self._last_translated_at: float = 0.0
        self._ready = False
        self._translator = None     # ctranslate2.Translator
        self._sp_src = None         # 來源端 SentencePiece
        self._sp_tgt = None         # 目標端 SentencePiece
        self._s2tw = None           # OpenCC 簡→繁
        self._src_code = self.DEFAULT_SRC
        self._tgt_code = self.DEFAULT_TGT

    def _is_already_target(self, text: str) -> bool:
        """輸出語言就是輸入語言時跳過。

        舊版寫死「沒有英文字母就不翻」，於是中文→英文這個方向永遠不會動。
        """
        if self._tgt_code.startswith("zho"):
            return not _has_english(text)
        if self._tgt_code.startswith("eng"):
            return not _has_cjk(text)
        return False

    def set_direction(self, source_lang: str, target_lang: str) -> None:
        """設定翻譯方向；認不得的代碼就退回預設（en→zh）。"""
        self._src_code = self.LANG_CODES.get(
            (source_lang or "").split("-")[0].lower(), self.DEFAULT_SRC
        )
        self._tgt_code = self.LANG_CODES.get(
            (target_lang or "").split("-")[0].lower(), self.DEFAULT_TGT
        )

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
            if self._is_already_target(text):
                # 已經是目標語言就不必翻（例如英文→中文時收到純中文）
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

    MODEL_REPO = "entai2965/nllb-200-distilled-600M-ctranslate2"
    # NLLB 用 FLORES-200 語言代碼；直接產繁體，省一次轉換
    LANG_CODES = {"en": "eng_Latn", "zh": "zho_Hant", "ja": "jpn_Jpan"}
    DEFAULT_SRC = "eng_Latn"
    DEFAULT_TGT = "zho_Hant"

    def _init_engines(self) -> None:
        self.status_changed.emit("初始化翻譯引擎…")
        try:
            import ctranslate2
            import sentencepiece as spm
            from huggingface_hub import snapshot_download

            self.status_changed.emit("載入翻譯模型（首次使用需下載約 1.2GB）…")
            path = snapshot_download(self.MODEL_REPO)
            self._translator = ctranslate2.Translator(
                path, device="cpu", compute_type="int8",
            )
            # NLLB 來源與目標共用同一個 sentencepiece 模型
            self._sp_src = spm.SentencePieceProcessor(
                os.path.join(path, "sentencepiece.bpe.model")
            )
            self._sp_tgt = self._sp_src
            self._ready = True
        except Exception as e:
            logger.warning("翻譯模型載入失敗: %s", e)
            self.status_changed.emit(f"離線翻譯不可用：{e}")
            self.engine_ready.emit("翻譯不可用")
            self.error.emit(f"翻譯模型載入失敗：{e}")
            return

        try:
            from opencc import OpenCC
            self._s2tw = OpenCC("s2tw")
        except Exception as e:
            logger.warning("opencc 不可用，將保留簡體: %s", e)

        self.engine_ready.emit("NLLB-200（離線）")
        self.status_changed.emit("翻譯引擎就緒")

    def _translate(self, text: str) -> str:
        if not self._ready or self._translator is None:
            raise RuntimeError("翻譯引擎尚未就緒")
        source = text.strip()
        if not source:
            return ""
        # 語音辨識吐出的片段常常沒有句末標點，而 MT 模型少了它就容易把同一句
        # 重譯好幾遍。補一個句點再翻，輸出乾淨很多。
        if source[-1] not in ".?!。？！,，":
            source += "."
        # NLLB 的輸入要標記來源語言、並用 target_prefix 指定目標語言
        tokens = self._sp_src.encode(source, out_type=str) + ["</s>", self._src_code]
        results = self._translator.translate_batch(
            [tokens],
            target_prefix=[[self._tgt_code]],
            beam_size=4,
            max_decoding_length=160,
        )
        hyp = results[0].hypotheses[0]
        if hyp and hyp[0] == self._tgt_code:
            hyp = hyp[1:]
        out = self._clean_output(self._sp_tgt.decode(hyp))
        # 目標已是繁體；保險起見再過一次（簡體殘留時才會有作用）
        if self._s2tw is not None:
            out = self._s2tw.convert(out)
        return out

    @staticmethod
    def _clean_output(text: str) -> str:
        """截掉 MT 模型常見的重複尾巴。

        Opus-MT 在輸入缺句末標點時，容易把同一句翻好幾遍再接一串問號。
        取到第一個句末標點就夠了，其餘視為雜訊。
        """
        text = text.strip()
        if not text:
            return text
        for idx, ch in enumerate(text):
            if ch in "。？！?!":
                head = text[: idx + 1].strip()
                if len(head) >= 4:      # 太短可能是誤截（例如開頭就是問號）
                    return head
        # 沒有句末標點 → 去掉尾端重複的空白與符號
        return re.sub(r"[\s　?？!！.。]{2,}$", "", text).strip()


class TranslatorController(QObject):
    translated = Signal(str, str)
    error = Signal(str)
    engine_ready = Signal(str)
    status_changed = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[TranslatorWorker] = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, source_lang: str = "en", target_lang: str = "zh") -> None:
        """啟動翻譯執行緒。

        語言參數以前被忽略（Argos 固定 en→zh），現在真的會生效：
        Q&A 面板選英文辨識就 en→zh，選中文就 zh→en。
        """
        if self.is_running():
            return
        self._thread = QThread()
        self._worker = TranslatorWorker()
        self._worker.set_direction(source_lang, target_lang)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.translated.connect(self.translated)
        self._worker.error.connect(self.error)
        self._worker.engine_ready.connect(self.engine_ready)
        self._worker.status_changed.connect(self.status_changed)
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
