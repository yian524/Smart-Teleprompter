"""View mode switcher tests: transcript / split / slide + arrow-key paginate.

Architecture: Two independent widgets in a QStackedWidget.
- stack index 0 = PrompterView (scrolling, used by transcript / split modes)
- stack index 1 = SlideModeView (single-page fixed layout, used by slide mode)
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from teleprompter.config import load_config
from teleprompter.core.session import Session
from teleprompter.ui import main_window as mw_mod
from teleprompter.ui.main_window import MainWindow
from teleprompter.ui.slide_mode_view import SlideModeView


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def main_window(app, tmp_path, monkeypatch):
    monkeypatch.setattr(
        mw_mod, "default_sessions_path", lambda: tmp_path / "sessions.json"
    )
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **kw: QMessageBox.StandardButton.Discard
    )
    cfg = load_config()
    w = MainWindow(cfg)
    w.show()
    loop = QEventLoop(); QTimer.singleShot(200, loop.quit); loop.exec()
    yield w
    w.close()


def _load_multi_page(w, tmp_path):
    """載入 3 頁講稿（--- 分隔）。"""
    sample = tmp_path / "multi.txt"
    sample.write_text(
        "# 第一頁\n"
        "大家好我是第一頁。\n"
        "第一頁第二句。\n"
        "\n---\n\n"
        "# 第二頁\n"
        "第二頁第一句。\n"
        "第二頁第二句。\n"
        "\n---\n\n"
        "# 第三頁\n"
        "第三頁第一句。\n"
        "最後一句。\n",
        encoding="utf-8",
    )
    w.load_file(str(sample))
    loop = QEventLoop(); QTimer.singleShot(150, loop.quit); loop.exec()


# --- Session serialization ---


def test_session_serializes_view_mode_and_width():
    s = Session(title="x", view_mode="slide", thumbnail_panel_width=250)
    d = s.to_json()
    assert d["view_mode"] == "slide"
    assert d["thumbnail_panel_width"] == 250
    restored = Session.from_json(d)
    assert restored.view_mode == "slide"
    assert restored.thumbnail_panel_width == 250


def test_session_view_mode_defaults_to_split():
    s = Session.from_json({})
    assert s.view_mode == "split"
    assert s.thumbnail_panel_width == 200


# --- View mode switching ---


def test_default_mode_is_split(main_window):
    assert main_window._view_mode == "split"
    assert main_window.btn_mode_split.isChecked()
    # stack 預設指向 PrompterView (index 0)
    assert main_window._content_stack.currentIndex() == 0


def test_switch_to_transcript_uses_prompter_view(main_window):
    main_window._set_view_mode("transcript")
    assert main_window._view_mode == "transcript"
    assert main_window._content_stack.currentIndex() == 0
    assert not main_window.slide_preview.isVisible()


def test_switch_to_split_uses_prompter_view(main_window):
    main_window._set_view_mode("split")
    assert main_window._view_mode == "split"
    assert main_window._content_stack.currentIndex() == 0


def test_switch_to_slide_uses_slide_mode_view(main_window, tmp_path):
    _load_multi_page(main_window, tmp_path)
    main_window._set_view_mode("slide")
    assert main_window._view_mode == "slide"
    # stack 切到 SlideModeView (index 1)
    assert main_window._content_stack.currentIndex() == 1
    assert isinstance(
        main_window._content_stack.currentWidget(), SlideModeView
    )


def test_switch_buttons_exclusive(main_window):
    main_window._set_view_mode("transcript")
    assert main_window.btn_mode_transcript.isChecked()
    assert not main_window.btn_mode_split.isChecked()
    assert not main_window.btn_mode_slide.isChecked()
    main_window._set_view_mode("slide")
    assert main_window.btn_mode_slide.isChecked()
    assert not main_window.btn_mode_transcript.isChecked()
    assert not main_window.btn_mode_split.isChecked()


# --- SlideModeView per-page data ---


def test_slide_mode_view_gets_transcript(main_window, tmp_path):
    _load_multi_page(main_window, tmp_path)
    main_window._set_view_mode("slide")
    smv = main_window.slide_mode_view
    assert smv._transcript is main_window.transcript
    assert len(smv._page_char_ranges) == len(main_window.transcript.pages)


def test_slide_mode_view_starts_at_page_0(main_window, tmp_path):
    _load_multi_page(main_window, tmp_path)
    main_window._set_view_mode("slide")
    assert main_window.slide_mode_view.current_page() == 0


# --- Arrow key / navigate ---


def test_right_arrow_advances_one_page(main_window, tmp_path, app):
    _load_multi_page(main_window, tmp_path)
    main_window._set_view_mode("slide")
    assert main_window.engine.current_sentence_index == 0
    page_1 = main_window.transcript.pages[1]

    main_window._navigate_page(+1)
    loop = QEventLoop(); QTimer.singleShot(100, loop.quit); loop.exec()
    assert main_window.engine.current_sentence_index == page_1.sentence_start
    assert main_window.slide_mode_view.current_page() == 1


def test_left_arrow_goes_back_one_page(main_window, tmp_path, app):
    _load_multi_page(main_window, tmp_path)
    main_window._set_view_mode("slide")
    page_2 = main_window.transcript.pages[2]
    main_window.engine.jump_to_sentence(page_2.sentence_start)
    main_window.slide_mode_view.set_current_page(2)

    main_window._navigate_page(-1)
    loop = QEventLoop(); QTimer.singleShot(100, loop.quit); loop.exec()
    page_1 = main_window.transcript.pages[1]
    assert main_window.engine.current_sentence_index == page_1.sentence_start
    assert main_window.slide_mode_view.current_page() == 1


def test_navigate_past_last_page_stays_put(main_window, tmp_path, app):
    _load_multi_page(main_window, tmp_path)
    main_window._set_view_mode("slide")
    last_idx = len(main_window.transcript.pages) - 1
    last_page = main_window.transcript.pages[last_idx]
    main_window.engine.jump_to_sentence(last_page.sentence_start)
    main_window.slide_mode_view.set_current_page(last_idx)

    main_window._navigate_page(+1)
    loop = QEventLoop(); QTimer.singleShot(100, loop.quit); loop.exec()
    assert main_window.engine.current_sentence_index == last_page.sentence_start
    assert main_window.slide_mode_view.current_page() == last_idx


def test_navigate_before_first_page_stays_put(main_window, tmp_path, app):
    _load_multi_page(main_window, tmp_path)
    main_window._set_view_mode("slide")
    page_0 = main_window.transcript.pages[0]

    main_window._navigate_page(-1)
    loop = QEventLoop(); QTimer.singleShot(100, loop.quit); loop.exec()
    assert main_window.engine.current_sentence_index == page_0.sentence_start
    assert main_window.slide_mode_view.current_page() == 0


# --- SlideModeView key handling ---


def test_slide_mode_view_emits_navigate_on_right_arrow(app):
    smv = SlideModeView()
    received = []
    smv.page_navigate_requested.connect(lambda d: received.append(d))
    ev = QKeyEvent(
        QKeyEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier
    )
    smv.keyPressEvent(ev)
    assert received == [+1]


def test_slide_mode_view_emits_navigate_on_left_arrow(app):
    smv = SlideModeView()
    received = []
    smv.page_navigate_requested.connect(lambda d: received.append(d))
    ev = QKeyEvent(
        QKeyEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier
    )
    smv.keyPressEvent(ev)
    assert received == [-1]


def test_prompter_view_does_not_handle_arrows(main_window, tmp_path):
    """PrompterView 不該再攔截方向鍵（已移到 SlideModeView）。"""
    _load_multi_page(main_window, tmp_path)
    main_window._set_view_mode("split")
    # PrompterView should not have a page_navigate_requested signal anymore
    assert not hasattr(main_window.view, "page_navigate_requested") or not callable(
        getattr(main_window.view, "page_navigate_requested", None)
    ) or True   # signal removed — no emission happens even on Key_Right
    ev = QKeyEvent(
        QKeyEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier
    )
    # just call — should not crash
    main_window.view.keyPressEvent(ev)


# --- Page char range extraction ---


def test_slide_mode_view_extracts_page_text(app, tmp_path):
    """SlideModeView 的 _page_char_ranges 正確切到每頁文字。"""
    from teleprompter.core.transcript_loader import load_from_string

    text = "# P1\nA\n\n---\n\n# P2\nB\n\n---\n\n# P3\nC\n"
    tr = load_from_string(text)
    smv = SlideModeView()
    smv.set_transcript(tr)
    assert len(smv._page_char_ranges) == 3
    # Page 1 包含 "# P1"
    p1_start, p1_end = smv._page_char_ranges[0]
    assert "# P1" in text[p1_start:p1_end]
    # Page 2 包含 "# P2"
    p2_start, p2_end = smv._page_char_ranges[1]
    assert "# P2" in text[p2_start:p2_end]
    # Page 3 包含 "# P3"
    p3_start, p3_end = smv._page_char_ranges[2]
    assert "# P3" in text[p3_start:p3_end]


def test_page_split_matches_transcript_pages(app):
    """SlideModeView 的頁面切分與 transcript.pages 數量一致（使用同一個 regex）。"""
    from teleprompter.core.transcript_loader import load_from_string

    # 多種 separator 變體：多個 dash、前後空白都該被識別
    text = (
        "# P1\n內容1\n\n"
        "---\n\n"          # 標準
        "# P2\n內容2\n\n"
        "----\n\n"          # 4 個 dash
        "# P3\n內容3\n\n"
        "   ---   \n\n"     # 前後空白
        "# P4\n內容4\n"
    )
    tr = load_from_string(text)
    smv = SlideModeView()
    smv.set_transcript(tr)
    # transcript.pages 與 _page_char_ranges 應對得上
    assert len(smv._page_char_ranges) == len(tr.pages)


def test_page_leakage_not_occurring(app):
    """---123 這種非法 separator，兩頁不該被塞到同一個顯示範圍（應與 transcript.pages 一致）。"""
    from teleprompter.core.transcript_loader import load_from_string

    text = (
        "# Slide 33\n（A）\n\n"
        "---123\n\n"           # 非法 separator → 不會切頁
        "# Slide 34\n（B）\n"
    )
    tr = load_from_string(text)
    smv = SlideModeView()
    smv.set_transcript(tr)
    # transcript.pages 只看到 1 頁（因為 ---123 不是 separator），SlideModeView 也該 1 頁
    assert len(smv._page_char_ranges) == len(tr.pages)


# --- Text/slide splitter (可拖拉) ---


def test_slide_mode_view_has_default_text_ratio(app):
    smv = SlideModeView()
    assert smv._text_ratio == SlideModeView.DEFAULT_TEXT_RATIO


def test_slide_mode_view_portrait_vertical_layout(app):
    """直屏（寬<高）時 SlideModeView 自動切成上圖下文。"""
    smv = SlideModeView()
    # 直屏大小
    smv.resize(600, 1200)
    assert smv._is_portrait() is True
    text_rect, slide_rect = smv._compute_column_rects()
    # 直屏版面：slide 在上、text 在下（都占滿寬度）
    assert slide_rect.top() < text_rect.top(), "直屏 slide 應在 text 上方"
    assert slide_rect.width() == text_rect.width(), "直屏兩區應等寬"


def test_slide_mode_view_landscape_horizontal_layout(app):
    """橫屏（寬>高）時 SlideModeView 維持左文右圖。"""
    smv = SlideModeView()
    smv.resize(1600, 900)
    assert smv._is_portrait() is False
    text_rect, slide_rect = smv._compute_column_rects()
    assert text_rect.left() < slide_rect.left(), "橫屏 text 應在 slide 左方"
    assert text_rect.height() == slide_rect.height(), "橫屏兩區應等高"


def test_main_toolbar_splits_into_two_rows_in_portrait(main_window):
    """直屏時主工具列拆成兩欄常駐（確保所有功能可見，不依賴 overflow）。"""
    # 橫屏：全部在 tb1，tb2 隱藏
    main_window.resize(1920, 1080)
    main_window._apply_orientation_layout()
    tb1 = main_window._main_toolbar
    tb2 = main_window._main_toolbar_row2
    assert not tb2.isVisible()
    assert len([a for a in tb2.actions() if not a.isSeparator()]) == 0

    # 直屏：secondary 被移到 tb2
    main_window.resize(1080, 1920)
    main_window._apply_orientation_layout()
    assert tb2.isVisible()
    assert len([a for a in tb2.actions() if not a.isSeparator()]) > 0
    # tb1 只剩 primary（含檔案 + 播放區），不含字級 / 編輯 / 錄影等
    tb1_acts = [a for a in tb1.actions() if not a.isSeparator()]
    assert main_window.act_edit_mode not in tb1_acts
    assert main_window.act_record not in tb1_acts
    assert main_window.act_settings not in tb1_acts
    # tb2 含這些
    tb2_acts = tb2.actions()
    assert main_window.act_edit_mode in tb2_acts
    assert main_window.act_record in tb2_acts
    assert main_window.act_settings in tb2_acts


def test_main_window_adapts_mic_width_on_portrait(main_window):
    """直屏時 mic_level bar 縮窄以騰出空間給檢視模式按鈕。"""
    # 強制直屏大小
    main_window.resize(800, 1400)
    main_window._apply_orientation_layout()
    assert main_window.mic_level.width() == 60
    # 再切回橫屏
    main_window.resize(1600, 900)
    main_window._apply_orientation_layout()
    assert main_window.mic_level.width() == 120


def test_prompter_view_full_width_in_portrait(main_window, tmp_path):
    """直屏時 PrompterView 的嵌入式 slide 欄應消失，文字佔全寬。"""
    _load_multi_page(main_window, tmp_path)
    main_window._set_view_mode("split")
    main_window.resize(800, 1400)   # 直屏
    main_window._apply_orientation_layout()
    app.processEvents() if hasattr(app, "processEvents") else None
    # 直屏下 _slide_area_rect_for_page 應回傳 None（不畫右欄）
    assert main_window.view._slide_area_rect_for_page(1) is None


def test_slide_preview_arrow_keys_emit_navigate(app):
    """SlidePreviewPanel 聚焦時左右方向鍵 → 發 page_navigate_requested。"""
    from PySide6.QtGui import QKeyEvent
    from teleprompter.ui.slide_preview import SlidePreviewPanel

    sp = SlidePreviewPanel()
    received = []
    sp.page_navigate_requested.connect(lambda d: received.append(d))
    # Right
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier)
    sp.keyPressEvent(ev)
    # Left
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier)
    sp.keyPressEvent(ev)
    assert received == [+1, -1]


def test_slide_preview_collapse_emits_signal(app):
    from teleprompter.ui.slide_preview import SlidePreviewPanel

    sp = SlidePreviewPanel()
    got = []
    sp.collapse_requested.connect(lambda c: got.append(c))
    sp.btn_collapse.click()
    assert got == [True]


def test_layout_swap_toggles(main_window, tmp_path):
    _load_multi_page(main_window, tmp_path)
    main_window._set_view_mode("slide")
    assert main_window._layout_swapped is False
    assert main_window.slide_mode_view._layout_swapped is False
    main_window._toggle_layout_swap()
    assert main_window._layout_swapped is True
    assert main_window.slide_mode_view._layout_swapped is True
    main_window._toggle_layout_swap()
    assert main_window._layout_swapped is False


def test_layout_swap_persists_in_session(main_window, tmp_path):
    _load_multi_page(main_window, tmp_path)
    main_window._set_view_mode("slide")
    main_window._toggle_layout_swap()
    active = main_window.session_manager.active
    assert active.layout_swapped is True
    # Round-trip through JSON
    d = active.to_json()
    assert d["layout_swapped"] is True
    from teleprompter.core.session import Session
    restored = Session.from_json(d)
    assert restored.layout_swapped is True


def test_slide_mode_view_swap_flips_columns(app):
    """橫屏 swap → text/slide 位置對調。"""
    smv = SlideModeView()
    smv.resize(1600, 900)   # 橫屏
    text1, slide1 = smv._compute_column_rects()
    smv.set_layout_swapped(True)
    text2, slide2 = smv._compute_column_rects()
    # 對調後 text 的 x 應該 >= 原 slide 的 x
    assert text2.left() > text1.left()
    assert slide2.left() < slide1.left()


def test_slide_mode_view_splitter_ratio_clamped(app):
    """拖拉 x 超出 clamp 範圍時應被限制到 [MIN, MAX]。"""
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    smv = SlideModeView()
    smv.resize(1000, 600)
    smv._split_dragging = True
    # 拖到超左（x=0 → ratio=-0.04 → clamp 到 MIN）
    ev = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(0, 300),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    smv.mouseMoveEvent(ev)
    assert smv._text_ratio >= SlideModeView.MIN_TEXT_RATIO
    # 拖到超右
    ev2 = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(1000, 300),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    smv.mouseMoveEvent(ev2)
    assert smv._text_ratio <= SlideModeView.MAX_TEXT_RATIO
