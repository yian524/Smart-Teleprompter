"""PrompterView 顯示行為測試（卡拉 OK 高亮 + 漏講標記）。"""

from __future__ import annotations

import os
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication

from teleprompter.ui.prompter_view import PrompterView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def view(qapp):
    v = PrompterView()
    v.set_text("第一句內容。第二句內容。第三句內容。第四句內容。第五句內容。")
    return v


# ==== 基本載入 ====

def test_set_text_initializes_clean_state(view):
    assert view._doc_length > 0
    assert view._display_pos == 0
    assert view._target_pos == 0
    assert view._skipped_ranges == []


def test_set_text_clears_previous_skipped(view):
    """重新載入應清除上一份講稿留下的漏講標記。"""
    view.mark_skipped(0, 5)
    assert len(view._skipped_ranges) == 1
    view.set_text("全新內容。")
    assert view._skipped_ranges == []


# ==== 字體調整 ====

def test_font_size_change_in_range(view):
    view.set_font_size(48)
    assert view.font_size() == 48
    view.set_font_size(24)
    assert view.font_size() == 24


def test_font_size_clamped_to_minimum(view):
    view.set_font_size(5)
    assert view.font_size() >= 12


def test_font_size_clamped_to_maximum(view):
    view.set_font_size(500)
    assert view.font_size() <= 120


def test_font_size_change_during_active_session(view):
    """字體調整不應影響已標記的漏講區段。"""
    view.set_position(20, animate=False)
    view.mark_skipped(5, 10)
    view.set_font_size(view.font_size() + 4)
    # 漏講區段仍然存在
    assert (5, 10) in view._skipped_ranges


# ==== 位置更新 ====

def test_set_position_updates_display_when_no_animate(view):
    view.set_position(15, animate=False)
    assert view._display_pos == 15
    assert view._target_pos == 15


def test_set_position_clamps_to_doc_length(view):
    view.set_position(99999, animate=False)
    assert view._display_pos == view._doc_length


def test_set_position_negative_clamps_to_zero(view):
    view.set_position(20, animate=False)
    view.set_position(-5, animate=False)
    assert view._display_pos == 0


# ==== 漏講標記 ====

def test_mark_skipped_basic(view):
    view.mark_skipped(5, 10)
    assert (5, 10) in view._skipped_ranges


def test_mark_skipped_merges_overlapping(view):
    view.mark_skipped(5, 10)
    view.mark_skipped(8, 15)
    # 應合併成 (5, 15)
    assert view._skipped_ranges == [(5, 15)]


def test_mark_skipped_merges_adjacent(view):
    view.mark_skipped(5, 10)
    view.mark_skipped(10, 15)
    assert view._skipped_ranges == [(5, 15)]


def test_mark_skipped_keeps_disjoint_separate(view):
    view.mark_skipped(2, 5)
    view.mark_skipped(10, 15)
    assert sorted(view._skipped_ranges) == [(2, 5), (10, 15)]


def test_mark_skipped_clamped_to_doc(view):
    """超過文件長度的範圍應被截斷而非崩潰。"""
    view.mark_skipped(0, 99999)
    # 應裁到 doc 長度
    s, e = view._skipped_ranges[0]
    assert s == 0
    assert e == view._doc_length


def test_mark_skipped_invalid_range_ignored(view):
    """end <= start 應靜默忽略。"""
    view.mark_skipped(10, 10)
    view.mark_skipped(15, 5)
    assert view._skipped_ranges == []


def test_clear_skipped_removes_all(view):
    view.mark_skipped(2, 5)
    view.mark_skipped(10, 15)
    view.clear_skipped()
    assert view._skipped_ranges == []


def test_clear_skipped_idempotent(view):
    """重複 clear 不應出問題。"""
    view.clear_skipped()
    view.clear_skipped()
    assert view._skipped_ranges == []


# ==== _iter_unskipped 邏輯 ====

def test_iter_unskipped_no_overlap(view):
    view._skipped_ranges = [(20, 30)]
    result = list(view._iter_unskipped(0, 10))
    assert result == [(0, 10)]


def test_iter_unskipped_skipped_in_middle(view):
    view._skipped_ranges = [(5, 10)]
    result = list(view._iter_unskipped(0, 20))
    assert result == [(0, 5), (10, 20)]


def test_iter_unskipped_skipped_covers_all(view):
    view._skipped_ranges = [(0, 100)]
    result = list(view._iter_unskipped(10, 50))
    assert result == []


def test_iter_unskipped_multiple_skips(view):
    view._skipped_ranges = [(5, 10), (15, 20), (25, 30)]
    result = list(view._iter_unskipped(0, 35))
    assert result == [(0, 5), (10, 15), (20, 25), (30, 35)]


# ==== 顏色設定 ====

def test_set_colors_keeps_skipped_marks(view):
    view.mark_skipped(5, 10)
    view.set_colors(spoken="#888", upcoming="#FFF", skipped="#F00")
    # 漏講區段不應因為換色而消失
    assert (5, 10) in view._skipped_ranges


def test_skipped_color_persists_after_position_advance(view):
    """位置往前推進不應蓋掉先前標記的漏講顏色。"""
    view.mark_skipped(10, 15)
    # 位置從 5 推進到 20（跨過 10-15 漏講區）
    view.set_position(5, animate=False)
    view.set_position(20, animate=False)
    # 漏講範圍仍應存在
    assert (10, 15) in view._skipped_ranges


# ==== 動畫時長設定 ====

def test_animation_duration_setter(view):
    view.set_animation_duration(50)
    assert view._pos_anim.duration() == 50


def test_animation_duration_zero_allowed(view):
    view.set_animation_duration(0)
    assert view._pos_anim.duration() == 0
