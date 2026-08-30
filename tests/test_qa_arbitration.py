"""Q&A 與跟讀共用辨識資源時的仲裁行為。

背景：辨識器與麥克風是單一資源，兩種模式要的設定互斥（聽自己/聽觀眾、
中文/英文）。先前兩邊各自 start/stop，造成「Q&A 開著按開始 → 麥克風沒開、
畫面不動、UI 卻說辨識中」這種無聲故障。
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from teleprompter.core.recognition_service import SOURCE_LOOPBACK, SOURCE_MIC  # noqa: E402


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
def fake_backend(win, monkeypatch):
    """把辨識器與音訊換成可觀察的假物件（真的載模型會拖垮測試）。"""
    state = {"rec_running": False, "audio_running": False,
             "rec_starts": [], "audio_starts": [], "rec_stops": 0, "audio_stops": 0}

    def rec_start(**kw):
        if state["rec_running"]:
            return                      # 模擬底層「已在跑就靜默略過」
        state["rec_running"] = True
        state["rec_starts"].append(kw)

    def rec_stop():
        if state["rec_running"]:
            state["rec_running"] = False
            state["rec_stops"] += 1

    def audio_start(device=None, *, loopback=False):
        if state["audio_running"]:
            return
        state["audio_running"] = True
        state["audio_starts"].append({"device": device, "loopback": loopback})

    def audio_stop():
        if state["audio_running"]:
            state["audio_running"] = False
            state["audio_stops"] += 1

    monkeypatch.setattr(win.recognizer, "start", rec_start)
    monkeypatch.setattr(win.recognizer, "stop", rec_stop)
    monkeypatch.setattr(win.recognizer, "is_running", lambda: state["rec_running"])
    monkeypatch.setattr(win.audio, "start", audio_start)
    monkeypatch.setattr(win.audio, "stop", audio_stop)
    monkeypatch.setattr(win.audio, "is_running", lambda: state["audio_running"])
    return state


@pytest.fixture
def with_script(win, tmp_path, app):
    f = tmp_path / "talk.txt"
    f.write_text("第一句話。第二句話。", encoding="utf-8")
    win.load_file(str(f))
    app.processEvents()
    return win


# ============================================================
# 核心：先前的無聲故障
# ============================================================

def test_start_while_qa_open_does_not_pretend_to_listen(with_script, app, fake_backend):
    """Q&A 開著按「開始」——不得出現「說在辨識、其實麥克風沒開」。

    Q&A 仍持有資源（觀眾在等回答），所以音訊維持系統音訊；跟讀被記為等待，
    使用者狀態一致，而不是靜默失敗。
    """
    win = with_script
    win._enter_qa_mode()
    app.processEvents()
    assert win.recognition.is_active("qa")
    assert win.recognition.applied_need.source == SOURCE_LOOPBACK

    win._start()
    app.processEvents()

    # 資源仍在 Q&A 手上，而且系統知道跟讀在等
    assert win.recognition.is_active("qa"), "Q&A 進行中不該被念稿悄悄接管"
    assert win.recognition.is_waiting("follow"), "跟讀應被記為等待中"
    assert win.recognition.applied_need.source == SOURCE_LOOPBACK


def test_follow_resumes_when_qa_closes(with_script, app, fake_backend):
    """關掉 Q&A → 跟讀的設定（麥克風 + 講稿語言 + 講稿偏向）自動回復。"""
    win = with_script
    win._start()                 # 先開始念稿
    app.processEvents()
    assert win.recognition.is_active("follow")
    assert win.recognition.applied_need.source == SOURCE_MIC

    win._enter_qa_mode()
    app.processEvents()
    assert win.recognition.is_active("qa")

    win._exit_qa_mode()
    app.processEvents()
    assert win.recognition.is_active("follow"), "Q&A 結束後跟讀要自動接回"
    need = win.recognition.applied_need
    assert need.source == SOURCE_MIC
    assert need.language == win.cfg.language
    assert need.initial_prompt, "跟讀要帶講稿偏向"


def test_pause_does_not_kill_qa_capture(with_script, app, fake_backend):
    """念稿暫停時，Q&A 的收音不能跟著斷——它是獨立的登記者。"""
    win = with_script
    win._start()
    app.processEvents()
    win._enter_qa_mode()
    app.processEvents()

    win._pause()
    app.processEvents()

    assert win.recognition.is_active("qa"), "暫停念稿不該停掉 Q&A 的辨識"
    assert "follow" not in win.recognition.owners()


def test_manual_mode_does_not_register_recognition(with_script, app, fake_backend):
    """手動模式只計時；就算之後開 Q&A，也只有 Q&A 一個登記者。"""
    win = with_script
    win.module_toggles["follow"].setChecked(False)
    app.processEvents()

    win._start()
    app.processEvents()
    assert win.recognition.owners() == [], "手動模式不該動用辨識資源"
    assert win.timer_ctrl.is_running()

    win._enter_qa_mode()
    app.processEvents()
    assert win.recognition.owners() == ["qa"]
    assert win.timer_ctrl.is_running(), "開 Q&A 不該影響計時"


def test_qa_language_change_updates_need(with_script, app, fake_backend):
    """在 Q&A 面板換辨識語言 → 更新登記需求（不是繞過仲裁自己重啟）。"""
    win = with_script
    win._enter_qa_mode()
    app.processEvents()

    win.qa_panel.lang_combo.setCurrentIndex(1)   # 切成中文
    app.processEvents()

    assert win.recognition.applied_need.language == win.qa_panel.get_language()


def test_transcript_routing_follows_resource_owner(with_script, app, fake_backend):
    """辨識文字該給誰，依「誰持有資源」判斷，而不是面板是否可見。"""
    win = with_script
    win._start()
    app.processEvents()
    before = win.qa_panel.question_text.toPlainText()

    win._on_text_committed("這是念稿的內容")
    app.processEvents()
    assert win.qa_panel.question_text.toPlainText() == before, "念稿時不該送進 Q&A 面板"

    win._enter_qa_mode()
    app.processEvents()
    win._on_text_committed("this is an audience question")
    app.processEvents()
    assert win.qa_panel.question_text.toPlainText() != before, "Q&A 持有資源時要送進面板"
