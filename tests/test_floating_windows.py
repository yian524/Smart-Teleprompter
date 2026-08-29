"""可獨立浮動的視窗：懸浮計時、Q&A 面板分離。

用途：報告時切到投影片或瀏覽器，時間仍看得到；Q&A 面板可移到第二螢幕，
講稿區不必讓出寬度。
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from teleprompter.core.timer_controller import TimerState, TimeColor  # noqa: E402


@pytest.fixture(scope="module")
def app():
    import sys

    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    from teleprompter.config import AppConfig
    from teleprompter.ui import main_window as mw
    from PySide6.QtWidgets import QMessageBox

    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setattr(mw, "default_sessions_path", lambda: sd / "sessions.json")
    monkeypatch.setattr(mw, "save_config", lambda cfg: None)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **kw: QMessageBox.StandardButton.Discard,
    )
    w = mw.MainWindow(AppConfig())
    w.resize(1400, 800)
    w.show()
    app.processEvents()
    time.sleep(0.05)
    app.processEvents()
    yield w
    w.close()
    app.processEvents()


# ============================================================
# 懸浮計時
# ============================================================

def test_floating_timer_created_lazily(win):
    """沒開啟就不該建立視窗（省資源，也避免無意間置頂遮住東西）。"""
    assert win.floating_timer is None


def test_floating_timer_shows_and_hides(win, app):
    win.act_floating_timer.setChecked(True)
    app.processEvents()
    assert win.floating_timer is not None
    assert win.floating_timer.isVisible()

    win.act_floating_timer.setChecked(False)
    app.processEvents()
    assert not win.floating_timer.isVisible()


def test_floating_timer_stays_on_top_and_frameless(win, app):
    """置頂 + 無邊框，才能疊在簡報軟體上面。"""
    from PySide6.QtCore import Qt

    win.act_floating_timer.setChecked(True)
    app.processEvents()
    flags = win.floating_timer.windowFlags()
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.FramelessWindowHint


def test_floating_timer_tracks_remaining_time(win, app):
    win.act_floating_timer.setChecked(True)
    app.processEvents()

    win.timer_ctrl.state_changed.emit(TimerState(
        elapsed_ms=65_000, target_ms=900_000,
        remaining_ms=835_000, time_color=TimeColor.GREEN,
    ))
    app.processEvents()
    assert win.floating_timer.main_label.text() == "13:55"

    win.timer_ctrl.state_changed.emit(TimerState(
        elapsed_ms=960_000, target_ms=900_000, remaining_ms=0,
        overrun_ms=60_000, time_color=TimeColor.RED,
    ))
    app.processEvents()
    assert win.floating_timer.main_label.text() == "+01:00"
    assert "超時" in win.floating_timer.sub_label.text()


def test_floating_timer_without_target_shows_elapsed(win, app):
    """沒設目標時長時顯示已用時間，而不是空白或 --:--。"""
    win.act_floating_timer.setChecked(True)
    app.processEvents()
    win.timer_ctrl.state_changed.emit(TimerState(elapsed_ms=125_000, target_ms=0))
    app.processEvents()
    assert win.floating_timer.main_label.text() == "02:05"


def test_floating_timer_closes_with_main_window(win, app):
    """主視窗關掉後不能留下孤兒視窗黏在畫面上。"""
    win.act_floating_timer.setChecked(True)
    app.processEvents()
    ft = win.floating_timer
    win.close()
    app.processEvents()
    assert not ft.isVisible()


# ============================================================
# Q&A 面板分離
# ============================================================

def test_qa_panel_starts_docked(win):
    assert not win.qa_panel.isWindow()


def test_qa_panel_detaches_and_redocks(win, app):
    win.act_qa_floating.setChecked(True)
    app.processEvents()
    assert win.qa_panel.isWindow(), "應成為獨立視窗"
    assert win.qa_panel.windowTitle() == "Q&A 助手"

    win.act_qa_floating.setChecked(False)
    app.processEvents()
    assert not win.qa_panel.isWindow(), "應收回主視窗"
    assert win.qa_panel.parent() is not None


def test_qa_panel_keeps_visibility_across_detach(win, app):
    """切換停靠方式不該讓正在使用的面板消失。"""
    win.qa_panel.show()
    app.processEvents()

    win.act_qa_floating.setChecked(True)
    app.processEvents()
    assert win.qa_panel.isVisible()

    win.act_qa_floating.setChecked(False)
    app.processEvents()
    assert win.qa_panel.isVisible()


def test_hidden_qa_panel_stays_hidden_when_detached(win, app):
    """沒在用 Q&A 時分離，不該平白冒出一個視窗。"""
    assert not win.qa_panel.isVisible()
    win.act_qa_floating.setChecked(True)
    app.processEvents()
    assert not win.qa_panel.isVisible()


# ============================================================
# 懸浮計時：縮放與可讀性
# ============================================================

def test_floating_timer_has_resize_grip(win, app):
    """右下角要有可拖曳的縮放握把。"""
    from PySide6.QtWidgets import QSizeGrip

    win.act_floating_timer.setChecked(True)
    app.processEvents()
    assert isinstance(win.floating_timer.size_grip, QSizeGrip)


def test_font_scales_with_window_size(win, app):
    """拉大視窗，數字要跟著變大——這是台上看得清楚的關鍵。"""
    from PySide6.QtCore import QSize

    win.act_floating_timer.setChecked(True)
    app.processEvents()
    ft = win.floating_timer

    ft.resize(QSize(200, 110))
    app.processEvents()
    small = ft._font_px()

    ft.resize(QSize(620, 300))
    app.processEvents()
    large = ft._font_px()

    assert large > small * 2, f"放大後字級應顯著變大（{small} → {large}）"
    assert small >= 22, "最小尺寸下仍要有基本可讀字級"


def test_detail_line_hides_when_too_small(win, app):
    """空間不夠時，說明字讓位給時間數字。"""
    from PySide6.QtCore import QSize

    win.act_floating_timer.setChecked(True)
    app.processEvents()
    ft = win.floating_timer

    ft.resize(QSize(190, 100))
    app.processEvents()
    assert not ft.sub_label.isVisible()

    ft.resize(QSize(340, 168))
    app.processEvents()
    assert ft.sub_label.isVisible()


def test_size_is_clamped(win, app):
    """縮放有上下限，避免拖到小得看不見或大到蓋住整個螢幕。"""
    from PySide6.QtCore import QSize
    from teleprompter.ui.floating_timer import MAX_SIZE, MIN_SIZE

    win.act_floating_timer.setChecked(True)
    app.processEvents()
    ft = win.floating_timer

    ft.resize(QSize(50, 30))
    app.processEvents()
    assert ft.width() >= MIN_SIZE.width() and ft.height() >= MIN_SIZE.height()

    ft.resize(QSize(4000, 3000))
    app.processEvents()
    assert ft.width() <= MAX_SIZE.width() and ft.height() <= MAX_SIZE.height()


def test_geometry_is_reported_for_persistence(win, app):
    """縮放/移動後要回報幾何，設定才存得住。"""
    from PySide6.QtCore import QSize

    win.act_floating_timer.setChecked(True)
    app.processEvents()
    ft = win.floating_timer

    seen = []
    ft.geometry_changed.connect(lambda x, y, w, h: seen.append((w, h)))
    ft.resize(QSize(500, 240))
    app.processEvents()
    ft._emit_geometry()
    assert seen and seen[-1] == (ft.width(), ft.height())


def test_double_click_toggles_big_mode(win, app):
    """雙擊在預設大小與大字模式間切換（台上要放大時最快的操作）。"""
    from PySide6.QtCore import QSize
    from teleprompter.ui.floating_timer import DEFAULT_SIZE

    win.act_floating_timer.setChecked(True)
    app.processEvents()
    ft = win.floating_timer
    ft.resize(DEFAULT_SIZE)
    app.processEvents()

    ft.mouseDoubleClickEvent(None)
    app.processEvents()
    assert ft.width() > DEFAULT_SIZE.width(), "第一次雙擊應放大"

    ft.mouseDoubleClickEvent(None)
    app.processEvents()
    assert ft.width() == DEFAULT_SIZE.width(), "再雙擊應回到預設"
