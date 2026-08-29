"""QA 模式增強（v1.3.0）：alias 匹配、翻頁指引、no-match 守門、開關與路徑記憶。

對應 plan：wiggly-hatching-bubble.md
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytest.importorskip("fitz")

from teleprompter.core.qa_library import (  # noqa: E402
    QAItem,
    QALibrary,
    load_qa,
    parse_qa_from_text,
    parse_slide_page,
)


# ============================================================
# 資料層：alias / K 行 / 翻頁指引 / JSON 相容
# ============================================================

def test_q_line_splits_into_aliases():
    lib = parse_qa_from_text(
        "Q: 為什麼選這五個維度 / why these five dimensions / dimension選擇\n"
        "A: 因為它們涵蓋三個層次。\n"
    )
    assert len(lib) == 1
    item = lib.items[0]
    assert item.question == "為什麼選這五個維度"
    assert len(item.aliases) == 2
    # 用任一 alias 提問都要命中
    for query in ("why these five dimensions", "dimension選擇"):
        m = lib.match(query)
        assert m is not None and m.score >= 80, query


def test_k_line_adds_keywords():
    lib = parse_qa_from_text(
        "Q: 資料集多大\n"
        "K: dataset size, 幾筆資料, corpus\n"
        "A: 共 42,632 筆。\n"
    )
    item = lib.items[0]
    assert "dataset size" in item.aliases
    assert lib.match("what is the dataset size").score >= 80


def test_backup_marker_needs_start_page():
    text = "Q: 統計顯著嗎\nA: 【翻到備答 B08 統計 頁】 是的，McNemar 檢定顯著。\n"
    # 未提供 backup_start_page → 不亂跳頁
    assert parse_qa_from_text(text).items[0].slide_page is None
    # B01 = 第 24 頁 → B08 = 31
    assert parse_qa_from_text(text, backup_start_page=24).items[0].slide_page == 31


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("【投影片 12 頁】內容", 12),
        ("【第 7 頁】", 7),
        ("[page 5] text", 5),
        ("沒有標記，只提到 K=4 與 4 個案例", None),
    ],
)
def test_explicit_page_markers(answer, expected):
    assert parse_slide_page(answer) == expected


def test_json_supports_new_fields_and_stays_backward_compatible(tmp_path):
    p = tmp_path / "qa.json"
    p.write_text(
        json.dumps([
            {"q": "新格式", "a": "帶欄位", "aliases": ["new format"], "slide_page": 9},
            {"q": "舊格式", "a": "只有問答"},
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    lib = load_qa(p)
    assert len(lib) == 2
    assert lib.items[0].slide_page == 9
    assert lib.items[0].aliases == ["new format"]
    # 舊格式仍可載入且不帶頁碼
    assert lib.items[1].slide_page is None
    assert lib.match("new format").score >= 80


def test_alias_scoring_does_not_break_plain_items():
    """沒有 alias 的題目，行為必須與舊版一致。"""
    lib = QALibrary([QAItem("什麼是 transformer", "一種架構")])
    m = lib.match("什麼是 transformer")
    assert m is not None and m.score >= 90


# ============================================================
# UI 層：翻頁訊號 / no-match / 開關 / 路徑記憶
# ============================================================

@pytest.fixture(scope="module")
def app():
    import sys

    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)


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
def qa_file(tmp_path):
    p = tmp_path / "qa.md"
    p.write_text(
        "Q: 為什麼選這五個維度 / why these five dimensions\n"
        "A: 【翻到備答 B02 五維度 頁】 五個維度涵蓋三個層次。\n\n"
        "Q: 統計顯著嗎 / statistically significant\n"
        "A: 【翻到備答 B04 統計 頁】 McNemar 檢定顯著。\n",
        encoding="utf-8",
    )
    return p


def _make_mw(app, cfg=None):
    from teleprompter.config import load_config
    from teleprompter.ui.main_window import MainWindow
    w = MainWindow(cfg if cfg is not None else load_config())
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    time.sleep(0.05)
    app.processEvents()
    return w


def test_panel_emits_goto_page_on_confident_match(app, sessions_dir, qa_file):
    """命中帶頁碼的題目 → 發出 goto_page，且同題不重複發。"""
    from teleprompter.ui.qa_panel import QAPanel

    panel = QAPanel()
    panel.set_backup_start_page(24)   # B01 = 24 → B02 = 25
    panel.load_qa_file(str(qa_file))

    seen: list[int] = []
    panel.goto_page.connect(seen.append)

    panel.append_recognized("why these five dimensions")
    app.processEvents()
    assert seen == [25]

    # 同一題再餵一次 → 不重複翻頁
    panel.append_recognized("why these five dimensions again")
    app.processEvents()
    assert seen == [25]

    # 換一題 → 翻到新頁（B04 = 27）
    panel.clear_question()
    panel.append_recognized("is it statistically significant")
    app.processEvents()
    assert seen == [25, 27]
    panel.deleteLater()


def test_low_confidence_does_not_fill_answer(app, sessions_dir, qa_file):
    """完全不相關的提問 → 不硬塞答案，改顯示未匹配提示。"""
    from teleprompter.ui.qa_panel import QAPanel

    panel = QAPanel()
    panel.load_qa_file(str(qa_file))
    # 面板預設英文；本例用中文提問，先切語言避免被語言不符過濾擋掉
    panel.lang_combo.setCurrentIndex(1)
    panel.append_recognized("今天午餐吃什麼比較好呢")
    app.processEvents()

    assert panel.answer_text.toPlainText().strip() == ""
    assert "未匹配" in panel.match_info.text()
    panel.deleteLater()


def test_karaoke_switch_toggles_and_emits(app, sessions_dir):
    from teleprompter.ui.qa_panel import QAPanel, ToggleSwitch

    panel = QAPanel()
    assert isinstance(panel.karaoke_switch, ToggleSwitch)
    states: list[bool] = []
    panel.karaoke_toggled.connect(states.append)

    panel.karaoke_switch.setChecked(True)
    app.processEvents()
    panel.karaoke_switch.setChecked(False)
    app.processEvents()
    assert states == [True, False]
    panel.deleteLater()


def test_qa_path_and_karaoke_persist_across_restart(app, sessions_dir, qa_file, tmp_path, monkeypatch):
    """載入 QA 庫 + 開啟卡拉 OK → 設定寫回；重開後自動還原。"""
    from teleprompter.config import AppConfig

    saved: dict = {}
    monkeypatch.setattr(
        "teleprompter.ui.main_window.save_config",
        lambda cfg: saved.update(
            last_qa_path=cfg.last_qa_path, qa_karaoke_enabled=cfg.qa_karaoke_enabled
        ),
    )

    w = _make_mw(app)
    w.qa_panel.load_qa_file(str(qa_file))
    w.qa_panel.karaoke_switch.setChecked(True)
    app.processEvents()
    assert saved.get("last_qa_path") == str(qa_file)
    assert saved.get("qa_karaoke_enabled") is True
    w.close()
    app.processEvents()

    # 帶著設定重開 → QA 庫自動載入、開關還原
    cfg2 = AppConfig(last_qa_path=str(qa_file), qa_karaoke_enabled=True)
    w2 = _make_mw(app, cfg2)
    assert len(w2.qa_panel.library) == 2
    assert w2.qa_panel.karaoke_switch.isChecked() is True
    w2.close()
    app.processEvents()


def test_goto_page_survives_without_transcript(app, sessions_dir, qa_file, tmp_path):
    """QA 常只開投影片沒講稿 → 翻頁不得崩潰。"""
    import fitz

    doc = fitz.open()
    for i in range(30):
        page = doc.new_page(width=960, height=540)
        page.insert_text((50, 72), f"Slide {i + 1}", fontsize=24)
    pdf = tmp_path / "deck.pdf"
    doc.save(str(pdf))
    doc.close()

    w = _make_mw(app)
    w.load_slides(str(pdf)) if hasattr(w, "load_slides") else None
    w.qa_panel.set_backup_start_page(24)
    w.qa_panel.load_qa_file(str(qa_file))
    # 直接觸發翻頁（不論有無 deck 都不該拋例外）
    w._on_qa_goto_page(25)
    app.processEvents()
    w.close()
    app.processEvents()
