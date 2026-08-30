"""手動模式：關掉跟讀時，「開始」只跑計時。

這是為了「辨識不夠準、寧可自己按上下鍵」的使用者——不該被迫等 10-30 秒
載入模型，也不該被佔用麥克風。
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


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
    w.resize(1200, 700)
    w.show()
    app.processEvents()
    time.sleep(0.05)
    app.processEvents()
    yield w
    w.close()
    app.processEvents()


@pytest.fixture
def counted(win, monkeypatch):
    """把辨識器與音訊換成計數器，確認手動模式真的沒碰它們。"""
    calls = {"rec_start": 0, "audio_start": 0, "audio_stop": 0}
    monkeypatch.setattr(
        win.recognizer, "start",
        lambda *a, **kw: calls.__setitem__("rec_start", calls["rec_start"] + 1),
    )
    monkeypatch.setattr(win.recognizer, "is_running", lambda: False)
    monkeypatch.setattr(
        win.audio, "start",
        lambda *a, **kw: calls.__setitem__("audio_start", calls["audio_start"] + 1),
    )
    monkeypatch.setattr(
        win.audio, "stop",
        lambda *a, **kw: calls.__setitem__("audio_stop", calls["audio_stop"] + 1),
    )
    monkeypatch.setattr(win.audio, "is_running", lambda: False)
    return calls


@pytest.fixture
def with_script(win, tmp_path, app):
    f = tmp_path / "talk.txt"
    f.write_text("第一句話。第二句話。第三句話。", encoding="utf-8")
    win.load_file(str(f))
    app.processEvents()
    return win


def test_follow_off_starts_timer_only(with_script, app, counted):
    """核心承諾：關掉跟讀後按開始，立刻計時、完全不碰辨識資源。"""
    win = with_script
    win.module_toggles["follow"].setChecked(False)
    app.processEvents()

    win._start()
    app.processEvents()

    assert win.timer_ctrl.is_running(), "計時應該立刻開始"
    assert counted["rec_start"] == 0, "不該載入辨識模型"
    assert counted["audio_start"] == 0, "不該開啟麥克風"
    assert win.act_start.text() == "暫停"


def test_follow_off_pause_does_not_touch_audio(with_script, app, counted):
    """手動模式暫停只停計時——先前會連 Q&A 的收音一起關掉。"""
    win = with_script
    win.module_toggles["follow"].setChecked(False)
    app.processEvents()
    win._start()
    app.processEvents()

    win._toggle_run()          # 暫停
    app.processEvents()

    assert not win.timer_ctrl.is_running()
    assert counted["audio_stop"] == 0, "手動模式不該去停音訊"
    assert win.act_start.text() == "繼續"


def test_follow_on_uses_recognition(with_script, app, counted):
    """跟讀開著時維持原本行為：會去啟動辨識。"""
    win = with_script
    win.module_toggles["follow"].setChecked(True)
    app.processEvents()

    win._start()
    app.processEvents()

    assert counted["rec_start"] == 1, "跟讀模式應載入辨識模型"


def test_toggle_reflects_in_config_and_tooltip(win, app):
    win.module_toggles["follow"].setChecked(False)
    app.processEvents()
    assert win.cfg.follow_mode_enabled is False
    assert "不啟動語音辨識" in win.act_start.toolTip()

    win.module_toggles["follow"].setChecked(True)
    app.processEvents()
    assert win.cfg.follow_mode_enabled is True
    assert "語音跟讀" in win.act_start.toolTip()


def test_follow_enabled_falls_back_to_config(win):
    """開關還沒建好時（例如測試直接呼叫）以設定為準，不要炸掉。"""
    toggles = win.module_toggles
    try:
        win.module_toggles = {}
        assert win.follow_enabled() == win.cfg.follow_mode_enabled
    finally:
        win.module_toggles = toggles


def test_manual_running_flag_clears_on_pause(with_script, app, counted):
    win = with_script
    win.module_toggles["follow"].setChecked(False)
    app.processEvents()
    win._start()
    assert win._manual_running is True
    win._toggle_run()
    assert win._manual_running is False
