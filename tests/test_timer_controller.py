"""TimerController 單元測試 — 確保計時/倒數在報告全程穩定。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from teleprompter.core.timer_controller import (
    PaceLight,
    TimeColor,
    TimerController,
    format_mmss,
)


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    return app


# ==== format_mmss ====

def test_format_under_hour():
    assert format_mmss(65 * 1000) == "01:05"
    assert format_mmss(0) == "00:00"


def test_format_over_hour():
    assert format_mmss(3661 * 1000) == "01:01:01"


def test_format_negative_for_overrun():
    s = format_mmss(-30 * 1000)
    assert s.startswith("-")
    assert "00:30" in s


# ==== TimerController 行為 ====

def test_timer_initial_state(qapp):
    tc = TimerController(target_sec=900)
    assert not tc.is_running()
    assert tc.elapsed_ms == 0
    assert tc.target_ms == 900_000


def test_set_target_changes_state(qapp):
    tc = TimerController(target_sec=600)
    tc.set_target_seconds(1200)
    assert tc.target_ms == 1200_000


def test_set_target_zero_means_no_target(qapp):
    """目標時長 = 0 應視為「無目標」，不觸發超時。"""
    tc = TimerController(target_sec=0)
    state = tc._compute_state()
    assert state.target_ms == 0
    assert state.time_color == TimeColor.GRAY


def test_milestones_armed_correctly(qapp):
    tc = TimerController(target_sec=600, milestones_sec=(300, 60))
    # 假設已過 250 秒（剩 350 秒），剩餘 350 > 300 → milestones_pending 應仍含 300, 60
    tc._elapsed_ms = 250_000
    tc.set_milestones((300, 60))
    assert 300 in tc._milestones_pending
    assert 60 in tc._milestones_pending


def test_milestones_below_remaining_are_dropped(qapp):
    """當前剩餘已小於某里程碑值時，那個里程碑不重新提醒。"""
    tc = TimerController(target_sec=600, milestones_sec=(300, 60))
    tc._elapsed_ms = 580_000  # 剩 20 秒
    tc.set_milestones((300, 60))
    # 剩 20 秒 < 60 → 60 不應在 pending
    assert 60 not in tc._milestones_pending


def test_pace_blue_when_script_ahead_of_time(qapp):
    """講稿進度遠超時間進度 → 太快（藍燈）。"""
    tc = TimerController(target_sec=600)
    tc.set_progress_callback(lambda: 0.5)  # 講稿走 50%
    tc.start()
    tc._elapsed_ms = 100_000  # 時間只走了 16.7%
    state = tc._compute_state()
    assert state.pace == PaceLight.BLUE


def test_pace_yellow_when_time_ahead_of_script(qapp):
    """時間進度超過講稿進度 → 太慢（黃燈）。"""
    tc = TimerController(target_sec=600)
    tc.set_progress_callback(lambda: 0.1)  # 講稿只走 10%
    tc.start()
    tc._elapsed_ms = 400_000  # 時間走了 67%
    state = tc._compute_state()
    assert state.pace == PaceLight.YELLOW


def test_pace_green_when_balanced(qapp):
    tc = TimerController(target_sec=600)
    tc.set_progress_callback(lambda: 0.5)
    tc.start()
    tc._elapsed_ms = 300_000  # 時間 50%
    state = tc._compute_state()
    assert state.pace == PaceLight.GREEN


def test_color_red_after_overrun(qapp):
    tc = TimerController(target_sec=60)
    tc._elapsed_ms = 90_000  # 超時 30 秒
    tc._time_up_emitted = True
    state = tc._compute_state()
    assert state.time_color == TimeColor.RED
    assert state.overrun_ms == 30_000


def test_color_orange_when_under_10_percent(qapp):
    tc = TimerController(target_sec=600)
    tc._elapsed_ms = 555_000  # 剩 7.5%
    state = tc._compute_state()
    assert state.time_color == TimeColor.ORANGE


def test_color_yellow_when_under_25_percent(qapp):
    tc = TimerController(target_sec=600)
    tc._elapsed_ms = 480_000  # 剩 20%
    state = tc._compute_state()
    assert state.time_color == TimeColor.YELLOW


def test_color_green_normally(qapp):
    tc = TimerController(target_sec=600)
    tc._elapsed_ms = 200_000  # 剩 67%
    state = tc._compute_state()
    assert state.time_color == TimeColor.GREEN


def test_progress_callback_exception_does_not_crash(qapp):
    """講稿進度 callback 拋例外時不應中斷狀態計算。"""
    tc = TimerController(target_sec=600)
    tc.set_progress_callback(lambda: 1 / 0)  # 會拋
    tc.start()
    tc._elapsed_ms = 100_000
    # 不應拋
    state = tc._compute_state()
    assert state is not None


def test_reset_clears_milestones_and_elapsed(qapp):
    tc = TimerController(target_sec=300, milestones_sec=(60,))
    tc._elapsed_ms = 280_000
    tc._milestones_pending.clear()  # 模擬已觸發
    tc.reset()
    assert tc._elapsed_ms == 0
    assert 60 in tc._milestones_pending


def test_pause_then_start_continues(qapp):
    """暫停後再啟動應從原本時間繼續，不歸零。"""
    tc = TimerController(target_sec=600)
    tc.start()
    tc._elapsed_ms = 50_000
    tc.pause()
    assert not tc.is_running()
    elapsed_at_pause = tc._elapsed_ms
    tc.start()
    assert tc.is_running()
    # 暫停期間 elapsed 不變
    assert tc._elapsed_ms == elapsed_at_pause


def test_double_start_is_idempotent(qapp):
    tc = TimerController(target_sec=600)
    tc.start()
    tc.start()  # 不應壞掉
    assert tc.is_running()
