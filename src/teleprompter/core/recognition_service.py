"""語音辨識資源的仲裁層。

辨識器與麥克風是**單一實體資源**：Whisper 的語言只能在建構時決定（換語言＝
重載模型），音訊也只有一條 stream（麥克風與系統音訊互斥）。而想用它的地方
不只一處——跟讀要「聽自己、中文、帶講稿偏向」，Q&A 要「聽觀眾、英文、不帶
偏向」。

先前沒有仲裁，兩邊各自呼叫 start／stop，形成最糟的組合：資源已在跑時
`start()` 靜默忽略新參數，而切模式的程式又無條件 stop+start 覆蓋別人。結果
是「Q&A 開著按開始 → 麥克風其實沒開，畫面不動但 UI 說辨識中」這類無聲故障。

這一層把「誰要用、要什麼設定」變成明確狀態：

- `acquire(owner, need)` 登記需求，`release(owner)` 退出
- `ensure()` 只在**實際設定與應有設定不同**時才重啟，避免無謂的模型重載
- 需求衝突時依 `PRIORITY` 決定誰上場，其餘登記者保留、待對方釋放後自動回復
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from PySide6.QtCore import QObject, Signal

# 誰的需求優先：Q&A 進行時觀眾正在等回答，讓它先用
PRIORITY = ("qa", "follow")

SOURCE_MIC = "mic"
SOURCE_LOOPBACK = "loopback"


@dataclass(frozen=True)
class RecognitionNeed:
    """一個使用者對辨識資源的需求。"""

    language: str = "zh"
    initial_prompt: str = ""
    source: str = SOURCE_MIC          # mic | loopback
    model_size: str = "large-v3-turbo"
    compute_type: str = "auto"
    device: str = ""                  # 麥克風裝置（loopback 時忽略）

    def same_recognizer_as(self, other: Optional["RecognitionNeed"]) -> bool:
        """辨識器需不需要重啟——只有這幾個參數會綁在 worker 建構上。"""
        if other is None:
            return False
        return (
            self.language == other.language
            and self.model_size == other.model_size
            and self.compute_type == other.compute_type
        )

    def same_audio_as(self, other: Optional["RecognitionNeed"]) -> bool:
        if other is None:
            return False
        return self.source == other.source and self.device == other.device


class RecognitionService(QObject):
    """管理辨識器與音訊的唯一入口。"""

    # active_owner（沒有人用時為 ""）, 是否正在切換
    state_changed = Signal(str, bool)

    def __init__(self, recognizer, audio, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._recognizer = recognizer
        self._audio = audio
        self._needs: dict[str, RecognitionNeed] = {}
        self._applied: Optional[RecognitionNeed] = None
        self._active_owner: str = ""
        self._switching = False

    # ---------- 查詢 ----------

    @property
    def active_owner(self) -> str:
        """目前實際佔用資源的登記者（沒有就是空字串）。"""
        return self._active_owner

    @property
    def applied_need(self) -> Optional[RecognitionNeed]:
        """目前實際套用的設定（供 UI 顯示與測試檢查）。"""
        return self._applied

    def is_active(self, owner: str) -> bool:
        return self._active_owner == owner

    def is_waiting(self, owner: str) -> bool:
        """有登記但被優先者壓著（例如 Q&A 進行時的跟讀）。"""
        return owner in self._needs and self._active_owner != owner

    def owners(self) -> list[str]:
        return list(self._needs)

    # ---------- 登記 / 退出 ----------

    def acquire(self, owner: str, need: RecognitionNeed) -> None:
        self._needs[owner] = need
        self.ensure()

    def release(self, owner: str) -> None:
        if self._needs.pop(owner, None) is None:
            return
        self.ensure()

    def update(self, owner: str, **changes) -> None:
        """改某個登記者的需求（例如使用者換了辨識語言）。"""
        current = self._needs.get(owner)
        if current is None:
            return
        self._needs[owner] = replace(current, **changes)
        self.ensure()

    # ---------- 核心：只在必要時重啟 ----------

    def _winner(self) -> Optional[str]:
        for owner in PRIORITY:
            if owner in self._needs:
                return owner
        return next(iter(self._needs), None)

    def ensure(self) -> None:
        """讓實際狀態與應有狀態一致。"""
        winner = self._winner()
        target = self._needs.get(winner) if winner else None

        if target is None:
            self._stop_all()
            return

        recognizer_ok = (
            self._recognizer.is_running() and target.same_recognizer_as(self._applied)
        )
        audio_ok = self._audio.is_running() and target.same_audio_as(self._applied)

        if recognizer_ok and audio_ok:
            # 設定沒變 → 不重啟。但講稿偏向是唯一能熱改的參數，換講稿時
            # 要讓辨識器知道，否則對齊仍以舊稿為準。
            self._maybe_update_prompt(target)
            self._applied = target
            self._set_active(winner)
            return

        self._switching = True
        self.state_changed.emit(winner, True)
        try:
            if not recognizer_ok:
                if self._recognizer.is_running():
                    self._recognizer.stop()
                self._recognizer.start(
                    model_size=target.model_size,
                    language=target.language,
                    compute_type=target.compute_type,
                    initial_prompt=target.initial_prompt,
                )

            if not audio_ok:
                if self._audio.is_running():
                    self._audio.stop()
                self._audio.start(
                    device=self._device_arg(target),
                    loopback=(target.source == SOURCE_LOOPBACK),
                )
        finally:
            self._switching = False

        self._applied = target
        self._set_active(winner)

    def _maybe_update_prompt(self, target: RecognitionNeed) -> None:
        """只有講稿偏向不同時熱更新（換語言才需要重載模型）。"""
        if self._applied is not None and target.initial_prompt == self._applied.initial_prompt:
            return
        update = getattr(self._recognizer, "update_prompt", None)
        if callable(update):
            update(target.initial_prompt)

    def _stop_all(self) -> None:
        if self._audio.is_running():
            self._audio.stop()
        if self._recognizer.is_running():
            self._recognizer.stop()
        self._applied = None
        self._set_active("")

    def _set_active(self, owner: str) -> None:
        if owner != self._active_owner:
            self._active_owner = owner
        self.state_changed.emit(self._active_owner, False)

    @staticmethod
    def _device_arg(need: RecognitionNeed) -> int | str | None:
        """設定裡的裝置字串轉成 sounddevice 認得的形式。"""
        if need.source == SOURCE_LOOPBACK or not need.device:
            return None
        try:
            return int(need.device)
        except ValueError:
            return need.device
