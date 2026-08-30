"""可停靠面板：檢視選單勾選、拖曳停靠、佈局記憶。"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402


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


PANELS = ("slides", "qa", "time")


def test_all_panels_are_docks(win):
    from PySide6.QtWidgets import QDockWidget

    assert set(win.panel_docks) == set(PANELS)
    for key, dock in win.panel_docks.items():
        assert isinstance(dock, QDockWidget), f"{key} 應為 QDockWidget"


def test_docks_have_object_names(win):
    """沒有 objectName 就無法被 saveState 記住，Qt 也會在 log 抱怨。"""
    for key, dock in win.panel_docks.items():
        assert dock.objectName(), f"{key} 缺少 objectName"


def test_view_menu_exposes_toggle_actions(win):
    """檢視選單要能勾選各面板——這是使用者選的控制方式。"""
    wanted = {dock.toggleViewAction() for dock in win.panel_docks.values()}
    found = set()
    for top in win.menuBar().actions():
        menu = top.menu()
        if menu is None:
            continue
        found.update(menu.actions())
    missing = [a.text() for a in wanted - found]
    assert not missing, f"這些面板未出現在選單：{missing}"


@pytest.mark.parametrize("key", PANELS)
def test_toggle_action_shows_and_hides(win, app, key):
    dock = win.panel_docks[key]
    action = dock.toggleViewAction()

    # toggleViewAction 要用 trigger 才會作用（setChecked 只改勾選狀態）
    if not dock.isVisible():
        action.trigger()
        app.processEvents()
    assert dock.isVisible()

    action.trigger()
    app.processEvents()
    assert not dock.isVisible()


def test_docks_can_float(win, app):
    """拖出來變獨立視窗（可放到第二螢幕）。"""
    dock = win.dock_qa
    dock.show()
    dock.setFloating(True)
    app.processEvents()
    assert dock.isFloating()

    dock.setFloating(False)
    app.processEvents()
    assert not dock.isFloating()
    assert win.dockWidgetArea(dock) != Qt.DockWidgetArea.NoDockWidgetArea


def test_layout_can_be_saved_and_restored(win, app):
    """調整版面 → 存 → 改動 → 還原，要回到存檔時的樣子。"""
    win.dock_qa.show()
    win.dock_slides.show()
    app.processEvents()
    saved = win.saveState()

    win.dock_qa.hide()
    win.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, win.dock_slides)
    app.processEvents()
    assert not win.dock_qa.isVisible()

    win.restoreState(saved)
    app.processEvents()
    assert win.dock_qa.isVisible(), "還原後 Q&A 應該回來"
    assert win.dockWidgetArea(win.dock_slides) == Qt.DockWidgetArea.LeftDockWidgetArea


def test_dock_state_is_persisted_on_close(win, app, monkeypatch):
    """關閉時要把佈局寫進設定，下次啟動才還原得回來。"""
    from teleprompter.ui import main_window as mw

    saved = {}
    monkeypatch.setattr(
        mw, "save_config",
        lambda cfg: saved.update(dock_state=cfg.dock_state),
    )
    win.dock_qa.show()
    app.processEvents()
    win.close()
    app.processEvents()

    assert saved.get("dock_state"), "closeEvent 應保存 dock 佈局"


def test_thumbnail_collapse_hides_dock(win, app):
    """縮圖列的收合鈕改成關掉 dock（舊的浮動展開鈕已移除）。"""
    win.dock_slides.show()
    app.processEvents()

    win._on_thumbnail_collapse(True)
    app.processEvents()
    assert not win.dock_slides.isVisible()

    win._on_thumbnail_collapse(False)
    app.processEvents()
    assert win.dock_slides.isVisible()


def test_time_panel_shortcut_toggles_dock(win, app):
    """T 鍵切換時間列。"""
    before = win.dock_time.isVisible()
    win._toggle_time_panel()
    app.processEvents()
    assert win.dock_time.isVisible() != before


def test_main_content_stays_central(win):
    """主內容區仍是 central widget——三模式切換不受 dock 影響。"""
    assert win.centralWidget() is not None
    assert win._content_stack.parent() is not None
    for mode in ("transcript", "split", "slide"):
        win._set_view_mode(mode)
        assert win._view_mode == mode
