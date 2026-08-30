"""譯文要一起參與題目匹配。

觀眾說英文、備答題庫是中文時，用英文原文比對中文題目幾乎不可能命中
（只剩拼音與字面模糊比對）。翻譯完成後把譯文也丟進去比，才找得到備答。
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
def panel(app, tmp_path):
    """載入一份純中文題庫的 Q&A 面板。"""
    from teleprompter.ui.qa_panel import QAPanel

    bank = tmp_path / "qa.md"
    bank.write_text(
        "Q: 你的資料集有沒有標籤洩漏的問題\n"
        "A: 【投影片 3 頁】 沒有，訓練與測試在時間上完全切開。\n\n"
        "Q: 這些結果在統計上顯著嗎\n"
        "A: 【投影片 5 頁】 顯著，McNemar 檢定 p 小於 0.001。\n\n"
        "Q: 為什麼選這五個維度\n"
        "A: 【投影片 7 頁】 五個維度涵蓋三個層次。\n",
        encoding="utf-8",
    )
    p = QAPanel()
    p.set_backup_start_page(0)
    p.load_qa_file(str(bank))
    p.lang_combo.setCurrentIndex(0)      # 英文辨識（國際場合）
    app.processEvents()
    yield p
    p.deleteLater()
    app.processEvents()


def test_english_question_alone_misses_chinese_bank(panel, app):
    """先確立前提：只有英文原文時確實比不出來（這就是要解決的問題）。"""
    panel.append_recognized("is there any label leakage in your dataset")
    app.processEvents()
    match = panel._best_match(panel._recognized_accum)
    assert match is None or match.score < panel.NO_MATCH_SCORE


def test_translation_makes_the_match(panel, app):
    """譯文到達後應命中正確題目。"""
    panel.append_recognized("is there any label leakage in your dataset")
    app.processEvents()

    panel._on_translation_ready(
        "is there any label leakage in your dataset",
        "你的資料集有沒有標籤洩漏的問題？",
    )
    app.processEvents()

    assert "沒有，訓練與測試" in panel.answer_text.toPlainText()
    assert "🎯" in panel.match_info.text()


def test_translation_triggers_page_jump(panel, app):
    """命中的題目帶頁碼時要一併翻頁。"""
    pages: list[int] = []
    panel.goto_page.connect(pages.append)

    panel.append_recognized("are these results statistically significant")
    panel._on_translation_ready(
        "are these results statistically significant",
        "這些結果在統計上顯著嗎？",
    )
    app.processEvents()

    assert pages == [5]


def test_best_match_picks_higher_score(panel):
    """兩個候選都比得到時取分數高的那個。"""
    best = panel._best_match(
        "why did you choose these five dimensions",   # 英文，對中文題庫分數低
        "為什麼選這五個維度",                            # 譯文，幾乎完全吻合
    )
    assert best is not None
    assert "五個維度" in best.item.question
    assert best.score >= panel.NO_MATCH_SCORE


def test_irrelevant_translation_does_not_force_an_answer(panel, app):
    """譯文若與題庫無關，仍要守住門檻、不硬塞答案。"""
    panel.append_recognized("what time is lunch today")
    panel._on_translation_ready("what time is lunch today", "今天午餐幾點吃？")
    app.processEvents()

    assert panel.answer_text.toPlainText().strip() == ""
    assert "未匹配" in panel.match_info.text()


def test_translation_is_cleared_between_questions(panel, app):
    """換一題時舊譯文不能殘留，否則會拿上一題的翻譯去比對。"""
    panel.append_recognized("is there any label leakage")
    panel._on_translation_ready("is there any label leakage", "有標籤洩漏嗎？")
    app.processEvents()
    assert panel._last_translation

    panel.clear_question()
    assert panel._last_translation == ""


def test_empty_translation_is_ignored(panel, app):
    """翻譯失敗或空字串時不該影響既有匹配結果。"""
    panel.append_recognized("為什麼選這五個維度")
    app.processEvents()
    before = panel.answer_text.toPlainText()

    panel._on_translation_ready("為什麼選這五個維度", "")
    app.processEvents()
    assert panel.answer_text.toPlainText() == before
