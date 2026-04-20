"""時間管理：正向計時、倒數、語速健康度燈號、里程碑提示。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from PySide6.QtCore import QObject, QTimer, Signal


class PaceLight(Enum):
    GREEN = "green"   # 節奏剛好
    BLUE = "blue"     # 過快
    YELLOW = "yellow" # 過慢
    GRAY = "gray"     # 未開始或未知


class TimeColor(Enum):
    GREEN = "#4CAF50"
    YELLOW = "#FFC107"
    ORANGE = "#FF9800"
    RED = "#F44336"
    GRAY = "#9E9E9E"


@dataclass
class TimerState:
    elapsed_ms: int = 0
    target_ms: int = 0
    remaining_ms: int = 0
    overrun_ms: int = 0
    pace: PaceLight = PaceLight.GRAY
    time_color: TimeColor = TimeColor.GRAY
    milestone_triggered: int = -1  # 最近觸發的里程碑（剩餘秒數），-1 表示無


def format_mmss(ms: int) -> str:
    sign = "-" if ms < 0 else ""
    secs = abs(ms) // 1000
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{sign}{h:02d}:{m:02d}:{s:02d}"
    return f"{sign}{m:02d}:{s:02d}"


class TimerController(QObject):
    """以 100 ms 心跳更新時間狀態，並透過 signal 發送給 UI。"""

    state_changed = Signal(object)        # TimerState
    milestone_reached = Signal(int)       # 觸發了哪個里程碑（剩餘秒數）
    time_up = Signal()                    # 倒數歸零瞬間

    TICK_MS = 100

    def __init__(
        self,
        *,
        target_sec: int = 900,
        milestones_sec: Iterable[int] = (300, 60),
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._target_ms = max(0, int(target_sec) * 1000)
        self._milestones = sorted(set(int(s) for s in milestones_sec), reverse=True)
        self._milestones_pending = list(self._milestones)
        self._elapsed_ms = 0
        self._running = False
        self._time_up_emitted = False

        self._progress_callback = lambda: 0.0  # 取得講稿進度 0~1（由 MainWindow 注入）

        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)

    # ---------- 公開 API ----------

    def set_target_seconds(self, seconds: int) -> None:
        self._target_ms = max(0, int(seconds) * 1000)
        self._milestones_pending = list(self._milestones)
        self._time_up_emitted = False
        self._emit_state()

    def set_milestones(self, milestones_sec: Iterable[int]) -> None:
        self._milestones = sorted(set(int(s) for s in milestones_sec), reverse=True)
        self._milestones_pending = [
            m for m in self._milestones if (self._target_ms - self._elapsed_ms) // 1000 >= m
        ]

    def set_progress_callback(self, fn) -> None:
        """fn() -> float in [0, 1]，講稿目前進度。"""
        self._progress_callback = fn

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._timer.start()

    def pause(self) -> None:
        self._running = False
        self._timer.stop()
        self._emit_state()

    def reset(self) -> None:
        self._running = False
        self._timer.stop()
        self._elapsed_ms = 0
        self._milestones_pending = list(self._milestones)
        self._time_up_emitted = False
        self._emit_state()

    def is_running(self) -> bool:
        return self._running

    @property
    def elapsed_ms(self) -> int:
        return self._elapsed_ms

    @property
    def target_ms(self) -> int:
        return self._target_ms

    # ---------- 內部 ----------

    def _tick(self) -> None:
        self._elapsed_ms += self.TICK_MS
        # 里程碑檢查
        remaining_sec = max(0, (self._target_ms - self._elapsed_ms) // 1000)
        for m in list(self._milestones_pending):
            if remaining_sec <= m:
                self._milestones_pending.remove(m)
                self.milestone_reached.emit(m)
                break  # 一次 tick 只觸發一個
        # 超時瞬間
        if (
            not self._time_up_emitted
            and self._target_ms > 0
            and self._elapsed_ms >= self._target_ms
        ):
            self._time_up_emitted = True
            self.time_up.emit()
        self._emit_state()

    def _compute_state(self) -> TimerState:
        target = self._target_ms
        elapsed = self._elapsed_ms
        remaining = max(0, target - elapsed)
        overrun = max(0, elapsed - target)

        # 顏色分級
        if target == 0:
            color = TimeColor.GRAY
        elif overrun > 0:
            color = TimeColor.RED
        else:
            ratio = remaining / target
            if ratio > 0.25:
                color = TimeColor.GREEN
            elif ratio > 0.10:
                color = TimeColor.YELLOW
            else:
                color = TimeColor.ORANGE

        # 語速健康度
        pace = PaceLight.GRAY
        if target > 0 and elapsed > 5000 and self._running:
            time_progress = elapsed / target
            try:
                script_progress = max(0.0, min(1.0, float(self._progress_callback())))
            except Exception:
                script_progress = 0.0
            delta = script_progress - time_progress
            if delta > 0.10:
                pace = PaceLight.BLUE  # 講稿超前 = 太快
            elif delta < -0.10:
                pace = PaceLight.YELLOW  # 講稿落後 = 太慢
            else:
                pace = PaceLight.GREEN

        last_triggered = -1
        if len(self._milestones_pending) < len(self._milestones):
            done = [m for m in self._milestones if m not in self._milestones_pending]
            if done:
                last_triggered = min(done)

        return TimerState(
            elapsed_ms=elapsed,
            target_ms=target,
            remaining_ms=remaining,
            overrun_ms=overrun,
            pace=pace,
            time_color=color,
            milestone_triggered=last_triggered,
        )

    def _emit_state(self) -> None:
        self.state_changed.emit(self._compute_state())
