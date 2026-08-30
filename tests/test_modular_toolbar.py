"""模組化工具列（v1.5）：常駐列瘦身、四個模組開關、圖示系統。

對應設計稿：design/toolbar_mockup_v2.html 方案一。
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from teleprompter.ui.icons import available_icons, icon  # noqa: E402


# ============================================================
# 圖示系統
# ============================================================

@pytest.fixture(scope="module")
def app():
    import sys

    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)


def test_every_icon_renders(app):
    """每顆圖示都要渲染得出來——空圖示會讓按鈕看起來壞掉。"""
    names = available_icons()
    assert len(names) >= 30
    broken = [n for n in names if icon(n).isNull()]
    assert broken == [], f"這些圖示渲染失敗：{broken}"


def test_unknown_icon_is_safe(app):
    """名稱打錯只回空圖示，不得拋例外（介面建構中途炸掉最難查）。"""
    assert icon("no-such-icon").isNull()


def test_icon_accepts_color(app):
    ic = icon("play", color="#ff0000")
    assert not ic.isNull()


# ============================================================
# 工具列結構
# ============================================================

@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setattr(
        "teleprompter.ui.main_window.default_sessions_path",
        lambda: sd / "sessions.json",
    )
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **kw: QMessageBox.StandardButton.Discard,
    )
    return sd


@pytest.fixture
def win(app, sessions_dir):
    from teleprompter.config import load_config
    from teleprompter.ui.main_window import MainWindow
    w = MainWindow(load_config())
    w.resize(1500, 800)
    w.show()
    app.processEvents()
    time.sleep(0.05)
    app.processEvents()
    yield w
    w.close()
    app.processEvents()


MODULES = ("edit", "annot", "qa", "follow")


def test_all_module_bars_hidden_by_default(win):
    """預設是演講狀態：四條模組列全收起，畫面只有常駐列。"""
    assert set(win.module_bars) == set(MODULES)
    for key in MODULES:
        assert not win.module_bars[key].isVisible(), f"{key} 模組列不該預設展開"
        assert not win.module_toggles[key].isChecked()
    # 舊的標註工具列也不該再常駐
    assert not win.annotation_toolbar.isVisible()


@pytest.fixture
def no_audio(win, monkeypatch):
    """把辨識器／音訊換成 no-op：QA 模組會真的去載模型與開麥克風。"""
    monkeypatch.setattr(win.recognizer, "start", lambda *a, **k: None)
    monkeypatch.setattr(win.recognizer, "stop", lambda *a, **k: None)
    monkeypatch.setattr(win.recognizer, "is_running", lambda: True)
    monkeypatch.setattr(win.audio, "start", lambda *a, **k: None)
    monkeypatch.setattr(win.audio, "stop", lambda *a, **k: None)
    monkeypatch.setattr(win.audio, "is_running", lambda: False)
    return win


@pytest.mark.parametrize("key", MODULES)
def test_toggle_shows_and_hides_module_bar(no_audio, win, app, key):
    win.module_toggles[key].setChecked(True)
    app.processEvents()
    assert win.module_bars[key].isVisible(), f"{key} 開關打開後模組列要出現"

    win.module_toggles[key].setChecked(False)
    app.processEvents()
    assert not win.module_bars[key].isVisible(), f"{key} 關閉後模組列要收起"


def test_edit_module_drives_edit_mode(win, app):
    """編輯模組 = 編輯模式，兩者不得各自為政。"""
    win.module_toggles["edit"].setChecked(True)
    app.processEvents()
    assert win.act_edit_mode.isChecked()

    win.module_toggles["edit"].setChecked(False)
    app.processEvents()
    assert not win.act_edit_mode.isChecked()


def test_closing_annotation_module_returns_to_pointer(win, app):
    """收起標註模組時要回到游標，否則畫布還停在鉛筆上，點擊行為會怪。"""
    win.module_toggles["annot"].setChecked(True)
    app.processEvents()
    win.act_tool_pencil.setChecked(True)
    win._set_annotation_tool("pencil")
    app.processEvents()

    win.module_toggles["annot"].setChecked(False)
    app.processEvents()
    assert win.act_tool_pointer.isChecked()


def test_module_bars_carry_their_tools(win):
    """每個模組列都要真的裝著對應的功能（不是空殼）。"""
    expected = {
        "edit": ["act_bold", "act_italic", "act_underline", "act_highlight",
                 "act_clear_fmt", "act_insert_annotation", "act_compact_ws",
                 "act_font_smaller", "act_font_bigger", "act_save"],
        "annot": ["act_tool_pointer", "act_tool_pencil", "act_tool_note",
                  "act_tool_eraser", "act_clear_page"],
        "qa": ["act_qa_mode"],
        "follow": ["act_reset_pos", "act_clear_skipped", "act_reset_timer", "act_record"],
    }
    for key, attrs in expected.items():
        acts = set(win.module_bars[key].actions())
        for attr in attrs:
            assert getattr(win, attr) in acts, f"{attr} 應該在 {key} 模組列裡"


def test_main_toolbar_keeps_only_presenting_essentials(win):
    """常駐列只留演講當下要按的；已模組化的項目不得再佔位。"""
    acts = [a for a in win._main_toolbar.actions() if not a.isSeparator()]
    for attr in ("act_start", "act_goto_speech", "act_fullscreen", "act_settings"):
        assert getattr(win, attr) in acts, f"{attr} 應留在常駐列"
    # 載入類與模組類都不該佔用常駐列（v1.7 起載入只在檔案選單／引導頁／拖放）
    for attr in ("act_open", "act_open_slides", "act_record", "act_reset_timer",
                 "act_bold", "act_save", "act_font_bigger", "act_clear_skipped"):
        assert getattr(win, attr) not in acts, f"{attr} 不該在常駐列"


def test_color_swatches_live_in_annotation_module(win):
    """色票是 widget，只能屬於一條工具列——必須在標註模組列裡才點得到。"""
    bar_widgets = [
        win.module_bars["annot"].widgetForAction(a)
        for a in win.module_bars["annot"].actions()
    ]
    for btn in win._color_preset_btns:
        assert btn in bar_widgets, "色票按鈕不在標註模組列上"
    assert win.btn_color_custom in bar_widgets


def test_toolbar_buttons_show_icon_and_text(win):
    """Qt 預設 IconOnly 會把標籤吃掉——常駐列必須是圖示＋文字。"""
    from PySide6.QtCore import Qt
    assert win._main_toolbar.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon
    for key in MODULES:
        assert win.module_bars[key].toolButtonStyle() == \
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon


def test_no_emoji_left_in_toolbar_labels(win):
    """企業級介面規格：工具列標籤不得殘留 emoji。"""
    import re
    emoji = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿]")
    bars = [win._main_toolbar] + [win.module_bars[k] for k in MODULES]
    offenders = []
    for bar in bars:
        for a in bar.actions():
            if a.text() and emoji.search(a.text()):
                offenders.append(a.text())
    assert offenders == [], f"這些標籤還有 emoji：{offenders}"


def test_sync_module_toggle_does_not_recurse(win, app):
    """外部改模式時同步開關，且不得反向再觸發一次（避免無窮迴圈）。"""
    win.sync_module_toggle("edit", True)
    app.processEvents()
    assert win.module_toggles["edit"].isChecked()
    assert win.module_bars["edit"].isVisible()
    # act_edit_mode 不該被 sync 連帶改動（sync 只負責外觀同步）
    win.sync_module_toggle("edit", False)
    app.processEvents()
    assert not win.module_toggles["edit"].isChecked()


def test_qa_module_actually_opens_the_panel(no_audio, win, app):
    """QA 模組不只是顯示工具列——右側 Q&A 面板要真的展開／收起。

    _enter_qa_mode 會嘗試啟動辨識器與音訊；測試環境沒有裝置，
    因此把這兩段換成 no-op，只驗證面板顯隱這件事。
    """
    win.module_toggles["qa"].setChecked(True)
    app.processEvents()
    assert win.qa_panel.isVisible(), "開啟 QA 模組時面板應展開"
    assert win.act_qa_mode.isChecked()

    win.module_toggles["qa"].setChecked(False)
    app.processEvents()
    assert not win.qa_panel.isVisible(), "關閉 QA 模組時面板應收起"

    win.module_toggles["qa"].setChecked(True)
    app.processEvents()
    assert win.qa_panel.isVisible(), "重複開關後仍要能展開"
